#!/usr/bin/env python3
"""forge selftest: the checks a change to forge itself must pass. No network, no models.

Run from a checkout root: `python3 forge/selftest.py`. Exit 0 = safe to land.
Each check is a fact about the loop that a careless edit could break.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else ': ' + detail}")
    if not ok:
        fails.append(name)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(cmd: str, cwd: Path) -> str:
    return subprocess.run(f"git {cmd}", shell=True, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


forge = load("forge_mod", ROOT / "forge.py")
gpu = load("gpu_mod", ROOT / "gpu.py")

# 1. Every role in a shipped project config must resolve, and fallbacks must name real providers.
for cfg in sorted((ROOT / "projects").glob("*.toml")):
    import tomllib
    c = tomllib.loads(cfg.read_text())
    roles = c.get("roles", {})
    check(f"{cfg.name}: builder+reviewer roles exist", {"builder", "reviewer"} <= set(roles))
    for r, v in roles.items():
        chain = [v["model"]] + v.get("fallback", [])
        check(f"{cfg.name}: {r} models are provider/model", all("/" in m for m in chain), str(chain))
    check(f"{cfg.name}: lanes >= 1", int(c.get("lanes", 1)) >= 1)
    check(f"{cfg.name}: gate is not editable by builders",
          any(p in str(c.get("protected_paths", [])) for p in ("gate.sh",)))

# 2. The protected-path check must reject a diff that touches the gate or tickets.
with tempfile.TemporaryDirectory() as d:
    repo = Path(d) / "repo"
    (repo / "tickets").mkdir(parents=True)
    (repo / "gate.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "tickets/0001-x.md").write_text("status: open\n---\n# x\n")
    git("init -q -b main", repo); git("add -A", repo); git("commit -qm i", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    (repo / "gate.sh").write_text("#!/bin/sh\nexit 0\n# tampered\n")
    git("add -A", repo); git("commit -qm t", repo); git("branch -f other", repo)
    git("checkout -q -b work main", repo)
    (repo / "gate.sh").write_text("#!/bin/sh\nexit 0\n# tampered again\n")
    git("add -A", repo); git("commit -qm t2", repo)
    check("protected paths catch a gate edit", "gate.sh" in f.touched_protected(repo))
    # 3. A ticket's status round-trips, and an interrupted ticket is not charged an attempt.
    t = repo / "tickets/0001-x.md"
    f.set_status(t, "in_progress lane=2")
    check("set_status writes lane", "status: in_progress lane=2" in t.read_text())
    before = f.attempts(t)
    f.set_status(t, "retry")
    t.write_text(t.read_text() + "\n\n## forge-recover x\ninterrupted\n")
    check("recover note is not an attempt", f.attempts(t) == before)
    f.set_status(t, "retry", "real failure")
    check("a real failure is an attempt", f.attempts(t) == before + 1)
    # 4. ready() must respect depends:.
    dep = repo / "tickets/0002-y.md"
    dep.write_text("status: open\ndepends: 0001\n---\n# y\n")
    check("depends blocks an unfinished prerequisite", not f.ready(dep))
    f.set_status(t, "done")
    check("depends clears when the prerequisite is done", f.ready(dep))
    # 4b. The PR title is model-written text; it must reach gh verbatim after the real shell, never run as shell.
    t.write_text("status: open\n---\n# `tools/x.py`: $(echo pwned) and `agree`\n")
    f.cfg["pull_requests"] = True
    (Path(d) / "bin").mkdir()
    (Path(d) / "bin/gh").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\necho http://pr\n')
    (Path(d) / "bin/gh").chmod(0o755)
    f.env.update(PATH=f"{d}/bin:{os.environ['PATH']}", GH_ARGS=f"{d}/gh-args")
    f.pr_open(repo, "b", t, "builder")  # the git push to a missing origin fails harmlessly
    words = Path(f"{d}/gh-args").read_text().splitlines()
    check("PR title reaches gh literally", words[words.index("--title") + 1] == "[0001] `tools/x.py`: $(echo pwned) and `agree`")
    # 4c. prune_target drops the workspace's artifacts (lib, test binary, dep-info) and keeps crates.io ones,
    # including a crates.io crate that shares its name with a workspace test target (`http`).
    (repo / "crates/rc/src").mkdir(parents=True); (repo / "crates/rc/tests").mkdir()
    (repo / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n')
    (repo / "crates/rc/Cargo.toml").write_text('[package]\nname = "reflex-core"\nversion = "0.1.0"\nedition = "2021"\n')
    (repo / "crates/rc/src/lib.rs").write_text(""); (repo / "crates/rc/tests/http.rs").write_text("")
    tdir = Path(d) / "target/debug/deps"
    tdir.mkdir(parents=True)
    for n in ("libreflex_core-a1.rlib", "reflex_core-a1.d", "http-b2", "http-b2.d", "libhttp-c3.rlib", "libtokio-d4.rmeta"):
        (tdir / n).write_text("x")
    f.env["CARGO_TARGET_DIR"] = str(Path(d) / "target")
    f.prune_target()
    check("prune_target drops workspace artifacts, keeps crates.io", sorted(p.name for p in tdir.iterdir()) == ["libhttp-c3.rlib", "libtokio-d4.rmeta"])

# 4d. Landing survives main moving while the post-rebase gate runs: the first gate call pushes a conflicting
# commit to origin/main (another lane); land() must re-sync, let the builder resolve, and merge, not lose the attempt.
with tempfile.TemporaryDirectory() as d:
    origin, repo, other = Path(d) / "origin.git", Path(d) / "repo", Path(d) / "other"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d)); git(f"clone -q {origin} {other}", Path(d))
    (repo / "tickets").mkdir(); (repo / "PROGRESS.md").write_text("base\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg); f.pr = ""
    wt = Path(d) / "wt"
    git(f"worktree add -q -b land {wt} main", repo)
    (wt / "PROGRESS.md").write_text("branch\n"); git("add -A", wt); git("commit -qm branch", wt)
    moved: list[int] = []
    def gate(cwd):
        if not moved:  # another lane lands on main while this gate runs
            moved.append(1); git("pull -q origin main", other); (other / "PROGRESS.md").write_text("main\n")
            git("add -A", other); git("commit -qm other", other); git("push -q origin main", other)
        return True, ""
    def agent(role, prompt, cwd, timeout_min=0):  # the builder's conflict resolution
        git("merge origin/main", cwd); (cwd / "PROGRESS.md").write_text("both\n"); git("add -A", cwd); git("commit -qm merge", cwd)
        return 0, ""
    f.gate, f.agent = gate, agent
    why = f.land(wt, "land", "0001-x", "builder")
    check("landing survives main moving during the gate", why == "" and git("show origin/main:PROGRESS.md", repo) == "both\n", why or "not landed")

    # 4f. pr_merge waits for GitHub to finish recomputing mergeability after the push, then merges through gh.
    bindir = Path(d) / "bin"; bindir.mkdir()
    (bindir / "gh").write_text('#!/bin/sh\necho "$@" >> "$0.log"\ncase "$1 $2" in "pr view") n=$(grep -c "pr view" "$0.log"); [ "$n" -ge 3 ] && echo MERGEABLE || echo UNKNOWN;; esac\n')
    (bindir / "gh").chmod(0o755)
    f.env["PATH"] = f"{bindir}:{f.env.get('PATH', os.environ['PATH'])}"
    f.sync_main = lambda: None; forge.time.sleep = lambda s: None
    merged = f.pr_merge("https://github.com/x/y/pull/1", "land")
    calls = (bindir / "gh.log").read_text().splitlines()
    check("pr_merge polls mergeability, then merges once", merged and calls.count("pr view https://github.com/x/y/pull/1 --json mergeable --jq .mergeable") == 3 and calls[-1].startswith("pr merge https://github.com/x/y/pull/1"), "\n".join(calls))

    # 4g. A prompt longer than Linux's 128 KiB per-argument cap still starts the agent (it goes through a file).
    (bindir / "omp").write_text('#!/bin/sh\nfor a; do case "$a" in @*) cat "${a#@}";; esac; done\n')
    (bindir / "omp").chmod(0o755)
    forge.OMP = str(bindir / "omp")
    big = "x" * 200_000 + "\nEND-OF-PROMPT"
    rc, text = f._agent_once("reviewer", "c/d", "high", big, repo, 1)
    check("a 200,000-char prompt reaches the agent", rc == 0 and text.rstrip().endswith("END-OF-PROMPT"), text[-200:])

    # 4h. The steward's SCORE.md edit is committed (its prompt tells it to keep SCORE.md true); code it touches is still discarded.
    # Every path in the steward's `git add` must exist: a missing pathspec makes git add stage nothing.
    git("checkout -q main", repo); git("pull -q origin main", repo)
    (repo / "docs/assets").mkdir(parents=True); (repo / "docs/assets/.keep").write_text("")
    for n in ("SCORE.md", "ROADMAP.md", "LEARNINGS.md", "README.md"):
        (repo / n).write_text("S1: ?\n")
    (repo / "main.rs").write_text("fn main() {}\n")
    git("add -A", repo); git("commit -qm score", repo); git("push -q origin main", repo)
    def steward_agent(role, prompt, cwd, timeout_min=0):
        (cwd / "SCORE.md").write_text("S1: 0.42 (3 seeds, `cargo run --release -- bench`, 2026-10-24)\n")
        (cwd / "main.rs").write_text("fn main() { panic!() }\n")
        return 0, ""
    f.agent = steward_agent
    f.steward()
    check("steward commits SCORE.md, drops code", "0.42" in git("show origin/main:SCORE.md", repo) and "panic" not in git("show origin/main:main.rs", repo) and not git("status --porcelain", repo))

    # 4i. A failure with a PR logs its cause, not the PR url (33 of 36 reflex1 RETRY/BLOCKED lines said only "PR: https://...").
    (repo / "tickets").mkdir(exist_ok=True); t = repo / "tickets/0009-z.md"; t.write_text("status: in_progress\n---\n# z\n")
    git("add -A", repo); git("commit -qm z", repo); git("push -q origin main", repo)
    f.pr, f.pr_close = "https://example.invalid/pull/1", lambda pr, why: None
    f.fail(t, "diff edits protected gate/ticket paths ['tickets/0002-y.md']; not allowed.")
    logged = f.log_file.read_text().splitlines()[-1]
    check("fail() logs the cause when there is a PR", "RETRY 0009-z: diff edits protected" in logged and "https://" not in logged, logged)
    check("ticket note still carries the PR url", "PR: https://example.invalid/pull/1\ndiff edits" in git("show origin/main:tickets/0009-z.md", repo))

    # 4j. A project that does not have every path the steward stages still gets its turn committed:
    # `git add -A a b c` stages nothing when one path is missing, so discard_strays threw the turn away.
    (repo / "SCORE.md").unlink(); (repo / "LEARNINGS.md").unlink()
    (repo / "docs/assets/.keep").unlink(); (repo / "docs/assets").rmdir(); (repo / "docs").rmdir()
    (repo / "PROGRESS.md").write_text("p\n")
    git("add -A", repo); git("commit -qm 'project without SCORE.md, LEARNINGS.md, docs'", repo); git("push -q origin main", repo)
    def sparse_agent(role, prompt, cwd, timeout_min=0):
        (cwd / "PROGRESS.md").write_text("p\nsteward was here\n")
        return 0, ""
    f.agent = sparse_agent
    f.steward()
    check("steward turn survives a project missing one of its staged paths", "steward was here" in git("show origin/main:PROGRESS.md", repo))

    # 4k. Two sessions of the same role in one cycle (gate repair, the builder resolving a merge, a rerun
    # review, the steward at the deadline) each keep their log and prompt; the second overwrote the first.
    f.state["cycle"] = 7
    f._agent_once("builder", "c/d", "high", "first session", repo, 1)
    f._agent_once("builder", "c/d", "high", "second session", repo, 1)
    kept = sorted(p.name for p in (f.home / "sessions").glob("00007-builder*"))
    check("a second session of a role keeps the first session's log",
          kept == ["00007-builder-2.log", "00007-builder-2.prompt", "00007-builder.log", "00007-builder.prompt"]
          and "first session" in (f.home / "sessions/00007-builder.log").read_text()
          and "second session" in (f.home / "sessions/00007-builder-2.log").read_text(), str(kept))

    # 4l. The closing steward turn is skipped when the scheduled one already ran on this cycle
    # (10-24: lane 1 ran one at 18:31:06Z on cycle 32 and another at 18:38:36Z at the deadline).
    turns = []
    f.recover, f.steward = (lambda: None), (lambda: turns.append(f.state["cycle"]))
    f.past_deadline, f.lane = (lambda: True), 1
    f.state["cycle"] = 32; f.state["steward_cycle"] = 32
    f.lane_loop()
    check("no second steward turn at the deadline when one ran on this cycle", turns == [], str(turns))
    f.state["steward_cycle"] = 31
    f.lane_loop()
    check("the deadline still gets a steward turn when the last one is older", turns == [32], str(turns))

# 4m. A branch whose rebase conflicts but whose merge is clean (the builder wrote a line and rewrote it
# later, another lane changed the same line on main) is brought up to date by git, not by a 30-min session.
with tempfile.TemporaryDirectory() as d:
    origin, repo, wt = Path(d) / "origin.git", Path(d) / "repo", Path(d) / "wt"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "PROGRESS.md").write_text("base\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    git(f"worktree add -q -b feat {wt} main", repo)
    (wt / "PROGRESS.md").write_text("base\nscratch\n"); git("commit -qam scratch", wt)
    (wt / "PROGRESS.md").write_text("base\n"); git("commit -qam 'drop scratch'", wt)
    (wt / "src.rs").write_text("fn main() {}\n"); git("add -A", wt); git("commit -qm work", wt)
    (repo / "PROGRESS.md").write_text("base\nanother lane\n")
    git("commit -qam other", repo); git("push -q origin main", repo)
    sessions = []
    f.agent = lambda role, prompt, cwd, timeout_min=0: (sessions.append(role), (0, ""))[1]
    ok = f.rebase_onto_main(wt, "builder")
    check("a conflicting rebase that merges cleanly costs no builder session",
          ok and not sessions and (wt / "PROGRESS.md").read_text() == "base\nanother lane\n"
          and (wt / "src.rs").exists(), f"ok={ok} sessions={sessions}")

# 4o. When the PR merge falls back to the local merge, the remote branch goes with it - but only when the
# merge commit actually reached origin/main.
with tempfile.TemporaryDirectory() as d:
    origin, repo, wt = Path(d) / "origin.git", Path(d) / "repo", Path(d) / "wt"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "PROGRESS.md").write_text("base\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg); f.pr = ""
    f.gate = lambda cwd: (True, "")
    git(f"worktree add -q -b landed {wt} main", repo)
    (wt / "src.rs").write_text("fn main() {}\n"); git("add -A", wt); git("commit -qm work", wt)
    git("push -q origin landed", wt)
    why = f.land(wt, "landed", "0001-x", "builder")
    check("a branch landed by the local merge is deleted from the remote",
          why == "" and "landed" not in git("ls-remote --heads origin", repo), why or git("ls-remote --heads origin", repo))

    git(f"worktree add -q -b unpushed {Path(d) / 'wt2'} main", repo)
    (Path(d) / "wt2" / "other.rs").write_text("fn other() {}\n")
    git("add -A", Path(d) / "wt2"); git("commit -qm work2", Path(d) / "wt2")
    git("push -q origin unpushed", Path(d) / "wt2")
    f.push = lambda: None  # the merge commit never reaches origin
    why = f.land(Path(d) / "wt2", "unpushed", "0002-y", "builder")
    check("a branch whose merge did not reach the remote is kept",
          why == "" and "unpushed" in git("ls-remote --heads origin", repo), why or git("ls-remote --heads origin", repo))

# 4p. A cycle that dies outside fail() (reflex1: './gate.sh' timed out, twice) must close its PR with the
# error on it; the ticket goes back to retry through recover() and the next attempt opens a new PR.
with tempfile.TemporaryDirectory() as d:
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "tickets").mkdir()
    (repo / "tickets/0003-t.md").write_text("status: open\n\n# t\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    calls = []
    f.plan, f.board = (lambda: None), (lambda doing: None)
    f.claim = lambda: repo / "tickets/0003-t.md"
    f.pr_open = lambda wt, branch, ticket, builder: "https://example.invalid/pull/12"
    f.gh = lambda args, cwd=None, body=None: (calls.append((args, body or "")), "OPEN" if args.startswith("pr view") else "")[1]
    f.agent = lambda role, prompt, cwd, timeout_min=None: ((cwd / "src.rs").write_text("fn main() {}\n"), (0, ""))[1]
    def gate_times_out(cwd):
        raise subprocess.TimeoutExpired("./gate.sh", 1800)
    f.gate = gate_times_out
    try:
        f.cycle()
    except subprocess.TimeoutExpired:
        pass
    check("a cycle that dies with an exception closes its PR with the error",
          any(a.startswith("pr close") for a, _ in calls)
          and any("TimeoutExpired" in b for _, b in calls), str(calls)[:300])

# 4q. A lane that runs dry refills the ticket queue itself. The planner used to run only on lane 1, at the
# top of its own cycle, so lanes 2-4 slept 2 min at a time until lane 1 finished a build (44 "no open ticket
# to claim" lines in reflex1's log; lane 4 idled 19:26-19:44 on 2026-10-23). Still one planner turn at a time.
with tempfile.TemporaryDirectory() as d:
    import fcntl
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "tickets").mkdir()
    git("commit -qm base --allow-empty", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\nlanes=4\nmin_queue=3\npull_requests=false\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n[roles.planner]\nmodel="e/f"\n')
    sleeps, real_sleep = [], forge.time.sleep
    forge.time.sleep = lambda s: sleeps.append(s)
    turns: list[str] = []
    f = forge.Forge(cfg, 2)  # lane 2 gets its own clone
    f.agent = lambda role, prompt, cwd, timeout_min=None: (turns.append(role), (0, ""))[1]
    f.cycle()
    check("a lane with nothing to claim refills the queue itself", turns == ["planner"], str(turns))

    held = (Path(d) / "s" / "plan.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another lane is mid planner turn
    turns.clear()
    f3 = forge.Forge(cfg, 3)
    f3.agent = lambda role, prompt, cwd, timeout_min=None: (turns.append(role), (0, ""))[1]
    f3.cycle()
    check("only one lane refills the queue at a time", turns == [], str(turns))
    held.close()
    forge.time.sleep = real_sleep

# 4r. A PR GitHub reports as CONFLICTING cannot be merged by `gh pr merge`; the local merge decides, so polling only delays it:
# reflex1 paid ten `pr view` calls and 30 s per case (17 "Pull Request has merge conflicts" lines) before
# firing the doomed merge. It must go straight to the local landing path instead.
calls: list[str] = []
with tempfile.TemporaryDirectory() as d:
    cfg = Path(d) / "c.toml"
    (Path(d) / "repo").mkdir()
    git("init -q -b main", Path(d) / "repo")
    cfg.write_text(f'name="t"\nrepo="{d}/repo"\nstate_dir="{d}/s"\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    real_sleep = forge.time.sleep
    forge.time.sleep = lambda s: None
    f.gh = lambda args, cwd=None, body=None: (calls.append(args), "CONFLICTING")[1]
    f.sh = lambda cmd, cwd=None, timeout=600, check=False: (calls.append(cmd),
                                                            subprocess.CompletedProcess(cmd, 1, "", "conflicts"))[1]
    merged = f.pr_merge("https://example.invalid/pull/7", "forge/l1-b")
    forge.time.sleep = real_sleep
check("a PR GitHub calls CONFLICTING is landed locally, not polled and merged",
      merged is False and len(calls) == 1 and not any("pr merge" in c for c in calls), str(calls)[:300])

# 4s. The gate and STEERING.md stay protected whatever a project config lists. reflex1.toml overrides
# protected_paths without STEERING.md, so the owner's standing instructions - prepended to every session
# by _agent_once - were editable by a builder diff; a rule in the prompt is not the same control (arXiv
# 2610.06274: a protected config value was written on 25 of 72 ordinary requests with no runtime floor,
# on 0 of 167 with one).
with tempfile.TemporaryDirectory() as d:
    repo = Path(d) / "repo"
    (repo / "ci").mkdir(parents=True)
    (repo / "ci/check.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "STEERING.md").write_text("owner: never submit to the leaderboard\n")
    git("init -q -b main", repo); git("add -A", repo); git("commit -qm i", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\ngate="./ci/check.sh"\nprotected_paths=["tickets/"]\n'
                   f'pull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    git("checkout -q -b work main", repo)
    (repo / "STEERING.md").write_text("owner: submit to the leaderboard\n")
    (repo / "ci/check.sh").write_text("#!/bin/sh\nexit 0\n# always passes now\n")
    git("add -A", repo); git("commit -qm t", repo)
    bad = f.touched_protected(repo)
    check("a config that lists neither still protects STEERING.md and the gate",
          set(bad) == {"STEERING.md", "ci/check.sh"}, str(bad))

# 5. GPU guards: pods carry a deadline in the name, the budget is capped, and reap can kill them.
gsrc = (ROOT / "gpu.py").read_text()
check("gpu pod names carry a deadline epoch", 'f"forge-{a.name}-{deadline}"' in gsrc)
check("gpu has a budget cap", "GPU_BUDGET_USD" in gsrc and "over budget" in gsrc)
check("gpu terminates in a finally block", "finally:" in gsrc and "terminate(pod" in gsrc)
check("gpu reap exists", callable(getattr(gpu, "reap", None)))

# 6. Prompts must keep the rules the loop depends on.
b = (ROOT / "prompts/builder.md").read_text()
r = (ROOT / "prompts/reviewer.md").read_text()
check("builder is told not to edit the gate", "gate.sh" in b and "never" in b.lower())
check("reviewer must end with a verdict line", "VERDICT: APPROVE" in r and "VERDICT: REJECT" in r)
check("reviewer is not told to be lenient", "benefit of the doubt" not in r.lower())

# 7. A sync of main must not destroy this lane's unpushed commits (planner tickets, status, doc edits).
with tempfile.TemporaryDirectory() as d:
    origin = Path(d) / "origin.git"
    subprocess.run(f"git init -q --bare -b main {origin}", shell=True, check=True)
    repo = Path(d) / "repo"
    (repo / "tickets").mkdir(parents=True)
    (repo / "gate.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "PROGRESS.md").write_text("base\n")
    git("init -q -b main", repo); git("add -A", repo); git("commit -qm i", repo)
    git(f"remote add origin {origin}", repo); git("push -q -u origin main", repo)
    other = Path(d) / "other"
    subprocess.run(f"git clone -q {origin} {other}", shell=True, check=True)
    (other / "PROGRESS.md").write_text("upstream\n")
    git("add -A", other); git("commit -qm up", other); git("push -q origin main", other)
    # This lane wrote a line and rewrote it (planner, then steward) and added a ticket: the final tree agrees
    # with the merge base, so a rebase conflicts replaying the first commit while a merge is clean.
    (repo / "PROGRESS.md").write_text("mid\n")
    git("add -A", repo); git("commit -qm local-a", repo)
    (repo / "PROGRESS.md").write_text("base\n")
    (repo / "tickets/0007-new.md").write_text("status: open\n---\n# new ticket\n")
    git("add -A", repo); git("commit -qm local-b", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    f.sync_main()
    check("sync keeps this lane's unpushed ticket", (repo / "tickets/0007-new.md").exists(), git("log --oneline -3", repo))
    check("sync takes the upstream change", "upstream" in (repo / "PROGRESS.md").read_text())
    # A divergence that really conflicts still resets to origin, but the commits must stay reachable.
    (repo / "PROGRESS.md").write_text("ours\n"); git("add -A", repo); git("commit -qm local-c", repo)
    (other / "PROGRESS.md").write_text("theirs\n"); git("add -A", other)
    git("commit -qm up2", other); git("push -q origin main", other)
    f.sync_main()
    check("a conflicting sync keeps the dropped commits on a branch",
          "local-c" in git("log --oneline --all", repo) and "ours" not in (repo / "PROGRESS.md").read_text(),
          git("log --oneline --all", repo))

# 8. A free recovery (restart mid-cycle, not counted as an attempt) must not repeat forever. reflex1's ticket
# 0012 was recovered three times - twice after `./gate.sh timed out after 1800 seconds` - and so spent 6 cycles
# on 3 counted attempts; 0009 was recovered twice and then merged, so the first two stay free.
with tempfile.TemporaryDirectory() as d:
    repo = Path(d) / "repo"
    (repo / "tickets").mkdir(parents=True)
    twice = repo / "tickets/0012-poison.md"
    twice.write_text("status: in_progress lane=1\n---\n# poison\n"
                     "\n\n## forge-recover t1\ninterrupted\n\n\n## forge-recover t2\ninterrupted\n")
    once = repo / "tickets/0009-slow.md"
    once.write_text("status: in_progress lane=1\n---\n# slow\n\n\n## forge-recover t1\ninterrupted\n")
    git("init -q -b main", repo); git("add -A", repo); git("commit -qm i", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\nmax_attempts=3\npull_requests=false\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    f.sync_main = lambda: None  # no remote in this fixture
    f.push = lambda: None
    f.recover()
    check("a ticket interrupted max_attempts times stops instead of retrying free",
          "status: blocked" in twice.read_text(), twice.read_text()[:200])
    check("the first interruptions of a ticket are still free retries",
          "status: retry" in once.read_text() and once.read_text().count("## forge-recover ") == 2,
          once.read_text()[:200])
    quota = repo / "tickets/0013-quota.md"
    quota.write_text("status: in_progress lane=1\n---\n# quota\n"
                     "\n\n## forge-recover t1\ninterrupted\n\n\n## forge-recover t2\ninterrupted\n")
    f.recover(quota=True)
    check("a quota recovery is not counted toward the interruption cap",
          "status: retry" in quota.read_text(), quota.read_text()[:200])

# 9. reviewctl: a PR whose last pass failed the gate must be looked at again once the base branch moves - the
# failure is usually a conflict with base, not the PR. Crewship PR #28 failed the gate on 2026-10-24, merged
# cleanly into `forge` 29 h later, and was never re-checked because the mark is keyed by the head sha alone.
# The re-check runs the cheap gate first: a PR that still conflicts costs no 40-minute audit session.
with tempfile.TemporaryDirectory() as d:
    rc = load("reviewctl_mod", ROOT / "reviewctl.py")
    rc.STATE = Path(d)
    rc.log = lambda m: None
    rc.record = lambda v: None  # the real one writes PR_AUDIT.md in the checkout and pushes it
    rc.sh = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "newbase", "")
    rc.GH_REPO = "selftest/none"  # a call that gets past the sh stub must not reach a real PR
    rc.open_prs = lambda: [{"number": 28, "headRefName": "b", "files": [{"path": "forge/forge.py"}]}]
    old = {"pr": 28, "sha": "deadbeefcafe", "gate": "fail", "base": "oldbase", "verdict": "MERGE",
           "risk": "low", "audit": "a", "at": "t"}
    rc.previous = lambda n: old
    calls: list[str] = []
    rc.audit = lambda pr: (calls.append("audit"), dict(old))[1]
    rc.mark = lambda n, v: calls.append(f"mark base={v.get('base')}")
    rc.gate = lambda pr: (calls.append("gate"), (False, "conflicts with base"))[1]
    rc.run()
    check("a PR that still fails the gate on the new base is re-gated, not re-audited",
          calls == ["gate", "mark base=newbase"], str(calls))
    calls.clear()
    rc.gate = lambda pr: (calls.append("gate"), (True, "ok"))[1]
    rc.run()
    check("a PR that merges cleanly after the base moved gets its audit", "audit" in calls, str(calls))
    check("a decided PR is not re-run while its branch stands still",
          not rc.needs_pass({"gate": "pass", "base": "oldbase"}, "newbase")
          and not rc.needs_pass({"gate": "fail", "base": "newbase"}, "newbase")
          and rc.needs_pass(None, "newbase"))

# 10. A session takes its vendor out of every role's chain only on the provider's own refusal, which arrives on
# stderr ("Codex error event: The usage limit has been reached"). An agent's report quotes those words when the
# work is about quota - reflex1 lane 2 cycle 28's builder and reviewer quoted the steward's `usage_limit_reached`
# bullet in LEARNINGS.md; 5 of 264 sessions match the pattern in stdout and none of them was a provider error.
with tempfile.TemporaryDirectory() as d:
    repo = Path(d) / "repo"
    repo.mkdir(parents=True)
    git("init -q -b main", repo)
    (repo / "PROGRESS.md").write_text("x\n")
    git("add -A", repo); git("commit -qm i", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n'
                   f'[roles.builder]\nmodel="a/b"\nfallback=["c/d"]\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    f.log = lambda m: None
    ran: list[str] = []
    prose = ("I added the retry and the `usage_limit_reached` bullet to LEARNINGS.md, then the gate timed out."
             "\n[stderr]\nWorking...\n")
    refusal = "\n[stderr]\nWorking...\nCodex error event: The usage limit has been reached (code=usage_limit_reached)\n"
    outs = {"a/b": (124, prose), "c/d": (0, "done\n[stderr]\n")}

    def once(role, model, thinking, prompt, cwd, timeout_min):
        ran.append(model)
        return outs[model]

    f._agent_once = once
    rc, text = f.agent("builder", "p", repo)
    check("a failed session that writes about quota keeps its vendor",
          (rc, ran, f.exhausted, f.last_model["builder"]) == (124, ["a/b"], {}, "a/b"), f"{ran} {f.exhausted}")
    ran.clear()
    outs["a/b"] = (1, refusal)
    rc, text = f.agent("builder", "p", repo)
    check("the provider's own refusal on stderr falls back to the other vendor",
          ran == ["a/b", "c/d"] and "a" in f.exhausted and f.last_model["builder"] == "c/d", f"{ran} {f.exhausted}")

# 11. A cycle that does not land must take its attempt branch off the remote; a branch already merged into
# local main must stay (land() keeps it as the only copy when the merge commit did not reach origin/main).
# 42 of reflex1's 106 remote forge/ refs are not in origin/main: only the merge paths ever deleted a branch.
with tempfile.TemporaryDirectory() as d:
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "tickets").mkdir()
    (repo / "tickets/0004-r.md").write_text("status: open\n\n# r\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    f.plan, f.board, f.gh = (lambda: None), (lambda doing: None), (lambda args, cwd=None, body=None: "")
    f.claim = lambda: repo / "tickets/0004-r.md"
    f.gate = lambda cwd: (True, "")
    f.pr_open = lambda wt, branch, ticket, builder: (git(f"push -q -u origin {branch}", wt), "https://example.invalid/pull/9")[1]
    verdict = ["VERDICT: REJECT"]
    def build_or_review(role, prompt, cwd, timeout_min=None):
        (cwd / "src.rs").write_text(f"fn main() {{}} // {role}\n")
        return 0, verdict[0]
    f.agent = build_or_review
    f.cycle()
    check("a rejected attempt's branch is deleted from the remote",
          "forge/l1-" not in git("ls-remote --heads origin", repo), git("ls-remote --heads origin", repo))

    verdict[0] = "VERDICT: APPROVE"
    f.land = lambda wt, branch, tid, builder: (git(f"merge -q --no-ff -m land {branch}", repo), "")[1]
    f.cycle()
    check("a branch merged into main is left for land() to decide",
          "forge/l1-" in git("ls-remote --heads origin", repo), git("ls-remote --heads origin", repo))

# 12. Losing the claim race must cost this lane its status commit only. The reset was to origin/main, so a
# planner turn's tickets or a steward doc edit that had not been pushed yet went with it - silently, with no
# log line (claim() races three other lanes on every cycle; 124 claims landed in reflex1).
with tempfile.TemporaryDirectory() as d:
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "tickets").mkdir()
    (repo / "tickets/0004-r.md").write_text("status: open\n\n# r\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    # Another lane pushed first and this lane never fetches it: every push it makes is a non-fast-forward.
    other = Path(d) / "other"
    git(f"clone -q {origin} {other}", Path(d))
    (other / "PROGRESS.md").write_text("another lane\n")
    git("add -A", other); git("commit -qm other", other); git("push -q origin main", other)
    # Unpushed work on main, as after a planner turn whose push did not get through.
    (repo / "tickets/0005-planned.md").write_text("status: open\n\n# planned by this lane\n")
    git("add -A", repo); git("commit -qm 'forge: planner refills the ticket queue'", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\n[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    f.sync_main = lambda: None
    real_sleep = forge.time.sleep
    forge.time.sleep = lambda s: None
    got = f.claim()
    forge.time.sleep = real_sleep
    check("a lost claim race keeps this lane's unpushed main work",
          got is None and (repo / "tickets/0005-planned.md").exists(), git("log --oneline -3", repo))
    check("a lost claim race undoes its own status change",
          "status: open" in (repo / "tickets/0004-r.md").read_text()
          and "starts" not in git("log --oneline -3", repo), git("log --oneline -3", repo))

# 13. A cycle must not claim new work once the deadline has passed. lane_loop checks it before the cycle,
# but sync_main() and a planner turn (timeout_min 40) run inside it: reflex1 lane 1 claimed 0128 at
# 17:05:32Z against a 17:00:00Z deadline and spent 86 more minutes on it, and the supervisor's "all lanes
# finished at the deadline" came at 18:43:52Z.
with tempfile.TemporaryDirectory() as d:
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "tickets").mkdir()
    (repo / "tickets/0001-t.md").write_text("status: open\n\n# t\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\nmin_queue=1\npull_requests=false\n'
                   f'[limits]\ndeadline="2000-01-01T00:00:00Z"\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    turns: list[str] = []
    f = forge.Forge(cfg)
    f.agent = lambda role, prompt, cwd, timeout_min=None: (turns.append(role), (0, ""))[1]
    f.cycle()
    check("a cycle past the deadline claims nothing",
          turns == [] and "status: open" in (repo / "tickets/0001-t.md").read_text(), str(turns))

# 14. A lane must not start a build it has no room for: out of space the gate fails like wrong code, and the
# repair session, the attempt and the retry are all spent on ENOSPC. The loop's own prune runs first - this
# box is at 92% (17 GB free) with 106 GB in the four lanes' cargo target dirs, 25.2 GB of it prunable.
with tempfile.TemporaryDirectory() as d:
    repo = Path(d) / "repo"
    repo.mkdir(parents=True)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\n[limits]\nmin_free_gb=0\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    logged: list[str] = []
    pruned: list[int] = []
    f.log = logged.append
    f.prune_target = lambda: pruned.append(1)
    check("a lane with room on disk starts its cycle without pruning", f.disk_ok() and not pruned, str(logged))
    f.cfg["limits"]["min_free_gb"] = 10 ** 9  # no filesystem has an exabyte free
    check("a lane with no room prunes, then waits instead of building",
          not f.disk_ok() and pruned == [1] and "floor" in logged[-1], f"{pruned} {logged}")

# 15. The prune disk_ok() leans on must say when it cannot run. `cargo metadata` failing (no Cargo.toml in
# the configured repo, a broken toolchain) left `packages: []`, so nothing matched and nothing was deleted,
# with no line in the log: the lane then sleeps 15 min at a time behind "not starting a build" while its own
# dead test binaries hold the space.
with tempfile.TemporaryDirectory() as d:
    repo, tdir = Path(d) / "repo", Path(d) / "target"
    repo.mkdir(parents=True)
    (tdir / "debug/deps").mkdir(parents=True)
    artifact = tdir / "debug/deps/libreflex_core-dead0001.rlib"
    artifact.write_text("x")
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\n[env]\nCARGO_TARGET_DIR="{tdir}"\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)
    logged = []
    f.log = logged.append
    f.prune_target()
    check("a prune that cannot read the workspace says so instead of deleting nothing in silence",
          artifact.exists() and any("cargo metadata failed" in m for m in logged), str(logged))

# 16. The builder session that resolves a conflict with main (29 reflex1 cycles ran one) commits through
# `git add -A`, and it starts after the pre-gate protected check and after the review. Until land() checks
# again, the only thing keeping that session off gate.sh and tickets/ was a sentence in its prompt.
with tempfile.TemporaryDirectory() as d:
    origin, repo = Path(d) / "origin.git", Path(d) / "repo"
    git(f"init -q --bare -b main {origin}", Path(d))
    git(f"clone -q {origin} {repo}", Path(d))
    (repo / "gate.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "a.txt").write_text("base\n")
    git("add -A", repo); git("commit -qm base", repo); git("push -q origin main", repo)
    wt = Path(d) / "wt"
    git(f"worktree add -q -b b {wt} main", repo)
    (wt / "a.txt").write_text("branch\n")
    git("add -A", wt); git("commit -qm 'builder work'", wt)
    # Another lane lands a conflicting change, so rebase and merge both stop and the builder is called in.
    (repo / "a.txt").write_text("other lane\n")
    git("add -A", repo); git("commit -qm other", repo); git("push -q origin main", repo)
    cfg = Path(d) / "c.toml"
    cfg.write_text(f'name="t"\nrepo="{repo}"\nstate_dir="{d}/s"\npull_requests=false\n'
                   f'[roles.builder]\nmodel="a/b"\n[roles.reviewer]\nmodel="c/d"\n')
    f = forge.Forge(cfg)

    def resolve(role, prompt, cwd, timeout_min=None):
        git("merge origin/main", cwd)          # conflicts, as it did for the real session
        (cwd / "a.txt").write_text("both\n")   # the work it was asked to do
        (cwd / "gate.sh").write_text("#!/bin/sh\nexit 0  # and the oracle it was told not to touch\n")
        git("add -A", cwd); git("commit -qm 'forge: merge main'", cwd)
        return 0, ""

    f.agent = resolve
    f.pr = ""
    f.gate = lambda cwd: (True, "")  # the gate is not the check under test; it also cannot check itself
    why = f.land(wt, "b", "0001-t", "builder")
    check("a conflict resolved by editing the gate does not land",
          "gate.sh" in why and "not to touch" not in git("show origin/main:gate.sh", repo), f"{why!r}")

print()
if fails:
    print(f"{len(fails)} check(s) failed: {', '.join(fails)}")
    sys.exit(1)
print("forge selftest: all checks passed")
