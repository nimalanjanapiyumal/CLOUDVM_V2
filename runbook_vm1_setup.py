#!/usr/bin/env python3
"""VM1 (Controller) - Setup runbook

What this does (Ubuntu 22.04):
  1) Installs OS packages (python venv tooling, build deps, git)
  2) Extracts vm1_controller_project.zip into .deploy/vm1_controller/app
  3) Creates a venv at .deploy/vm1_controller/.venv
  4) Installs Python requirements (OS-Ken + metrics + yaml)

Run:
  sudo -E python3 runbook_vm1_setup.py

Optional:
  sudo -E python3 runbook_vm1_setup.py --with-grafana
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
ZIP_NAME = "vm1_controller_project.zip"
DEPLOY_DIR = REPO_ROOT / ".deploy" / "vm1_controller"
APP_DIR = DEPLOY_DIR / "app"
CONF_DIR = DEPLOY_DIR / "config"
VENV_DIR = DEPLOY_DIR / ".venv"


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
                        "\n[vm1-setup] ERROR: APT lock held too long (usually unattended-upgrades).\n"
                        "Let unattended-upgrades finish, then re-run this runbook.\n"
                    )
                    print(out)
                    raise
                print(f"[vm1-setup] APT lock detected. Waiting 10s and retrying... ({waited}s elapsed)")
                time.sleep(10)
                continue

            # Non-lock error
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


def copy_default_config() -> None:
    """Copy the default config from the project zip into .deploy/config."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    src = APP_DIR / "config" / "default.yaml"
    dst = CONF_DIR / "default.yaml"
    if src.exists():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        dst.write_text(
            "controller:\n  of_listen_port: 6653\n  rest_port: 8080\n  metrics_port: 9100\n"
            "vip:\n  ip: '10.0.0.100'\n  mac: '00:00:00:00:00:64'\n",
            encoding="utf-8",
        )


def ensure_venv(python_bin: str = "python3") -> None:
    if VENV_DIR.exists():
        return
    run([python_bin, "-m", "venv", str(VENV_DIR)])


def venv_python() -> str:
    return str(VENV_DIR / "bin" / "python")


def venv_bin(name: str) -> str:
    return str(VENV_DIR / "bin" / name)


def ensure_osken_manager_wrapper() -> None:
    """Ensure an executable named 'osken-manager' exists in the venv.

    In some environments the console_script wrapper may not be generated even if
    the 'os-ken' package is installed. OS-Ken can always be launched via:
        <venv>/bin/python -m os_ken.cmd.manager

    This function creates a small wrapper script at:
        <venv>/bin/osken-manager
    when missing.
    """

    osken_mgr = Path(venv_bin("osken-manager"))
    if osken_mgr.exists():
        return

    py = venv_python()

    # Verify os_ken is importable inside the venv before creating the wrapper.
    try:
        run([py, "-c", "import os_ken"], check=True)
    except Exception:
        return

    wrapper = f"""#!{py}
import runpy

if __name__ == '__main__':
    # Execute OS-Ken's manager module as if 'osken-manager' was invoked.
    runpy.run_module('os_ken.cmd.manager', run_name='__main__')
"""
    osken_mgr.write_text(wrapper, encoding="utf-8")
    os.chmod(osken_mgr, 0o755)


def pip_install(args: list[str]) -> None:
    py = venv_python()
    cmd = [py, "-m", "pip"] + args
    try:
        run(cmd)
    except subprocess.CalledProcessError as e:
        print(e.output or "")
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="VM1 setup (controller): install deps + venv + OS-Ken + project")
    ap.add_argument("--with-grafana", action="store_true", help="Also install Docker + Compose for Grafana/Prometheus.")
    args = ap.parse_args()

    if not is_root():
        print("[vm1-setup] Please run as root:\n  sudo -E python3 runbook_vm1_setup.py")
        raise SystemExit(1)

    (REPO_ROOT / ".deploy").mkdir(exist_ok=True)

    print("[vm1-setup] Installing OS dependencies (APT lock-safe)...")
    pkgs = [
        "python3-venv",
        "python3-pip",
        "python3-dev",
        "build-essential",
        "libssl-dev",
        "libffi-dev",
        "git",
        "curl",
    ]
    if args.with_grafana:
        pkgs += ["docker.io", "docker-compose"]
    apt_install(pkgs)

    print("[vm1-setup] Extracting controller project zip...")
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    extract_zip()
    copy_default_config()

    print("[vm1-setup] Creating venv...")
    ensure_venv("python3")

    print("[vm1-setup] Upgrading pip tooling...")
    pip_install(["install", "--upgrade", "pip", "setuptools", "wheel"])

    req = APP_DIR / "requirements.txt"
    if not req.exists():
        raise FileNotFoundError(f"Missing requirements.txt inside zip at {req}")

    print("[vm1-setup] Installing Python requirements...")
    pip_install(["install", "-r", str(req)])

    # Make sure we can run the controller even if console scripts were not generated.
    ensure_osken_manager_wrapper()

    # Sanity check
    osken_mgr = Path(venv_bin("osken-manager"))
    if not osken_mgr.exists():
        print("\n[vm1-setup] WARNING: osken-manager was not found in the venv.")
        print("           Try:  .deploy/vm1_controller/.venv/bin/python -m pip show os-ken")
        print("           If missing, re-run this setup or check your internet/proxy.")
    else:
        print(f"[vm1-setup] Found controller executable: {osken_mgr}")

    print("\n[vm1-setup] DONE")
    print(f"Next: sudo -E python3 {REPO_ROOT/'runbook_vm1_run.py'}")


if __name__ == "__main__":
    main()
