#!/usr/bin/env python3
"""Runbook - VM1 (Controller VM)

What this single script does:
  1) Extract the VM1 controller project from zip
  2) Create a Python venv
  3) Install dependencies
  4) Start the Ryu controller app (Hybrid RR + GA, flow rules, monitoring)
  5) Optionally start Prometheus+Grafana (Docker compose) for visualization

Target OS: Ubuntu 22.04
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional


REPO_DIR = Path(__file__).resolve().parent
ZIP_NAME_DEFAULT = "vm1_controller_project.zip"

DEFAULT_OFP_PORT = 6653
DEFAULT_REST_PORT = 8080
DEFAULT_METRICS_PORT = 9100


def run(cmd: list[str], *, check: bool = True, cwd: Optional[Path] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    print(f"[runbook:vm1] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=check, cwd=str(cwd) if cwd else None, env=env)


def ensure_apt(pkgs: list[str]) -> None:
    """Install OS packages (best effort)."""
    if shutil.which("apt-get") is None:
        print("[runbook:vm1] apt-get not found; skipping OS package installation.", flush=True)
        return

    prefix = []
    if os.geteuid() != 0:
        prefix = ["sudo"]
        # If sudo isn't available, we can't install packages.
        if shutil.which("sudo") is None:
            print("[runbook:vm1] sudo not found; skipping OS package installation.", flush=True)
            return

    run(prefix + ["apt-get", "update"])
    run(prefix + ["apt-get", "install", "-y"] + pkgs)


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    if dest_dir.exists() and any(dest_dir.iterdir()):
        print(f"[runbook:vm1] Using existing extracted dir: {dest_dir}", flush=True)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"[runbook:vm1] Extracting {zip_path} -> {dest_dir}", flush=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def ensure_venv(proj_dir: Path) -> Path:
    venv_dir = proj_dir / ".venv"
    python_bin = venv_dir / "bin" / "python"
    if python_bin.exists():
        return python_bin

    print(f"[runbook:vm1] Creating venv: {venv_dir}", flush=True)
    run([sys.executable, "-m", "venv", str(venv_dir)])

    # Upgrade pip
    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    return python_bin


def pip_install(python_bin: Path, requirements: Path) -> None:
    run([str(python_bin), "-m", "pip", "install", "-r", str(requirements)])


def get_primary_ip() -> str:
    """Best-effort: get the IP address used for default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def docker_compose_up(compose_dir: Path) -> bool:
    """Start Prometheus+Grafana if Docker is available."""
    docker = shutil.which("docker")
    if not docker:
        print("[runbook:vm1] Docker not found; skipping Prometheus/Grafana containers.", flush=True)
        return False

    # Try both: `docker compose` (plugin) and legacy `docker-compose`.
    compose_cmd = None
    if run([docker, "compose", "version"], check=False).returncode == 0:
        compose_cmd = [docker, "compose"]
    elif shutil.which("docker-compose"):
        compose_cmd = ["docker-compose"]

    if not compose_cmd:
        print("[runbook:vm1] Docker Compose not found; skipping Prometheus/Grafana containers.", flush=True)
        return False

    print("[runbook:vm1] Starting Prometheus+Grafana (docker compose)...", flush=True)
    run(compose_cmd + ["up", "-d"], cwd=compose_dir)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=str(REPO_DIR / ZIP_NAME_DEFAULT), help="Path to VM1 zip")
    ap.add_argument("--deploy-dir", default=str(REPO_DIR / ".deploy" / "vm1_controller"), help="Extraction dir")
    ap.add_argument("--with-grafana", action="store_true", help="Start Prometheus+Grafana via Docker")
    ap.add_argument("--no-grafana", action="store_true", help="Do not start Prometheus+Grafana")
    ap.add_argument("--ofp-port", type=int, default=DEFAULT_OFP_PORT, help="OpenFlow listen port")
    ap.add_argument("--rest-port", type=int, default=DEFAULT_REST_PORT, help="Ryu WSGI REST port")
    args = ap.parse_args()

    zip_path = Path(args.zip).expanduser().resolve()
    if not zip_path.exists():
        print(f"[runbook:vm1] ERROR: zip not found: {zip_path}", file=sys.stderr)
        return 2

    deploy_dir = Path(args.deploy_dir).expanduser().resolve()
    deploy_dir.parent.mkdir(parents=True, exist_ok=True)

    # OS deps for building some pip wheels + optional Docker.
    ensure_apt([
        "python3-venv",
        "python3-pip",
        "python3-dev",
        "build-essential",
        "libssl-dev",
        "libffi-dev",
        "git",
    ])

    extract_zip(zip_path, deploy_dir)

    python_bin = ensure_venv(deploy_dir)
    pip_install(python_bin, deploy_dir / "requirements.txt")

    # Optionally start Grafana/Prometheus stack for visualization
    want_grafana = args.with_grafana and (not args.no_grafana)
    if want_grafana:
        # Ensure Docker exists (best effort)
        ensure_apt(["docker.io", "docker-compose-plugin"])
        docker_compose_up(deploy_dir / "docker")

    # Start Ryu controller
    controller_ip = get_primary_ip()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(deploy_dir)
    env["HYBRID_LB_CONFIG"] = str(deploy_dir / "config" / "default.yaml")
    env["HYBRID_LB_LOG_DIR"] = str(deploy_dir / "logs")

    ryu_mgr = deploy_dir / ".venv" / "bin" / "ryu-manager"
    if not ryu_mgr.exists():
        print(f"[runbook:vm1] ERROR: ryu-manager not found at {ryu_mgr}", file=sys.stderr)
        return 3

    app_path = deploy_dir / "hybrid_lb" / "controller" / "ryu_hybrid_lb.py"
    cmd = [
        str(ryu_mgr),
        "--ofp-tcp-listen-port", str(args.ofp_port),
        "--wsapi-host", "0.0.0.0",
        "--wsapi-port", str(args.rest_port),
        str(app_path),
    ]

    print("\n[runbook:vm1] Controller endpoints:")
    print(f"  OpenFlow: tcp://{controller_ip}:{args.ofp_port}")
    print(f"  REST API: http://{controller_ip}:{args.rest_port}/status")
    print(f"  Discover: http://{controller_ip}:{args.rest_port}/discover")
    print(f"  Metrics:  http://{controller_ip}:{DEFAULT_METRICS_PORT}/metrics")
    if want_grafana:
        print(f"  Prometheus: http://{controller_ip}:9090")
        print(f"  Grafana:    http://{controller_ip}:3000  (admin/admin)")
    print("\n[runbook:vm1] Starting ryu-manager... (Ctrl+C to stop)\n", flush=True)

    proc = subprocess.Popen(cmd, cwd=str(deploy_dir), env=env)

    def _shutdown(signum, frame):
        print(f"\n[runbook:vm1] Caught signal {signum}; stopping...", flush=True)
        try:
            proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
