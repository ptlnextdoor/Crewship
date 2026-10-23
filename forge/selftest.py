#!/usr/bin/env python3
"""forge selftest: the checks a change to forge itself must pass. No network, no models.

Run from a checkout root: `python3 forge/selftest.py`. Exit 0 = safe to land.
Each check is a fact about the loop that a careless edit could break.
"""
from __future__ import annotations

import importlib.util
import json
import os
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
