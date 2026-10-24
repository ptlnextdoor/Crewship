#!/usr/bin/env python3
"""forge: unattended build loop for one project repo, run by a systemd user service.

One cycle = pick the next open ticket, have a builder model implement it in a fresh git
worktree, run the project's gate (a script the builder may not edit), have a reviewer from a
different model family read the diff, then merge to main and push. Failures go back to the
ticket with the evidence. A planner turn refills the queue when it runs low. Every few
cycles a steward turn updates PROGRESS.md and the roadmap. Nothing here needs a laptop.

    forge.py run    --config ~/.config/forge/reflex1.toml   # the loop (systemd runs this)
    forge.py once   --config ...                            # one cycle, for testing
    forge.py status --config ...                            # what is happening, read-only

Design notes (sources: Anthropic "Effective harnesses for long-running agents" and
"Harness design for long-running application development"; loop-engineering practice):
- State lives in files, not in model context: tickets/*.md, PROGRESS.md, state.json, git log.
- One ticket per session, small and checkable. The gate is the acceptance oracle, and the
  builder's diff may not touch it (gate paths are protected; a diff that edits them is rejected).
- Generation and evaluation are separate models from different vendors.
- Hard limits: wall-clock deadline, per-session timeout, quota floor, GPU spend cap.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

OMP = shutil.which("omp") or str(Path.home() / ".local/bin/omp")
UTC = dt.timezone.utc


class QuotaExhausted(Exception):
    """Every model available to a role hit its provider's usage limit."""


