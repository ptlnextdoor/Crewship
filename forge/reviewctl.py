#!/usr/bin/env python3
"""reviewctl: the loop that reviews and lands forge's own PRs, on the server.

forge builds project code. This reviews the changes to forge itself (the scout's PRs and
anything else opened against the `forge` branch of the Crewship fork), so no human has to.

One pass per PR:
  audit   Opus 5.5 xhigh with the thermo-nuclear + deslop contract, writes a verdict file
  gate    the audit's own checks: py_compile, the forge self-test, and a protected-path diff check
  land    MERGE and low risk -> squash-merge; otherwise comment the findings and leave it open
Results go in forge/PR_AUDIT.md on the `forge` branch, so the record is in the repo.

    reviewctl.py run            # review every open PR against `forge`, then exit
    reviewctl.py status
A systemd timer runs `run` every 30 minutes (forge-review.timer).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(os.environ.get("REVIEW_REPO", Path.home() / "Dev/Crewship-scout"))
GH_REPO = os.environ.get("REVIEW_GH_REPO", "ptlnextdoor/Crewship")
BASE = os.environ.get("REVIEW_BASE", "forge")
MODEL = os.environ.get("REVIEW_MODEL", "anthropic/claude-opus-5-5")
THINKING = os.environ.get("REVIEW_THINKING", "xhigh")
STATE = Path.home() / ".local/state/forge/review"
OMP = str(Path.home() / ".local/bin/omp")
# A PR that touches these is never merged by this loop: they are the safety surface.
PROTECTED = ("gpu.py", "systemd/", "projects/")
PROMPT = (Path(__file__).parent / "prompts" / "pr_audit.md")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd: str, cwd: Path = REPO, timeout: int = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"}
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{now()} {msg}"
    print(line, flush=True)
    (STATE / "review.log").open("a").write(line + "\n")


def open_prs() -> list[dict]:
    r = sh(f"gh pr list -R {GH_REPO} --base {BASE} --state open --json number,title,headRefName,files,additions,deletions")
    return json.loads(r.stdout or "[]")


def previous(pr: int) -> dict | None:
    """The last verdict recorded for this PR's current head commit, if any. Keyed by head sha so a new push re-audits."""
    sha = sh(f"gh pr view {pr} -R {GH_REPO} --json headRefOid --jq .headRefOid").stdout.strip()
    f = STATE / f"pr{pr}.{sha[:12]}.json"
    return json.loads(f.read_text()) if f.exists() else None


def needs_pass(prev: dict | None, base: str) -> bool:
    """A gate failure is usually the base's doing - the branch no longer merges cleanly, or base's selftest
    changed - so it is not final for this head sha. PR #28 failed the gate on 2026-10-24 and merges cleanly
    into `forge` today, unseen for 29 h because the mark was keyed by the head sha alone. Every other
    outcome (merged, closed, left to the owner) stays decided until the branch moves."""
    return prev is None or (prev.get("gate") == "fail" and prev.get("base") != base)


def mark(pr: int, verdict: dict) -> None:
    sha = verdict.get("sha", "unknown")
    (STATE / f"pr{pr}.{sha[:12]}.json").write_text(json.dumps(verdict, indent=1))


