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

print()
if fails:
    print(f"{len(fails)} check(s) failed: {', '.join(fails)}")
    sys.exit(1)
print("forge selftest: all checks passed")
