#!/usr/bin/env python3
"""gpu: rent a RunPod GPU for one job, run it over SSH, copy results back, always terminate.

    gpu.py run --name train-vit --gpu "NVIDIA GeForce RTX 4090" --hours 1.5 \
        --push ./bench --pull runs/out --cmd "cd /workspace/bench && python train.py"
    gpu.py list            # pods and their spend
    gpu.py reap [--all]    # terminate pods past their deadline (or all forge pods)
    gpu.py balance

Guards (agents call this unattended, so the guards are the point):
- Every pod is named forge-<name>-<deadline epoch>; reap kills any forge pod past its deadline,
  and the forge service runs reap on a timer, so a crashed job cannot leave a pod running.
- --hours is capped by GPU_MAX_HOURS (default 3) and the job's estimated cost by GPU_BUDGET_USD
  minus what forge pods already spent (ledger in ~/.local/state/forge/gpu-ledger.jsonl).
- Refuses when the account balance is below GPU_MIN_BALANCE (default 2 USD).
Key: RUNPOD_API_KEY from ~/.config/runpod/env. SSH key: ~/.ssh/id_ed25519.pub (sent as PUBLIC_KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "https://rest.runpod.io/v1"
# RunPod's edge rejects Python's default urllib User-Agent with 403; send our own.
HEADERS = {"Content-Type": "application/json", "User-Agent": "crewship-forge/1.0"}
LEDGER = Path.home() / ".local/state/forge/gpu-ledger.jsonl"
IMAGE = os.environ.get("GPU_IMAGE", "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04")


def key() -> str:
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        env = Path.home() / ".config/runpod/env"
        for line in env.read_text().splitlines():
            if line.startswith("RUNPOD_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"')
    if not k:
        sys.exit("gpu: RUNPOD_API_KEY missing")
    return k


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {key()}", **HEADERS})
    with urllib.request.urlopen(req, timeout=60) as r:
        t = r.read()
        return json.loads(t) if t else None


def balance() -> float:
    q = {"query": "{ myself { clientBalance currentSpendPerHr } }"}
    req = urllib.request.Request("https://api.runpod.io/graphql", data=json.dumps(q).encode(),
                                 headers={"Authorization": f"Bearer {key()}", **HEADERS})
    with urllib.request.urlopen(req, timeout=60) as r:
        return float(json.loads(r.read())["data"]["myself"]["clientBalance"])


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(l).get("usd", 0) for l in LEDGER.read_text().splitlines() if l.strip())


def record(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def forge_pods() -> list[dict]:
    return [p for p in (call("GET", "/pods") or []) if str(p.get("name", "")).startswith("forge-")]


def terminate(pod: dict, why: str) -> None:
    started = pod.get("_started") or time.time()
    call("DELETE", f"/pods/{pod['id']}")
    hrs = max(0.0, (time.time() - started) / 3600)
    usd = round(hrs * float(pod.get("costPerHr") or 0), 3)
    record({"at": time.time(), "pod": pod["id"], "name": pod.get("name"), "hours": round(hrs, 3), "usd": usd, "why": why})
    print(f"gpu: terminated {pod['id']} ({why}), ~${usd}")


def reap(all_pods: bool) -> None:
    for p in forge_pods():
        try:
            deadline = int(p["name"].rsplit("-", 1)[1])
        except (ValueError, IndexError):
            deadline = 0
        if all_pods or time.time() > deadline:
            terminate(p, "reap")


def ssh_target(pod_id: str, wait_s: int = 900) -> tuple[str, int]:
    t0 = time.time()
    while time.time() - t0 < wait_s:
        p = call("GET", f"/pods/{pod_id}")
        ip, ports = p.get("publicIp"), p.get("portMappings") or {}
        if ip and ports.get("22"):
            return ip, int(ports["22"])
        time.sleep(10)
    raise TimeoutError("pod never exposed SSH")


def ssh(ip: str, port: int, cmd: str, timeout: int) -> int:
    base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ServerAliveInterval=30",
            "-p", str(port), f"root@{ip}"]
    return subprocess.call(base + [cmd], timeout=timeout)


def rsync(src: str, dst: str, port: int) -> int:
    e = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p {port}"
    return subprocess.call(["rsync", "-az", "-e", e, src, dst])


def run(a) -> int:
    max_h = float(os.environ.get("GPU_MAX_HOURS", 3))
    budget = float(os.environ.get("GPU_BUDGET_USD", 15))
    hours = min(a.hours, max_h)
    bal = balance()
    if bal < float(os.environ.get("GPU_MIN_BALANCE", 2)):
        sys.exit(f"gpu: balance ${bal:.2f} too low, refusing")
    left = budget - spent()
    deadline = int(time.time() + hours * 3600)
    body = {"name": f"forge-{a.name}-{deadline}", "imageName": IMAGE, "gpuTypeIds": a.gpu, "gpuCount": 1,
            "gpuTypePriority": "custom", "cloudType": a.cloud, "containerDiskInGb": 40, "volumeInGb": 0,
            "ports": ["22/tcp"], "supportPublicIp": True, "interruptible": a.spot,
            "env": {"PUBLIC_KEY": (Path.home() / ".ssh/id_ed25519.pub").read_text().strip()}}
    pod = call("POST", "/pods", body)
    pod["_started"] = time.time()
    cost = float(pod.get("costPerHr") or 0)
    print(f"gpu: pod {pod['id']} {pod.get('gpu', {}).get('displayName', a.gpu)} ${cost}/h, deadline {hours}h")
    if cost * hours > left:
        terminate(pod, f"over budget: ${cost * hours:.2f} > ${left:.2f} left of ${budget}")
        return 3
    rc = 1
    try:
        ip, port = ssh_target(pod["id"])
        time.sleep(15)
        for src in a.push or []:
            rsync(src, f"root@{ip}:/workspace/", port)
        rc = ssh(ip, port, a.cmd, timeout=int(hours * 3600) - 120)
        for path in a.pull or []:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            rsync(f"root@{ip}:/workspace/{path}", str(Path(path).parent) + "/", port)
    except Exception as e:
        print(f"gpu: job error {type(e).__name__}: {e}")
    finally:
        terminate(pod, f"job done rc={rc}")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--gpu", action="append", default=None, help="RunPod gpuTypeId, repeatable in priority order")
    r.add_argument("--hours", type=float, default=1.0)
    r.add_argument("--cloud", default="COMMUNITY", choices=["COMMUNITY", "SECURE"])
    r.add_argument("--spot", action="store_true")
    r.add_argument("--push", action="append")
    r.add_argument("--pull", action="append")
    r.add_argument("--cmd", required=True)
    sub.add_parser("list")
    rp = sub.add_parser("reap")
    rp.add_argument("--all", action="store_true")
    sub.add_parser("balance")
    a = ap.parse_args()
    if a.action == "run":
        a.gpu = a.gpu or ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090"]
        sys.exit(run(a))
    if a.action == "list":
        for p in forge_pods():
            print(p["id"], p["name"], p.get("desiredStatus"), p.get("costPerHr"))
        print(f"forge spend so far: ${spent():.2f}")
    elif a.action == "reap":
        reap(a.all)
    elif a.action == "balance":
        print(f"${balance():.2f}, forge spend so far ${spent():.2f}")


if __name__ == "__main__":
    main()
