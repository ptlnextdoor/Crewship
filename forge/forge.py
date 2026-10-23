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
import json
import os
import re
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
    def __init__(self, cfg_path: Path):
        self.cfg = tomllib.loads(cfg_path.read_text())
        c = self.cfg
        self.repo = Path(c["repo"]).expanduser()
        self.home = Path(c.get("state_dir", f"~/.local/state/forge/{c['name']}")).expanduser()
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_file = self.home / "state.json"
        self.log_file = self.home / "forge.log"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {
            "cycle": 0, "merged": 0, "failed": 0, "started": now(), "history": []}
        self.tickets = self.repo / c.get("tickets_dir", "tickets")
        self.protected = c.get("protected_paths", ["gate.sh", "tickets/", ".forge/"])
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

    def prompt(self, name: str, **subs: str) -> str:
        text = (self.prompts / f"{name}.md").read_text()
        for k, v in {"PROJECT": self.cfg["name"], "GATE": self.cfg.get("gate", "./gate.sh"), **subs}.items():
            text = text.replace("{" + k + "}", v)
        return text

    # ---------- plumbing ----------
    def log(self, msg: str) -> None:
        line = f"{now()} {msg}"
        print(line, flush=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")

    def board(self, doing: str) -> None:
        """Pin what the loop is doing right now to a GitHub issue, so the repo shows live state."""
        if not self.cfg.get("pull_requests", True):
            return
        s = self.state
        hist = "\n".join(f"| {h['at']} | {h['ticket']} | {h['result']} |" for h in reversed(s['history'][-15:]))
        body = (f"**Now:** {doing}\n\nUpdated {now()} · cycle {s['cycle']} · merged {s['merged']} · failed {s['failed']} · "
                f"deadline {self.cfg.get('limits', {}).get('deadline', '-')}\n\n"
                f"Open tickets: {', '.join(p.stem for p in self.open_tickets()) or '-'}\n\n"
                f"| when (UTC) | ticket | result |\n|---|---|---|\n{hist}\n\n"
                f"PRs: every attempt opens one (label `forge`). Progress notes: `PROGRESS.md`.")
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
        r = subprocess.run(cmd, shell=True, cwd=cwd or self.repo, capture_output=True, text=True, timeout=timeout, env=self.env)
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
        out = logdir / f"{self.state['cycle']:05d}-{role}.log"
        cmd = [OMP, "-p", "--no-session", f"--model={model}", f"--thinking={thinking}",
               "--approval-mode=yolo", prompt]
        self.log(f"agent {role} model={model}:{thinking} cwd={cwd.name}")
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               env={**self.env, "FORGE_ROLE": role, "FORGE_STATE_DIR": str(self.home)})
            text = (p.stdout or "") + ("\n[stderr]\n" + p.stderr if p.stderr else "")
            rc = p.returncode
        except subprocess.TimeoutExpired as e:
            text = f"TIMEOUT after {timeout}s\n{(e.stdout or b'')[-4000:]!r}"
            rc = 124
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
        t = re.sub(r"^status:\s*\S+\s*$", f"status: {status}", t, count=1, flags=re.M)
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

    def plan(self) -> None:
        self.log("queue low: planner turn")
        self.board("planner is writing the next tickets")
        rc, out = self.agent("planner", self.prompt("planner"), self.repo)
        self.sh("git add -A tickets PROGRESS.md ROADMAP.md 2>/dev/null; git commit -qm 'forge: planner refills the ticket queue' || true")
        self.discard_strays("planner")
        self.push()

    def push(self) -> None:
        r = self.sh("git push -q origin main", timeout=120)
        if r.returncode:
            self.log(f"push failed: {r.stderr.strip()[:300]}")

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
        title = f"[{ticket.stem[:4]}] {title.group(1) if title else ticket.stem}"
        model = self.last_model.get(builder, self.cfg["roles"][builder]["model"])
        body = (f"Opened by forge (cycle {self.state['cycle']}). Builder: `{model}`.\n\n"
                f"The gate result and the cross-vendor review are posted below as comments; forge squash-merges on approval.\n\n"
                f"<details><summary>Ticket {ticket.relative_to(self.repo)}</summary>\n\n{ticket.read_text()}\n</details>")
        url = self.gh(f"pr create --base main --head {branch} --title {json.dumps(title)} --label forge", cwd=wt, body=body)
        if not url.startswith("http"):
            url = self.gh(f"pr create --base main --head {branch} --title {json.dumps(title)}", cwd=wt, body=body)
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
        """Squash-merge through GitHub so the history and the PR page show it. False = fall back to a local merge."""
        if not pr:
            return False
        r = self.sh(f"gh pr merge {pr} -R {self.gh_repo} --squash --delete-branch", timeout=180)
        if r.returncode:
            self.log(f"gh pr merge failed: {r.stderr.strip()[:200]}")
            return False
        self.sh("git checkout -q main && git pull -q --rebase origin main", timeout=120)
        return True

    def cycle(self) -> None:
        self.state["cycle"] += 1
        n = self.state["cycle"]
        self.sh("git checkout -q main && git pull -q --rebase origin main", timeout=120)
        if len(self.open_tickets()) < self.cfg.get("min_queue", 3):
            self.plan()
        tickets = self.open_tickets()
        if not tickets:
            self.log("no open tickets after planning; sleeping")
            time.sleep(600)
            return
        ticket = tickets[0]
        tid = ticket.stem
        self.log(f"cycle {n}: {tid} (attempt {self.attempts(ticket) + 1})")
        self.board(f"building `{tid}` (attempt {self.attempts(ticket) + 1})")
        self.set_status(ticket, "in_progress")
        self.sh(f"git add {ticket} && git commit -qm 'forge: start {tid}'")
        self.push()

        wt = self.home / "wt" / f"{n:05d}-{tid}"
        branch = f"forge/{n:05d}-{tid}"
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
                # one in-session repair pass with the gate output before giving up
                rc, out = self.agent(builder, f"The gate failed. Fix the code (never the gate or tests' expectations unless the ticket says so) until `{self.cfg.get('gate', './gate.sh')}` passes, then commit.\n\nGate output:\n{gout}", wt)
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
            rc, rout = self.agent(reviewer, self.prompt("reviewer", TICKET=ticket.read_text(), DIFF=diff[:120000]), wt, timeout_min=30)
            verdict = re.findall(r"VERDICT:\s*(APPROVE|REJECT)", rout)
            review = rout.split("[stderr]")[0].strip()
            self.pr_comment(self.pr, f"**review by `{self.last_model.get(reviewer, self.cfg['roles'][reviewer]['model'])}`**\n\n{review[-12000:]}")
            if not verdict or verdict[-1] != "APPROVE":
                return self.fail(ticket, f"reviewer did not approve.\n{rout[-3000:]}")
            # land: rebase on main, re-gate, then merge (through the PR when there is one)
            self.sh("git fetch -q origin && git rebase -q origin/main", cwd=wt)
            ok, gout = self.gate(wt)
            if not ok:
                return self.fail(ticket, f"gate failed after rebase on main.\n{gout[-2000:]}")
            done_note = f"Landed by {built_by}, approved by {self.last_model.get(reviewer, self.cfg['roles'][reviewer]['model'])}." + (f" PR: {self.pr}" if self.pr else "")
            if self.pr:
                self.sh(f"git push -q --force-with-lease origin {branch}", cwd=wt, timeout=120)
            merged_via_pr = self.pr_merge(self.pr, branch)
            if not merged_via_pr:
                self.sh("git checkout -q main && git pull -q --rebase origin main", timeout=120)
                r = self.sh(f"git merge -q --ff-only {branch} || git merge -q --no-ff -m 'forge: land {tid}' {branch}")
                if r.returncode:
                    self.sh("git merge --abort")
                    return self.fail(ticket, f"merge conflict landing {branch}")
            self.set_status(ticket, "done", done_note)
            self.sh(f"git add {ticket} && git commit -qm 'forge: done {tid}'")
            self.push()
            self.state["merged"] += 1
            self.state["history"] = (self.state["history"] + [{"cycle": n, "ticket": tid, "result": "merged", "at": now()}])[-200:]
            self.log(f"cycle {n}: MERGED {tid}")
        finally:
            self.sh(f"git worktree remove --force {wt}")
            self.sh(f"git branch -D {branch}")
            if not self.cfg.get("pull_requests", True):
                self.sh(f"git push -q origin --delete {branch} 2>/dev/null || true")
            self.save()
            self.board("between cycles")

    def fail(self, ticket: Path, why: str) -> None:
        tid = ticket.stem
        self.pr_close(getattr(self, "pr", ""), why)
        if getattr(self, "pr", ""):
            why = f"PR: {self.pr}\n{why}"
        self.sh("git checkout -q main")
        tries = self.attempts(ticket) + 1
        status = "blocked" if tries >= self.cfg.get("max_attempts", 3) else "retry"
        self.set_status(ticket, status, why[-4000:])
        self.sh(f"git add {ticket} && git commit -qm 'forge: {status} {tid}'")
        self.push()
        self.state["failed"] += 1
        self.state["history"] = (self.state["history"] + [{"cycle": self.state["cycle"], "ticket": tid, "result": status, "at": now()}])[-200:]
        self.log(f"cycle {self.state['cycle']}: {status.upper()} {tid}: {why.splitlines()[0][:200]}")

    def steward(self) -> None:
        self.log("steward turn: PROGRESS.md + roadmap + learnings")
        self.agent("steward", self.prompt("steward", STATE=json.dumps(self.state)[-6000:]), self.repo, timeout_min=30)
        self.sh("git add -A PROGRESS.md ROADMAP.md LEARNINGS.md tickets 2>/dev/null; git commit -qm 'forge: steward update' || true")
        self.discard_strays("steward")
        self.push()

    def discard_strays(self, role: str) -> None:
        """Planner and steward may only touch docs/tickets; drop anything else they changed on main."""
        stray = self.sh("git status --porcelain").stdout.strip()
        if stray:
            self.log(f"{role} left edits outside its paths, discarding: {stray.splitlines()[:5]}")
            self.sh("git reset -q --hard HEAD && git clean -qfd")

    def recover(self) -> None:
        """After a crash or restart: tickets left in_progress go back to retry, stale worktrees go."""
        self.sh("git checkout -q main; git pull -q --rebase origin main; git worktree prune")
        for p in sorted(self.tickets.glob("[!.]*.md")) if self.tickets.exists() else []:
            if re.search(r"^status:\s*in_progress\s*$", p.read_text(errors="replace")[:600], re.M):
                self.set_status(p, "retry")
                p.write_text(p.read_text() + f"\n\n## forge-recover {now()}\ninterrupted (loop restarted mid-cycle); not counted as an attempt\n")
                self.sh(f"git add {p} && git commit -qm 'forge: recover {p.stem} after restart'")
                self.log(f"recovered {p.stem} -> retry")
        for wt in (self.home / "wt").glob("*") if (self.home / "wt").exists() else []:
            self.sh(f"git worktree remove --force {wt}; rm -rf {wt}")
        for b in self.sh("git branch --list 'forge/*'").stdout.split():
            self.sh(f"git branch -D {b}")
        self.push()

    def run(self) -> None:
        self.log(f"forge start: {self.cfg['name']} repo={self.repo}")
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
                self.log("deadline reached; final steward pass, then idle")
                self.steward()
                gpu = Path(__file__).with_name("gpu.py")
                if gpu.exists():
                    self.sh(f"python3 {gpu} reap --all")
                return
            if not self.quota_ok():
                time.sleep(900)
                continue
            try:
                self.cycle()
                if self.state["cycle"] % every == 0:
                    self.steward()
            except QuotaExhausted as e:
                self.log(f"quota: no model left for {e}; ticket back to the queue, sleeping 15 min")
                self.recover()
                time.sleep(900)
            except Exception as e:  # keep the loop alive; record and move on
                self.log(f"cycle error: {type(e).__name__}: {str(e)[:500]}")
                self.recover()
                time.sleep(120)
            self.save()

    def status(self) -> None:
        s = self.state
        print(f"{self.cfg['name']}: cycle {s['cycle']} merged {s['merged']} failed {s['failed']} since {s['started']} (updated {s.get('updated')})")
        print("open:", ", ".join(p.stem for p in self.open_tickets()) or "-")
        for h in s["history"][-8:]:
            print(f"  {h['at']} {h['ticket']}: {h['result']}")
        if self.log_file.exists():
            print("".join(self.log_file.read_text().splitlines(True)[-6:]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "once", "status", "steward"])
    ap.add_argument("--config", required=True, type=Path)
    a = ap.parse_args()
    f = Forge(a.config.expanduser())
    {"run": f.run, "once": f.cycle, "status": f.status, "steward": f.steward}[a.cmd]()
    if a.cmd == "once":
        f.save()


if __name__ == "__main__":
    main()
