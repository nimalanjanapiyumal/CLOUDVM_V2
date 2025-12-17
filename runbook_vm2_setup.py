#!/usr/bin/env python3
"""VM2 (Data Plane) - Setup runbook

Installs Mininet + Open vSwitch + iperf3 and extracts the VM2 project zip.

Run:
  sudo -E python3 runbook_vm2_setup.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ZIP_NAME = "vm2_dataplane_project.zip"
DEPLOY_DIR = REPO_ROOT / ".deploy" / "vm2_dataplane"
APP_DIR = DEPLOY_DIR / "app"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=p.stdout)
    return p


def apt_install(pkgs: list[str], *, max_wait_sec: int = 1200) -> None:
    """Install apt packages with lock handling (unattended-upgrades)."""
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    start = time.time()
    while True:
        try:
            run(["apt-get", "update"], env=env)
            run(["apt-get", "install", "-y"] + pkgs, env=env)
            return
        except subprocess.CalledProcessError as e:
            out = (e.output or "").strip()
            lock_err = (
                "Could not get lock" in out
                or "Unable to acquire the dpkg frontend lock" in out
                or "is held by process" in out
                or "dpkg frontend lock" in out
            )
            if lock_err:
                waited = int(time.time() - start)
                if waited >= max_wait_sec:
                    print(
                        "\n[vm2-setup] ERROR: APT lock held too long (usually unattended-upgrades).\n"
                        "Let unattended-upgrades finish, then re-run this runbook.\n"
                    )
                    print(out)
                    raise
                print(f"[vm2-setup] APT lock detected. Waiting 10s and retrying... ({waited}s elapsed)")
                time.sleep(10)
                continue

            print(out)
            raise


def extract_zip() -> None:
    zip_path = REPO_ROOT / ZIP_NAME
    if not zip_path.exists():
        raise FileNotFoundError(f"Missing {ZIP_NAME} in repo root: {zip_path}")

    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    APP_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(APP_DIR)


def main() -> None:
    ap = argparse.ArgumentParser(description="VM2 setup (dataplane): install Mininet/OVS/iperf3 + project")
    _ = ap.parse_args()

    if not is_root():
        print("[vm2-setup] Please run as root:\n  sudo -E python3 runbook_vm2_setup.py")
        raise SystemExit(1)

    (REPO_ROOT / ".deploy").mkdir(exist_ok=True)

    print("[vm2-setup] Installing OS dependencies (Mininet + OVS + iperf3) (APT lock-safe)...")
    pkgs = ["mininet", "openvswitch-switch", "iperf3", "python3", "python3-pip", "curl"]
    apt_install(pkgs)

    print("[vm2-setup] Extracting dataplane project zip...")
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    extract_zip()

    if not (APP_DIR / "topology" / "mininet_stack.py").exists():
        raise RuntimeError("Expected topology/mininet_stack.py missing after extraction.")

    print("\n[vm2-setup] DONE ✅")
    print(f"Next: sudo -E python3 {REPO_ROOT/'runbook_vm2_run.py'} --start-load --cli")


if __name__ == "__main__":
    main()