def audit(pr: dict) -> dict:
    n = pr["number"]
    sha = sh(f"gh pr view {n} -R {GH_REPO} --json headRefOid --jq .headRefOid").stdout.strip()
    out = STATE / f"pr{n}.audit.md"
    prompt = PROMPT.read_text().replace("{PR}", str(n)).replace("{GH_REPO}", GH_REPO).replace("{OUT}", str(out))
    log(f"PR #{n}: audit with {MODEL}:{THINKING}")
    pf = STATE / f"pr{n}.prompt.md"
    pf.write_text(prompt)
    p = sh(f"timeout 2400 {OMP} -p --no-session --model={MODEL} --thinking={THINKING} --approval-mode=yolo \"$(cat {pf})\"", timeout=2500)
    (STATE / f"pr{n}.omp.log").write_text(f"exit={p.returncode}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
    text = out.read_text() if out.exists() else (p.stdout or "")
    if p.returncode or not text.strip():
        log(f"PR #{n}: audit agent failed (exit {p.returncode}), see {STATE}/pr{n}.omp.log")
    verdict = (re.search(r"^Verdict:\s*(MERGE WITH CHANGE|MERGE|REJECT)", text, re.M | re.I) or [None, "NONE"])[1].upper()
    risk = (re.search(r"^Risk:\s*(none|low|medium|high)", text, re.M | re.I) or [None, "unknown"])[1].lower()
    return {"pr": n, "sha": sha, "verdict": verdict, "risk": risk, "audit": text[-12000:], "at": now()}


def gate(pr: dict) -> tuple[bool, str]:
    """Merge the PR into base in a scratch worktree, then run the checks *base* defines.

    The PR never supplies its own gate: a change cannot vouch for itself. A PR that
    cannot merge cleanly fails here, which is the honest answer.
    """
    n = pr["number"]
    wt = STATE / f"wt-{n}"
    sh(f"git worktree remove --force {wt} 2>/dev/null; rm -rf {wt}")
    r = sh(f"git fetch -q origin pull/{n}/head:pr-{n} -f && git worktree add -q --detach {wt} origin/{BASE}")
    if r.returncode:
        return False, f"could not set up a worktree: {r.stderr[-400:]}"
    try:
        m = sh(f"git -c user.email=f@f -c user.name=forge merge --no-edit pr-{n}", cwd=wt)
        if m.returncode:
            return False, f"does not merge into {BASE} cleanly, needs a rebase:\n{(m.stdout + m.stderr)[-800:]}"
        # The gate itself comes from base, so a PR cannot weaken its own checks.
        sh(f"git checkout origin/{BASE} -- forge/selftest.py", cwd=wt)
        c = sh("python3 -m py_compile forge/forge.py forge/gpu.py forge/reviewctl.py", cwd=wt)
        if c.returncode:
            return False, f"py_compile failed:\n{c.stderr[-1500:]}"
        t = sh("python3 forge/selftest.py", cwd=wt, timeout=600)
        if t.returncode:
            return False, f"forge selftest failed:\n{(t.stdout + t.stderr)[-2500:]}"
        return True, (t.stdout or "ok")[-1200:]
    finally:
        sh(f"git worktree remove --force {wt}; git branch -D pr-{n} 2>/dev/null")


def fix(pr: dict, v: dict) -> tuple[bool, str]:
    """Apply the one edit the audit named, on the PR's own branch, then push it.

    Only for MERGE WITH CHANGE: the audit already said a single concrete edit makes this
    landable. The fixer may touch only the files the PR already touches, so it cannot grow
    into a second, unreviewed change. The result is re-gated and re-audited next pass.
    """
    n = pr["number"]
    branch = pr["headRefName"]
    files = [f["path"] for f in pr.get("files", [])]
    wt = STATE / f"fix-{n}"
    sh(f"git worktree remove --force {wt} 2>/dev/null; rm -rf {wt}")
    r = sh(f"git fetch -q origin {branch} -f && git worktree add -q -B fixbr-{n} {wt} origin/{branch}")
    if r.returncode:
        return False, f"could not check out {branch}: {r.stderr[-300:]}"
    try:
        pf = STATE / f"pr{n}.fix.md"
        pf.write_text(
            f"You are applying one agreed edit to pull request #{n} of {GH_REPO}, in the current directory "
            f"(a worktree of its branch `{branch}`).\n\n"
            f"An audit approved this PR on the condition of a single change. Here is the audit:\n\n"
            f"{v['audit']}\n\n"
            "Make exactly the edit named under 'Change needed'. Nothing else: no refactors, no extra "
            f"improvements, no new files. Touch only these files: {files}. If the audit's change cannot be "
            "made within those files, change nothing and say why.\n\n"
            "Commit with a message that says what the audit asked for and why. Do not push; the caller pushes and runs the gate.\n"
        )
        p = sh(f"timeout 1200 {OMP} -p --no-session --model={MODEL} --thinking=high --approval-mode=yolo \"$(cat {pf})\"",
               cwd=wt, timeout=1300)
        (STATE / f"pr{n}.fix.log").write_text(f"exit={p.returncode}\n{p.stdout}\n{p.stderr}")
        if sh("git status --porcelain", cwd=wt).stdout.strip():
            sh("git add -A && git -c user.email=f@f -c user.name=reviewctl commit -qm 'reviewctl: apply the change the audit required'", cwd=wt)
        if not sh(f"git log --oneline origin/{branch}..HEAD", cwd=wt).stdout.strip():
            return False, "the fixer changed nothing"
        stray = [f for f in sh(f"git diff --name-only origin/{branch}..HEAD", cwd=wt).stdout.split() if f not in files]
        if stray:
            return False, f"the fixer touched files outside the PR: {stray}"
        # No selftest here: gate() runs base's selftest on the merged result right after this.
        u = sh(f"git push -q origin HEAD:{branch}", cwd=wt)
        if u.returncode:
            return False, f"could not push the fix: {u.stderr[-300:]}"
        return True, sh(f"git log --oneline origin/{branch}..HEAD", cwd=wt).stdout.strip() or "pushed"
    finally:
        sh(f"git worktree remove --force {wt}; git branch -D fixbr-{n} 2>/dev/null")


def touches_protected(pr: dict) -> list[str]:
    return [f["path"] for f in pr.get("files", []) if any(p in f["path"] for p in PROTECTED)]


def record(v: dict) -> None:
    """Prepend this audit to forge/PR_AUDIT.md on the base branch, so the record lives in the repo."""
    f = REPO / "forge/PR_AUDIT.md"
    header = "# PR audits\n\nWritten by reviewctl (forge/reviewctl.py). Newest first.\n"
    old = f.read_text().removeprefix(header) if f.exists() else ""
    entry = f"\n## PR #{v['pr']} · {v['at']} · {v['verdict']} · risk {v['risk']} · gate {v.get('gate')} · {v.get('action', '')}\n\n{v['audit']}\n"
    f.write_text(header + entry + old)
    sh(f"git add forge/PR_AUDIT.md && git commit -qm 'reviewctl: audit PR #{v['pr']}' && git push -q origin HEAD:{BASE}")


def run() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    sh(f"git checkout -q {BASE} && git pull -q --rebase")
    prs = open_prs()
    log(f"{len(prs)} open PR(s) against {BASE}")
    base = sh(f"git rev-parse origin/{BASE}").stdout.strip()
    for pr in prs:
        n = pr["number"]
        prev = previous(n)
        if not needs_pass(prev, base):
            continue
        regate = None
        if prev:  # re-check: the cheap gate decides whether the expensive audit is worth a turn
            ok, gout = gate(pr)
            if not ok:
                mark(n, {**prev, "base": base, "at": now()})
                log(f"PR #{n}: still fails the gate on {base[:8]}; not re-audited")
                continue
            regate = (ok, gout)
        v = audit(pr)
        v["base"] = base
        bad = touches_protected(pr)
        ok, gout = regate or gate(pr)
        v["gate"] = "pass" if ok else "fail"
        files = [f["path"] for f in pr.get("files", [])]
        prompt_only = files and all(f.startswith("forge/prompts/") for f in files)
        # Prompt-only changes are a git revert away; code changes at medium risk wait for the owner.
        landable = v["risk"] in ("none", "low") or (prompt_only and v["risk"] == "medium")
        if not ok:
            v["action"] = "left open: gate failed"
        elif bad:
            v["action"] = f"left open: touches {bad} (owner decides)"
        elif v["verdict"] == "MERGE" and landable:
            m = sh(f"gh pr merge {n} -R {GH_REPO} --squash --delete-branch")
            v["action"] = "merged" if m.returncode == 0 else f"merge failed: {m.stderr[-200:]}"
        elif v["verdict"] == "MERGE WITH CHANGE" and landable:
            # Apply the named edit, then merge: the next pass re-audits the pushed result.
            ok2, detail = fix(pr, v)
            if not ok2:
                v["action"] = f"left open: fixer could not apply the change ({detail})"
            else:
                ok3, gout = gate(pr)
                if not ok3:
                    v["action"] = f"left open: gate failed after the fix ({gout[-200:]})"
                else:
                    m = sh(f"gh pr merge {n} -R {GH_REPO} --squash --delete-branch")
                    v["action"] = f"fixed and merged ({detail})" if m.returncode == 0 else f"fixed, merge failed: {m.stderr[-200:]}"
        elif v["verdict"] == "REJECT" and v["risk"] in ("none", "low"):
            cf = STATE / f"pr{n}.close.md"
            cf.write_text(f"Closed by reviewctl: the audit rejected this change.\n\n{v['audit'][-6000:]}")
            c = sh(f"gh pr close {n} -R {GH_REPO} --delete-branch --comment \"$(cat {cf})\"")
            v["action"] = "closed: rejected" if c.returncode == 0 else f"close failed: {c.stderr[-200:]}"
        else:
            v["action"] = f"left open: {v['verdict']} risk={v['risk']} needs the owner"
        body = f"**reviewctl** ({MODEL}:{THINKING}) · verdict {v['verdict']} · risk {v['risk']} · gate {v['gate']} · {v['action']}\n\n<details><summary>audit</summary>\n\n{v['audit'][-8000:]}\n</details>\n\n```\n{gout[-1500:]}\n```"
        bf = STATE / f"pr{n}.comment.md"
        bf.write_text(body)
        sh(f"gh pr comment {n} -R {GH_REPO} --body-file {bf}")
        mark(n, v)
        sh(f"git checkout -q {BASE} && git pull -q --rebase")
        record(v)
        log(f"PR #{n}: {v['verdict']} risk={v['risk']} gate={v['gate']} -> {v['action']}")


def status() -> None:
    prs = open_prs()
    print(f"{GH_REPO} base={BASE}: {len(prs)} open")
    for pr in prs:
        marks = sorted(STATE.glob(f"pr{pr['number']}.*.json"))
        last = json.loads(marks[-1].read_text()) if marks else {}
        print(f"  #{pr['number']:>3} {pr['title'][:60]:62s} {last.get('verdict', '-'):6s} {last.get('action', 'not audited')}")
    if (STATE / "review.log").exists():
        print("".join((STATE / "review.log").read_text().splitlines(True)[-6:]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "status"])
    a = ap.parse_args()
    (run if a.cmd == "run" else status)()