def now() -> str:
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Forge:
    def __init__(self, cfg_path: Path, lane: int = 1):
        self.cfg_path = cfg_path
        self.cfg = tomllib.loads(cfg_path.read_text())
        c = self.cfg
        self.lane = lane
        self.lanes = int(c.get("lanes", 1))
        base_repo = Path(c["repo"]).expanduser()
        self.base_home = Path(c.get("state_dir", f"~/.local/state/forge/{c['name']}")).expanduser()
        # Lane 1 uses the configured checkout and state dir; lane k>1 gets its own clone and state,
        # so lanes never share a working tree, a branch name, or a cargo target dir.
        if lane == 1:
            self.repo, self.home = base_repo, self.base_home
        else:
            self.repo = base_repo.with_name(f"{base_repo.name}-lane{lane}")
            self.home = self.base_home / f"lane{lane}"
            if not self.repo.exists():
                origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=base_repo, capture_output=True, text=True).stdout.strip()
                subprocess.run(["git", "clone", "-q", origin, str(self.repo)], check=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_file = self.home / "state.json"
        self.log_file = self.base_home / "forge.log"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {
            "cycle": 0, "merged": 0, "failed": 0, "started": now(), "history": []}
        self.tickets = self.repo / c.get("tickets_dir", "tickets")
        self.protected = c.get("protected_paths", ["gate.sh", "tickets/", ".forge/", "STEERING.md"])
        self.exhausted: dict[str, float] = {}  # vendor -> when its quota ran out (cleared after an hour)
        self.last_model: dict[str, str] = {}  # role -> model that actually ran (after fallback)
        # Prompts are part of the Crewship setup, not the project: default to the ones beside this file.
        self.prompts = Path(c.get("prompts_dir", Path(__file__).resolve().parent / "prompts")).expanduser()
        home = str(Path.home())
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
        self.gh_repo = c.get("github_repo") or (m.group(1) if m else "")
        self.env = {**os.environ, "PATH": f"{home}/.cargo/bin:{home}/.local/bin:{os.environ.get('PATH', '')}",
                    **{k: os.path.expanduser(str(v)) for k, v in c.get("env", {}).items()}}
        if lane > 1 and "CARGO_TARGET_DIR" in self.env:
            self.env["CARGO_TARGET_DIR"] += f"-lane{lane}"

    @property
    def version(self) -> str:
        """Short hash of forge.py and the four role prompts as they are on disk now; prompt() re-reads the
        prompts on every call and a prompt-only merge does not restart the lanes, so hash at stamp time."""
        files = [Path(__file__), *(self.prompts / f"{r}.md" for r in ("planner", "builder", "reviewer", "steward"))]
        return hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()[:8]

    def prompt(self, name: str, **subs: str) -> str:
        text = (self.prompts / f"{name}.md").read_text()
        for k, v in {"PROJECT": self.cfg["name"], "GATE": self.cfg.get("gate", "./gate.sh"), **subs}.items():
            text = text.replace("{" + k + "}", v)
        return text

    # ---------- plumbing ----------
    def log(self, msg: str) -> None:
        line = f"{now()} [l{self.lane}] {msg}"
        print(line, flush=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")

    def all_states(self) -> list[dict]:
        out = [self.state if self.lane == 1 else json.loads((self.base_home / "state.json").read_text() or "{}")]
        for f in sorted(self.base_home.glob("lane*/state.json")):
            if f != self.state_file:
                try:
                    out.append(json.loads(f.read_text()))
                except (OSError, ValueError):
                    pass
        if self.lane != 1:
            out.append(self.state)
        return out

    def board(self, doing: str) -> None:
        """Pin what the loop is doing right now to a GitHub issue, so the repo shows live state. Lane 1 owns it."""
        if not self.cfg.get("pull_requests", True) or self.lane != 1:
            return
        s = self.state
        states = self.all_states()
        hist = sorted((h for st in states for h in st.get("history", [])), key=lambda h: h["at"])
        hist = "\n".join(f"| {h['at']} | {h['ticket']} | {h['result']} |" for h in reversed(hist[-20:]))
        lanes_now = []
        for p in sorted(self.tickets.glob("[!.]*.md")):
            m = re.search(r"^status:\s*in_progress\s+lane=(\d+)", p.read_text(errors="replace")[:600], re.M)
            if m:
                lanes_now.append(f"lane {m.group(1)}: `{p.stem}`")
        merged = sum(st.get("merged", 0) for st in states)
        failed = sum(st.get("failed", 0) for st in states)
        body = (f"**Now:** {doing}\n\n**Lanes ({self.lanes}):** {' · '.join(lanes_now) or 'idle'}\n\n"
                f"Updated {now()} · merged {merged} · failed {failed} · "
                f"deadline {self.cfg.get('limits', {}).get('deadline', '-')}\n\n"
                f"Open tickets: {', '.join(p.stem for p in self.open_tickets()) or '-'}\n\n"
                f"| when (UTC) | ticket | result |\n|---|---|---|\n{hist}\n\n"
                f"PRs: every attempt opens one (label `forge`). Progress notes: `PROGRESS.md`, per-ticket notes in `notes/`.")
        if not s.get("board"):
            found = self.gh("issue list --state open --label forge --search 'forge: live status in:title' --json url --jq '.[0].url'")
            if found.startswith("http"):
                s["board"] = found
        if not s.get("board"):
            url = self.gh("issue create --title 'forge: live status' --label forge", body=body)
            if url.startswith("http"):
                s["board"] = url
                self.gh(f"issue pin {url}")
        else:
            self.gh(f"issue edit {s['board']}", body=body)

    def save(self) -> None:
        self.state["updated"] = now()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_file)

    def sh(self, cmd: str, cwd: Path | None = None, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess:
        r = subprocess.run(cmd, shell=True, cwd=cwd or self.repo, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, env=self.env)
        if check and r.returncode:
            raise RuntimeError(f"{cmd} -> {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
        return r

    def agent(self, role: str, prompt: str, cwd: Path, timeout_min: int | None = None) -> tuple[int, str]:
        """Run one headless omp session for a role. On a provider quota error, try the role's
        `fallback` model, then any role with a model from another vendor. Returns (rc, tail)."""
        r = self.cfg["roles"][role]
        chain = [r["model"]] + r.get("fallback", [])
        for model in chain:
            if model.split("/")[0] in self.exhausted:
                continue
            rc, text = self._agent_once(role, model, r.get("thinking", "high"), prompt, cwd, timeout_min or r.get("timeout_min", 60))
            if re.search(r"usage_limit_reached|usage limit has been reached|rate_limit|quota", text[-3000:], re.I) and rc != 0:
                self.exhausted[model.split("/")[0]] = time.time()
                self.log(f"quota: {model} exhausted; trying the next model for {role}")
                continue
            self.last_model[role] = model
            return rc, text
        raise QuotaExhausted(role)

    def _agent_once(self, role: str, model: str, thinking: str, prompt: str, cwd: Path, timeout_min: int) -> tuple[int, str]:
        timeout = 60 * timeout_min
        logdir = self.home / "sessions"
        logdir.mkdir(exist_ok=True)
        # A role can run twice in one cycle: the builder repairs after a gate failure and again to resolve
        # a merge, a stalled review is rerun, the steward runs once more at the deadline. One name per
        # (cycle, role) let the second session overwrite the first, and the lost one is the session that
        # wrote the diff (reflex1 cycle 32 kept only the 1.5 KB merge run of a 25-minute builder).
        out = logdir / f"{self.state['cycle']:05d}-{role}.log"
        k = 1
        while out.exists():
            k += 1
            out = logdir / f"{self.state['cycle']:05d}-{role}-{k}.log"
        steer = self.repo / "STEERING.md"
        if steer.exists() and steer.read_text().strip():
            # The owner's standing instructions come first in every session (borrowed from
            # Autonomous-Company's Human Overrides). Agents may not edit this file.
            prompt = f"Owner's standing instructions (STEERING.md), these override anything below:\n\n{steer.read_text()}\n\n---\n\n{prompt}"
        # The prompt goes through a file (omp reads `@path`): one argv string is capped at 128 KiB on
        # Linux, and a review prompt with a 120,000-char diff plus ticket and STEERING.md exceeded it
        # (E2BIG, "failed to start agent", attempt charged to the ticket).
        pfile = out.with_suffix(".prompt")
        pfile.write_text(prompt)
        cmd = [OMP, "-p", "--no-session", f"--model={model}", f"--thinking={thinking}",
               "--approval-mode=yolo", f"@{pfile}"]
        self.log(f"agent {role} model={model}:{thinking} cwd={cwd.name}")
        try:
            # New session so a timeout kills the whole tree (cargo, rustc, test binaries), not just omp.
            # stdin from /dev/null: `omp -p` waits for EOF on a piped stdin (startup phase readPipedInput) before
            # it reads the prompt, so a loop started over `ssh host forge.py run` would hang every session.
            p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True, env={**self.env, "FORGE_ROLE": role, "FORGE_STATE_DIR": str(self.home)})
            try:
                so, se = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, 15)
                time.sleep(5)
                try:
                    os.killpg(p.pid, 9)
                except ProcessLookupError:
                    pass
                so, se = p.communicate()
                so = f"TIMEOUT after {timeout}s (process group killed)\n{(so or '')[-4000:]}"
            text = (so or "") + ("\n[stderr]\n" + se if se else "")
            rc = 124 if so.startswith("TIMEOUT") else p.returncode
        except OSError as e:
            text, rc = f"failed to start agent: {e}", 127
        out.write_text(text)
        return rc, text[-6000:]

    # ---------- budget ----------
    def quota_ok(self) -> bool:
        """Stop spending when the shared Claude/Codex windows are nearly used (the owner works too)."""
        floor = self.cfg.get("limits", {}).get("max_window_used_pct", 85)
        r = subprocess.run([OMP, "usage"], capture_output=True, text=True, timeout=120)
        # Pause only when every vendor the roles use is over the floor; one vendor out is handled
        # by per-role fallback. A vendor is over when any of its windows passes the floor.
        over = {}
        for block in re.split(r"\n(?=\S)", r.stdout):
            name = block.split("\n", 1)[0].lower()
            vendor = "anthropic" if "anthropic" in name else "openai-codex" if "codex" in name else None
            used = [float(x) for x in re.findall(r"([\d.]+)% used", block)]
            if vendor and used:
                over[vendor] = max(used) >= floor
        for v, is_over in over.items():
            if is_over:
                self.exhausted.setdefault(v, time.time())
            else:
                self.exhausted.pop(v, None)
        vendors = {x["model"].split("/")[0] for x in self.cfg["roles"].values()}
        if vendors and all(over.get(v, False) for v in vendors):
            self.log(f"quota: every vendor over {floor}% ({over}), pausing")
            return False
        return True

    def past_deadline(self) -> bool:
        d = self.cfg.get("limits", {}).get("deadline")
        return bool(d) and dt.datetime.now(UTC) >= dt.datetime.fromisoformat(d.replace("Z", "+00:00"))

    # ---------- tickets ----------
    def open_tickets(self) -> list[Path]:
        if not self.tickets.exists():
            return []
        out = []
        for p in sorted(self.tickets.glob("[!.]*.md")):
            head = p.read_text(errors="replace")[:600]
            if re.search(r"^status:\s*(open|retry)\s*$", head, re.M):
                out.append(p)
        return out

    def set_status(self, ticket: Path, status: str, note: str = "") -> None:
        t = ticket.read_text()
        t = re.sub(r"^status:.*$", f"status: {status}", t, count=1, flags=re.M)
        if note:
            t += f"\n\n## forge {now()} ({status})\n{note.strip()}\n"
        ticket.write_text(t)

    def attempts(self, ticket: Path) -> int:
        return ticket.read_text().count("## forge ")

    # ---------- the cycle ----------
    def gate(self, cwd: Path) -> tuple[bool, str]:
        g = self.cfg.get("gate", "./gate.sh")
        r = self.sh(g, cwd=cwd, timeout=60 * self.cfg.get("limits", {}).get("gate_timeout_min", 30))
        return r.returncode == 0, (r.stdout + r.stderr)[-5000:]

    def touched_protected(self, cwd: Path) -> list[str]:
        files = self.sh("git diff --name-only main...HEAD", cwd=cwd).stdout.split()
        return [f for f in files if any(f == p or f.startswith(p) for p in self.protected)]

    def startable(self) -> list[Path]:
        """Open tickets a lane could claim now: every `depends:` ticket is done."""
        return [t for t in self.open_tickets() if self.ready(t)]

    def plan(self) -> None:
        # The planner is told how many tickets can start now and how many it must add that can. Without
        # the number, 16 reflex1 turns wrote 8 to 9 tickets each with a median of 2 startable (the rest
        # chained on open tickets), so the queue was low again one cycle later: 10 turns in 13 h.
        ready, need = len(self.startable()), self.cfg.get("min_queue", 3)
        self.log(f"queue low: planner turn ({ready} of {need} tickets can start now)")
        self.board("planner is writing the next tickets")
        rc, out = self.agent("planner", self.prompt("planner", LANES=str(self.lanes), MIN_TICKETS=str(max(5, 2 * self.lanes)),
                                                    READY=str(ready), NEED=str(max(self.lanes, need - ready))), self.repo)
        self.commit_docs("tickets PROGRESS.md ROADMAP.md", "forge: planner refills the ticket queue")
        self.discard_strays("planner")
        self.log(f"planner: {len(self.startable())} tickets can start now (was {ready})")
        self.push()

    def push(self) -> None:
        """Push main; other lanes push too, so on rejection rebase onto origin and try again."""
        for i in range(4):
            r = self.sh("git push -q origin main", timeout=120)
            if r.returncode == 0:
                return
            self.sync_main()
            time.sleep(2 + 3 * i)
        self.log(f"push failed after retries: {r.stderr.strip()[:300]}")

    def sync_main(self) -> None:
        self.sh("git checkout -q main; git fetch -q origin", timeout=120)
        if self.sh("git rebase -q origin/main").returncode:
            self.sh("git rebase --abort; git reset -q --hard origin/main")
            self.log("sync: local main conflicted with origin; reset to origin/main")

    def ready(self, t: Path) -> bool:
        """A ticket can start when every ticket number in its `depends:` line is done."""
        m = re.search(r"^depends:\s*(.*)$", t.read_text(errors="replace")[:600], re.M)
        for num in re.findall(r"\d{4}", m.group(1) if m else ""):
            dep = next(iter(self.tickets.glob(f"{num}-*.md")), None)
            if dep and not re.search(r"^status:\s*done", dep.read_text(errors="replace")[:600], re.M):
                return False
        return True

    def claim(self) -> Path | None:
        """Take the first ready open ticket by pushing its status change; a rejected push means another lane won."""
        for _ in range(6):
            self.sync_main()
            tickets = [t for t in self.open_tickets() if self.ready(t)]
            if not tickets:
                return None
            t = tickets[0]
            self.set_status(t, f"in_progress lane={self.lane}")
            self.sh(f"git add {t} && git commit -qm 'forge: lane {self.lane} starts {t.stem}'")
            if self.sh("git push -q origin main", timeout=120).returncode == 0:
                return t
            self.sh("git reset -q --hard origin/main")
            time.sleep(3 + self.lane * 2)
        return None

    def rebase_onto_main(self, wt: Path, builder: str) -> bool:
        """Bring the branch up to date with main. Conflicts (another lane landed nearby) go to the builder once."""
        self.sh("git fetch -q origin", cwd=wt, timeout=120)
        if self.sh("git rebase -q origin/main", cwd=wt).returncode == 0:
            return True
        self.sh("git rebase --abort", cwd=wt)
        self.log("rebase conflicts with main; builder resolves via merge")
        self.agent(builder, "Another lane landed changes on main that conflict with this branch. Run `git merge origin/main`, "
                   "resolve every conflict keeping the intent of both sides, run the gate, and commit the merge. "
                   "Do not edit gate.sh, tickets/ or .forge/.", wt, timeout_min=30)
        if self.sh("git ls-files -u", cwd=wt).stdout.strip():
            return False
        self.sh("git add -A && git commit -qm 'forge: merge main' || true", cwd=wt)
        return self.sh("git merge-base --is-ancestor origin/main HEAD", cwd=wt).returncode == 0

    # ---------- GitHub visibility: one PR per attempt ----------
    def gh(self, args: str, cwd: Path | None = None, body: str | None = None) -> str:
        """Run gh; long bodies go through a file. Never raises: PR plumbing must not stop the loop."""
        if body is not None:
            f = self.home / "gh-body.md"
            f.write_text(body[:60000])
            args += f" --body-file {f}"
        # Crewship's gh guard requires an explicit repository target on every write.
        sub = args.split()[:2]
        if sub[0] in ("pr", "issue") and "-R " not in args and not (len(args.split()) > 2 and args.split()[2].startswith("http")):
            args = f"{args} -R {self.gh_repo}"
        r = self.sh(f"gh {args}", cwd=cwd, timeout=120)
        if r.returncode:
            self.log(f"gh {args.split()[0:2]} failed: {r.stderr.strip()[:200]}")
        return r.stdout.strip()

    def pr_open(self, wt: Path, branch: str, ticket: Path, builder: str) -> str:
        if not self.cfg.get("pull_requests", True):
            return ""
        self.sh(f"git push -q -u origin {branch}", cwd=wt, timeout=120)
        title = re.search(r"^# (.+)$", ticket.read_text(), re.M)
        # single-quoted: the heading is model-written text, and json.dumps left backticks live in the
        # shell (ticket 0052's `tools/baselines.py` ran for 22 s and its output became the PR title)
        title = shlex.quote(f"[{ticket.stem[:4]}] {title.group(1) if title else ticket.stem}")
        model = self.last_model.get(builder, self.cfg["roles"][builder]["model"])
        body = (f"Opened by forge (cycle {self.state['cycle']}). Builder: `{model}`.\n\n"
                f"The gate result and the cross-vendor review are posted below as comments; forge squash-merges on approval.\n\n"
                f"<details><summary>Ticket {ticket.relative_to(self.repo)}</summary>\n\n{ticket.read_text()}\n</details>")
        url = self.gh(f"pr create --base main --head {branch} --title {title} --label forge", cwd=wt, body=body)
        if not url.startswith("http"):
            url = self.gh(f"pr create --base main --head {branch} --title {title}", cwd=wt, body=body)
        if url.startswith("http"):
            self.log(f"PR {url}")
        return url if url.startswith("http") else ""

    def pr_comment(self, pr: str, text: str) -> None:
        if pr:
            self.gh(f"pr comment {pr}", body=text)

    def pr_close(self, pr: str, why: str) -> None:
        if pr:
            self.pr_comment(pr, f"**forge: not merged.**\n\n```\n{why[-6000:]}\n```\nThe ticket goes back to the queue with this evidence; the next attempt opens a new PR.")
            self.gh(f"pr close {pr}")

    def pr_merge(self, pr: str, branch: str) -> bool:
        """Merge through GitHub so the PR page shows it and the remote branch goes. False = fall back to a local merge."""
        if not pr:
            return False
        # GitHub recomputes mergeability after the push that precedes this call; merging before it has fails on the
        # stale state. Of reflex1's first 37 merges, 24 fell to the local path after a failed `gh pr merge`, and that
        # path never deletes the remote branch.
        for _ in range(10):
            if self.gh(f"pr view {pr} --json mergeable --jq .mergeable") == "MERGEABLE":
                break
            time.sleep(3)
        r = self.sh(f"gh pr merge {pr} -R {self.gh_repo} --merge --delete-branch", timeout=180)
        if r.returncode:
            self.log(f"gh pr merge failed: {r.stderr.strip()[:200]}")
            return False
        self.sync_main()
        return True

    def land(self, wt: Path, branch: str, tid: str, builder: str) -> str:
        """Bring the branch up to date with main, re-gate, merge (through the PR when there is one). Returns "" or
        why it could not. Main moves while the gate runs (8 to 25 min, four lanes): four approved reflex1 attempts
        in two hours died here on PROGRESS/LEARNINGS conflicts with a lane that landed meanwhile, so a conflict at
        the merge re-syncs and re-gates instead of costing the attempt."""
        for _ in range(3):
            if not self.rebase_onto_main(wt, builder):
                return "could not bring the branch up to date with main (conflicts the builder could not resolve)"
            ok, gout = self.gate(wt)
            if not ok:
                return f"gate failed after updating from main.\n{gout[-2000:]}"
            if self.pr:
                self.sh(f"git push -q --force-with-lease origin {branch}", cwd=wt, timeout=120)
            if self.pr_merge(self.pr, branch):
                return ""
            self.sync_main()
            if self.sh(f"git merge -q --no-ff -m 'forge: land {tid}' {branch}").returncode == 0:
                self.push()
                return ""
            self.sh("git merge --abort")
            self.log(f"main moved during the gate; landing {tid} again")
        return f"merge conflict landing {branch}"

    def cycle(self) -> None:
        self.state["cycle"] += 1
        n = self.state["cycle"]
        self.sync_main()
        if self.lane == 1 and len(self.startable()) < self.cfg.get("min_queue", 3):
            self.plan()
        ticket = self.claim()
        if not ticket:
            self.log("no open ticket to claim; sleeping")
            time.sleep(300 if self.lane == 1 else 120)
            return
        tid = ticket.stem
        self.log(f"cycle {n}: {tid} (attempt {self.attempts(ticket) + 1})")
        self.board(f"lane {self.lane} building `{tid}` (attempt {self.attempts(ticket) + 1})")

        wt = self.home / "wt" / f"{n:05d}-{tid}"
        branch = f"forge/l{self.lane}-{n:05d}-{tid}"
        self.sh(f"git worktree add -q -b {branch} {wt} main", check=True)
        self.pr = ""
        try:
            builder = "builder_hard" if re.search(r"^difficulty:\s*hard", ticket.read_text(), re.M) else "builder"
            if self.attempts(ticket) >= 2:
                builder = "builder_hard"  # escalate after repeated failure
            prompt = self.prompt("builder", TICKET=ticket.read_text(), TICKET_PATH=str(ticket.relative_to(self.repo)))
            rc, out = self.agent(builder, prompt, wt)
            self.sh("git add -A && git commit -qm 'forge: builder work in progress' || true", cwd=wt)
            if not self.sh("git diff --stat main...HEAD", cwd=wt).stdout.strip():
                return self.fail(ticket, f"builder produced no diff (rc={rc}).\n{out[-1500:]}")
            self.pr = self.pr_open(wt, branch, ticket, builder)
            bad = self.touched_protected(wt)
            if bad:
                return self.fail(ticket, f"diff edits protected gate/ticket paths {bad}; not allowed. Change the code, not the oracle.")
            ok, gout = self.gate(wt)
            self.pr_comment(self.pr, f"**gate: {'PASS' if ok else 'FAIL'}**\n\n```\n{gout[-3500:]}\n```")
            if not ok:
                # one repair pass before giving up. It is a fresh omp session (--no-session), so it gets the
                # ticket as well as the gate output: spec beats test feedback for repair (arXiv 2610.28834).
                rc, out = self.agent(builder, f"The gate failed. Fix the code (never the gate or tests' expectations unless the ticket says so) until `{self.cfg.get('gate', './gate.sh')}` passes, then commit.\n\nTicket ({ticket.relative_to(self.repo)}):\n{ticket.read_text()}\n\nGate output:\n{gout}", wt)
                self.sh("git add -A && git commit -qm 'forge: repair after gate' || true", cwd=wt)
                self.sh(f"git push -q origin {branch}", cwd=wt, timeout=120)
                bad = self.touched_protected(wt)
                ok, gout = self.gate(wt)
                self.pr_comment(self.pr, f"**gate after repair: {'PASS' if ok else 'FAIL'}**\n\n```\n{gout[-3500:]}\n```")
                if bad or not ok:
                    return self.fail(ticket, f"gate still failing after repair.\n{gout[-2500:]}")
            diff = self.sh("git diff main...HEAD", cwd=wt).stdout
            # Reviewer comes from the other vendor than the model that actually built it (cross-family review).
            built_by = self.last_model.get(builder, self.cfg["roles"][builder]["model"])
            same = built_by.split("/")[0] == self.cfg["roles"]["reviewer"]["model"].split("/")[0]
            reviewer = "reviewer_alt" if same and "reviewer_alt" in self.cfg["roles"] else "reviewer"
            rprompt = self.prompt("reviewer", TICKET=ticket.read_text(), DIFF=diff[:120000])
            rc, rout = self.agent(reviewer, rprompt, wt, timeout_min=30)
            verdict = re.findall(r"VERDICT:\s*(APPROVE|REJECT)", rout)
            if rc == 124 and not verdict:
                # a review that stalls to its timeout without a verdict is a harness failure, not a finding:
                # 4 of the first 22 reflex1 reviews did, and each cost the ticket a whole attempt. One rerun.
                self.log(f"review of {tid} timed out with no verdict; rerunning once")
                rc, rout = self.agent(reviewer, rprompt, wt, timeout_min=30)
                verdict = re.findall(r"VERDICT:\s*(APPROVE|REJECT)", rout)
            review = rout.split("[stderr]")[0].strip()
            self.pr_comment(self.pr, f"**review by `{self.last_model.get(reviewer, self.cfg['roles'][reviewer]['model'])}`**\n\n{review[-12000:]}")
            if not verdict or verdict[-1] != "APPROVE":
                return self.fail(ticket, f"reviewer did not approve.\n{rout[-3000:]}")
            why = self.land(wt, branch, tid, builder)
            if why:
                return self.fail(ticket, why)
            done_note = f"Landed by {built_by}, approved by {self.last_model.get(reviewer, self.cfg['roles'][reviewer]['model'])}." + (f" PR: {self.pr}" if self.pr else "")
            self.sync_main()
            self.set_status(ticket, "done", done_note)
            self.sh(f"git add {ticket} && git commit -qm 'forge: done {tid}'")
            self.push()
            self.state["merged"] += 1
            self.state["history"] = (self.state["history"] + [{"cycle": n, "ticket": tid, "result": "merged", "at": now(), "version": self.version}])[-200:]
            self.log(f"cycle {n}: MERGED {tid}")
        finally:
            self.sh(f"git worktree remove --force {wt}")
            self.prune_target()
            self.sh(f"git branch -D {branch}")
            if not self.cfg.get("pull_requests", True):
                self.sh(f"git push -q origin --delete {branch} 2>/dev/null || true")
            self.save()
            self.board("between cycles")

    def prune_target(self) -> None:
        """Drop the workspace's own artifacts from this lane's target dir once its worktree is gone. A fresh
        worktree rebuilds them anyway (new file mtimes), and every ticket that changes the dependency graph
        leaves them under new hashes: 94 GB of dead 300 MB test binaries across four lanes in 20 h. Artifacts
        of crates.io dependencies keep their hashes and stay."""
        tdir = self.env.get("CARGO_TARGET_DIR")
        if not tdir:
            return
        meta = json.loads(self.sh("cargo metadata --format-version 1 --no-deps").stdout or '{"packages": []}')
        pkgs = {p["name"].replace("-", "_") for p in meta["packages"]}
        targets = {t["name"].replace("-", "_") for p in meta["packages"] for t in p["targets"]}
        for f in [*Path(tdir).glob("*/deps/*"), *Path(tdir).glob("*/examples/*")]:
            name = f.name.split(".")[0].rpartition("-")[0]  # libreflex_core-<hash>.rlib, http-<hash>, http-<hash>.d
            if f.is_file() and (name in targets or name.removeprefix("lib") in pkgs):
                f.unlink()

    def fail(self, ticket: Path, why: str) -> None:
        tid = ticket.stem
        head = why.splitlines()[0][:200]  # the PR line prefixed below is for the ticket note; the log wants the cause
        self.pr_close(getattr(self, "pr", ""), why)
        if getattr(self, "pr", ""):
            why = f"PR: {self.pr}\n{why}"
        self.sync_main()
        tries = self.attempts(ticket) + 1
        status = "blocked" if tries >= self.cfg.get("max_attempts", 3) else "retry"
        self.set_status(ticket, status, why[-4000:])
        self.sh(f"git add {ticket} && git commit -qm 'forge: {status} {tid}'")
        self.push()
        self.state["failed"] += 1
        self.state["history"] = (self.state["history"] + [{"cycle": self.state["cycle"], "ticket": tid, "result": status, "at": now(), "version": self.version}])[-200:]
        self.log(f"cycle {self.state['cycle']}: {status.upper()} {tid}: {head}")

    def steward(self) -> None:
        self.log("steward turn: PROGRESS.md + roadmap + learnings")
        self.agent("steward", self.prompt("steward", STATE=json.dumps(self.state)[-6000:]), self.repo, timeout_min=30)
        self.commit_docs("PROGRESS.md SCORE.md ROADMAP.md LEARNINGS.md README.md docs/assets tickets", "forge: steward update")
        self.discard_strays("steward")
        self.push()

    def commit_docs(self, paths: str, msg: str) -> None:
        """Commit a role's doc edits. `git add -A a b c` is all-or-nothing: one path a project does not
        have and nothing at all is staged (the error went to /dev/null), so discard_strays threw the
        whole turn away. Stage the paths that exist; a path the role deleted outright is restored there."""
        here = " ".join(p for p in paths.split() if (self.repo / p).exists())
        if here:
            self.sh(f"git add -A {here}; git commit -qm '{msg}' || true")

    def discard_strays(self, role: str) -> None:
        """Planner and steward may only touch docs/tickets; drop anything else they changed on main."""
        stray = self.sh("git status --porcelain").stdout.strip()
        if stray:
            self.log(f"{role} left edits outside its paths, discarding: {stray.splitlines()[:5]}")
            self.sh("git reset -q --hard HEAD && git clean -qfd")

    def recover(self, all_lanes: bool = False) -> None:
        """After a crash or restart: this lane's in_progress tickets go back to retry, stale worktrees go.
        `all_lanes` (the supervisor at startup, when every lane is restarting) recovers every lane's tickets."""
        self.sync_main()
        self.sh("git worktree prune")
        if all_lanes:
            pat = r"^status:\s*in_progress(\s+lane=\d+)?\s*$"
        elif self.lane == 1:
            pat = r"^status:\s*in_progress(\s+lane=1)?\s*$"
        else:
            pat = rf"^status:\s*in_progress\s+lane={self.lane}\s*$"
        for p in sorted(self.tickets.glob("[!.]*.md")) if self.tickets.exists() else []:
            if re.search(pat, p.read_text(errors="replace")[:600], re.M):
                self.set_status(p, "retry")
                p.write_text(p.read_text() + f"\n\n## forge-recover {now()}\ninterrupted (loop restarted mid-cycle); not counted as an attempt\n")
                self.sh(f"git add {p} && git commit -qm 'forge: recover {p.stem} after restart'")
                self.log(f"recovered {p.stem} -> retry")
        for wt in (self.home / "wt").glob("*") if (self.home / "wt").exists() else []:
            self.sh(f"git worktree remove --force {wt}; rm -rf {wt}")
        for b in self.sh("git branch --list 'forge/*'").stdout.split():
            self.sh(f"git branch -D {b}")
        self.push()
        self.prune_target()

    def run(self) -> None:
        """Supervisor: start one process per lane and keep them alive until the deadline.
        A lane exits 75 when forge.py changes on disk; it is restarted at once on the new code."""
        self.log(f"forge supervisor: {self.cfg['name']} with {self.lanes} lane(s)")
        self.recover(all_lanes=True)
        procs: dict[int, subprocess.Popen] = {}

        def start(k: int) -> None:
            procs[k] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "lane",
                                         "--config", str(self.cfg_path), "--lane", str(k)])

        for k in range(1, self.lanes + 1):
            start(k)
            time.sleep(15)
        while procs:
            time.sleep(20)
            for k, p in list(procs.items()):
                rc = p.poll()
                if rc is None:
                    continue
                if rc == 0 and self.past_deadline():
                    del procs[k]
                    continue
                self.log(f"lane {k} exited rc={rc}; restarting")
                if rc != 75:
                    time.sleep(30)
                start(k)
        gpu = Path(__file__).with_name("gpu.py")
        if gpu.exists():
            self.sh(f"python3 {gpu} reap --all")
        self.log("all lanes finished at the deadline")

    def lane_loop(self) -> None:
        self.log(f"lane {self.lane} start: repo={self.repo}")
        self.recover()
        every = self.cfg.get("steward_every", 4)
        me = Path(__file__).resolve()
        born = me.stat().st_mtime
        while True:
            if me.stat().st_mtime != born:
                # forge.py was updated: exit between cycles; systemd restarts us on the new code.
                self.log("forge.py changed on disk; restarting between cycles to load it")
                self.save()
                sys.exit(75)
            if self.past_deadline():
                self.log("deadline reached")
                if self.lane == 1:
                    self.steward()
                return
            if not self.quota_ok():
                time.sleep(900)
                continue
            try:
                self.cycle()
                if self.lane == 1 and self.state["cycle"] % every == 0:
                    self.steward()
            except QuotaExhausted as e:
                self.log(f"quota: no model left for {e}; ticket back to the queue, sleeping 15 min")
                self.recover()
                time.sleep(900)
            except Exception as e:  # keep the loop alive; record and move on
                self.log(f"cycle error: {type(e).__name__}: {str(e)[:500]}")
                self.recover()
                # Circuit breaker (from Autonomous-Company): consecutive errors back off exponentially.
                self.errors = getattr(self, "errors", 0) + 1
                time.sleep(min(1800, 120 * 2 ** (self.errors - 1)))
            else:
                self.errors = 0
            self.save()

    def status(self) -> None:
        states = self.all_states()
        print(f"{self.cfg['name']}: {self.lanes} lane(s) · merged {sum(s.get('merged', 0) for s in states)} · failed {sum(s.get('failed', 0) for s in states)}")
        busy = []
        for p in sorted(self.tickets.glob("[!.]*.md")):
            m = re.search(r"^status:\s*in_progress\s+lane=(\d+)", p.read_text(errors="replace")[:600], re.M)
            if m:
                busy.append(f"{p.stem} (lane {m.group(1)})")
        print("building:", ", ".join(busy) or "-")
        print("open:", ", ".join(p.stem for p in self.open_tickets()) or "-")
        for h in sorted((h for s in states for h in s.get("history", [])), key=lambda h: h["at"])[-8:]:
            print(f"  {h['at']} {h['ticket']}: {h['result']}")
        by_version: dict[str, list[int]] = {}
        for h in (h for s in states for h in s.get("history", [])):
            t = by_version.setdefault(h.get("version", "pre"), [0, 0])
            t[0] += h["result"] == "merged"
            t[1] += 1
        print("merged/attempts per forge version (forge.py+prompts, now", self.version + "):", ", ".join(f"{v} {m}/{n}" for v, (m, n) in by_version.items()))
        if self.log_file.exists():
            print("".join(self.log_file.read_text().splitlines(True)[-8:]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "lane", "once", "status", "steward"])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--lane", type=int, default=1)
    a = ap.parse_args()
    f = Forge(a.config.expanduser(), a.lane)
    {"run": f.run, "lane": f.lane_loop, "once": f.cycle, "status": f.status, "steward": f.steward}[a.cmd]()
    if a.cmd == "once":
        f.save()


if __name__ == "__main__":
    main()
