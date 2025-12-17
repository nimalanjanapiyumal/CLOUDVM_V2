#!/usr/bin/env python3
"""Runbook - VM2 (Data Plane VM)

What this single script does:
  1) Installs Mininet + OVS + iPerf3 (Ubuntu 22.04)
  2) Extracts the VM2 dataplane project from zip
  3) Auto-discovers the Controller VM (or uses --controller-ip)
  4) Starts Mininet topology + demo backend servers + optional traffic generation

Target OS: Ubuntu 22.04
NOTE: Mininet requires root. Run with:
  sudo -E python3 runbook_vm2_dataplane.py
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple


REPO_DIR = Path(__file__).resolve().parent
ZIP_NAME_DEFAULT = "vm2_dataplane_project.zip"

DISCOVER_PATH = "/discover"
DISCOVER_PORT = 8080  # Ryu REST port default (can be overridden with --rest-port)


def run(cmd: list[str], *, check: bool = True, cwd: Optional[Path] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    print(f"[runbook:vm2] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=check, cwd=str(cwd) if cwd else None, env=env)


def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    print("[runbook:vm2] This runbook must run as root (Mininet). Re-launching with sudo...", flush=True)
    cmd = ["sudo", "-E", sys.executable] + sys.argv
    os.execvp("sudo", cmd)


def ensure_apt(pkgs: list[str]) -> None:
    if shutil.which("apt-get") is None:
        print("[runbook:vm2] ERROR: apt-get not found (expected Ubuntu/Debian).", file=sys.stderr)
        raise SystemExit(2)
    run(["apt-get", "update"])
    run(["apt-get", "install", "-y"] + pkgs)


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    if dest_dir.exists() and any(dest_dir.iterdir()):
        print(f"[runbook:vm2] Using existing extracted dir: {dest_dir}", flush=True)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"[runbook:vm2] Extracting {zip_path} -> {dest_dir}", flush=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def get_primary_ip_and_cidr() -> Tuple[str, str]:
    """Return (ip, cidr_str) for the primary interface, best-effort."""
    # Best effort: infer /24 if netmask isn't easy.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()

    # Assume /24 for OpenStack private networks by default.
    cidr = f"{ip}/24"
    return ip, cidr


def try_discover(ip: str, rest_port: int, timeout: float = 0.25) -> Optional[dict]:
    url = f"http://{ip}:{rest_port}{DISCOVER_PATH}"
    try:
        req = urllib.request.Request(url, headers={"Connection": "close", "User-Agent": "hybrid-lb-discovery"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def discover_controller(rest_port: int, cidr: str) -> Tuple[str, dict]:
    net = ipaddress.ip_network(cidr, strict=False)
    print(f"[runbook:vm2] Auto-discovery: scanning {net} for controller REST on port {rest_port} ...", flush=True)

    # Simple linear scan. For /24 this is fast enough.
    for host in net.hosts():
        ip = str(host)
        payload = try_discover(ip, rest_port=rest_port, timeout=0.2)
        if payload and "controller" in payload and "vip" in payload:
            print(f"[runbook:vm2] Found controller at {ip}:{rest_port}", flush=True)
            return ip, payload

    raise RuntimeError(f"Controller not found in {cidr}. Provide --controller-ip or set CONTROLLER_IP env var.")


def main() -> int:
    ensure_root()

    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=str(REPO_DIR / ZIP_NAME_DEFAULT), help="Path to VM2 zip")
    ap.add_argument("--deploy-dir", default=str(REPO_DIR / ".deploy" / "vm2_dataplane"), help="Extraction dir")
    ap.add_argument("--controller-ip", default=os.environ.get("CONTROLLER_IP", ""), help="Controller VM IP (optional)")
    ap.add_argument("--controller-port", type=int, default=6653, help="Controller OpenFlow TCP port")
    ap.add_argument("--rest-port", type=int, default=DISCOVER_PORT, help="Controller REST port for discovery")
    ap.add_argument("--http-port", type=int, default=8080, help="Backend HTTP port")
    ap.add_argument("--vip", default="", help="VIP IP (optional; if empty, auto from discovery)")
    ap.add_argument("--start-load", action="store_true", help="Start HTTP benchmark from h1 automatically")
    ap.add_argument("--duration", type=int, default=60, help="Load duration seconds")
    ap.add_argument("--concurrency", type=int, default=20, help="Load concurrency")
    ap.add_argument("--cli", action="store_true", help="Drop into Mininet CLI after setup")
    args = ap.parse_args()

    zip_path = Path(args.zip).expanduser().resolve()
    if not zip_path.exists():
        print(f"[runbook:vm2] ERROR: zip not found: {zip_path}", file=sys.stderr)
        return 2

    # OS dependencies (Mininet + OVS + tools)
    ensure_apt([
        "mininet",
        "openvswitch-switch",
        "iperf3",
        "python3",
        "python3-pip",
    ])

    # Ensure OVS is running
    run(["systemctl", "enable", "--now", "openvswitch-switch"], check=False)

    deploy_dir = Path(args.deploy_dir).expanduser().resolve()
    deploy_dir.parent.mkdir(parents=True, exist_ok=True)
    extract_zip(zip_path, deploy_dir)

    # Controller IP discovery
    controller_ip = args.controller_ip.strip()
    vip_ip = args.vip.strip()

    payload = None
    if controller_ip:
        payload = try_discover(controller_ip, rest_port=args.rest_port, timeout=0.5)
        if not payload:
            print(f"[runbook:vm2] WARNING: could not reach controller REST at {controller_ip}:{args.rest_port}; continuing.", flush=True)
    else:
        my_ip, cidr = get_primary_ip_and_cidr()
        try:
            controller_ip, payload = discover_controller(rest_port=args.rest_port, cidr=cidr)
        except Exception as e:
            print(f"[runbook:vm2] ERROR: {e}", file=sys.stderr)
            return 3

    if payload and not vip_ip:
        vip_ip = payload.get("vip", {}).get("ip", "") or "10.0.0.100"

    controller_port = args.controller_port
    if payload:
        controller_port = int(payload.get("controller", {}).get("of_listen_port", controller_port))

    if not vip_ip:
        vip_ip = "10.0.0.100"

    print("\n[runbook:vm2] Using controller:")
    print(f"  OpenFlow: {controller_ip}:{controller_port}")
    print(f"  REST:     http://{controller_ip}:{args.rest_port}{DISCOVER_PATH}")
    print(f"  VIP:      {vip_ip}")
    print("", flush=True)

    # Launch Mininet stack
    stack = deploy_dir / "topology" / "mininet_stack.py"
    cmd = [
        "python3",
        str(stack),
        "--controller-ip", controller_ip,
        "--controller-port", str(controller_port),
        "--vip", vip_ip,
        "--http-port", str(args.http_port),
    ]
    if args.start_load:
        cmd += ["--start-load", "--duration", str(args.duration), "--concurrency", str(args.concurrency)]
    if args.cli:
        cmd += ["--cli"]

    return run(cmd, check=False, cwd=deploy_dir).returncode


if __name__ == "__main__":
    raise SystemExit(main())
