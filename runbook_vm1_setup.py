\
#!/usr/bin/env python3
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


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def apt_install(pkgs: list[str], *, max_wait_sec: int = 900) -> None:
    """Install apt packages with automatic lock handling (unattended-upgrades)."""
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    start = time.time()
    while True:
        try:
            run(["apt-get", "update"], env=env)
            run(["apt-get", "install", "-y"] + pkgs, env=env)
            return
        except subprocess.CalledProcessError as e:
            out = e.output or ""
            lock_err = ("Could not get lock" in out) or ("Unable to acquire the dpkg frontend lock" in out) or ("is held by process" in out)
            if lock_err:
                waited = int(time.time() - start)
                if waited >= max_wait_sec:
                    print("\n[vm1-setup] ERROR: APT lock is held too long. This is usually unattended-upgrades.\n"
                          "Wait until it finishes and re-run this setup runbook.\n")
                    print(out)
                    raise
                print(f"[vm1-setup] APT lock detected (likely unattended-upgrades). Waiting 10s and retrying... ({waited}s elapsed)")
                time.sleep(10)
                continue
            print(out)
            raise


def ensure_venv(python_bin: str) -> Path:
    if VENV_DIR.exists():
        return VENV_DIR
    run([python_bin, "-m", "venv", str(VENV_DIR)])
    return VENV_DIR


def venv_python() -> str:
    return str(VENV_DIR / "bin" / "python")


def pip_install(args: list[str]) -> None:
    py = venv_python()
    cmd = [py, "-m", "pip"] + args
    try:
        run(cmd)
    except subprocess.CalledProcessError as e:
        print(e.output)
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
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    src = APP_DIR / "config" / "default.yaml"
    dst = CONF_DIR / "default.yaml"
    if src.exists():
        dst.write_text(src.read_text(), encoding="utf-8")
    else:
        # fallback: create minimal
        dst.write_text("vip:\n  ip: \"10.0.0.100\"\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="VM1 setup: installs deps + creates venv + installs Ryu safely.")
    ap.add_argument("--with-grafana", action="store_true", help="Also install Docker + docker-compose (optional).")
    args = ap.parse_args()

    if not is_root():
        print("[vm1-setup] Please run as root:\n  sudo -E python3 runbook_vm1_setup.py")
        sys.exit(1)

    (REPO_ROOT / ".deploy").mkdir(exist_ok=True)

    print("[vm1-setup] Installing OS dependencies (with APT lock handling)...")
    pkgs = ["python3-venv", "python3-pip", "python3-dev", "build-essential", "libssl-dev", "libffi-dev", "git"]
    if args.with_grafana:
        pkgs += ["docker.io", "docker-compose"]
    apt_install(pkgs)

    print("[vm1-setup] Extracting controller project zip...")
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    extract_zip()
    copy_default_config()

    print("[vm1-setup] Creating venv...")
    ensure_venv("python3")

    print("[vm1-setup] Upgrading pip + pinning build tooling (fixes Ryu build on Ubuntu 22 / Python 3.10)...")
    pip_install(["install", "--upgrade", "pip"])
    # Critical pins: newer setuptools breaks Ryu 4.34 build hooks.
    pip_install(["install", "--upgrade", "setuptools==65.5.1", "wheel==0.41.3"])

    req = APP_DIR / "requirements.txt"
    if not req.exists():
        raise FileNotFoundError(f"Missing requirements.txt inside zip at {req}")

    print("[vm1-setup] Installing Python requirements with --no-build-isolation...")
    try:
        pip_install(["install", "--no-build-isolation", "-r", str(req)])
    except Exception:
        # fallback: disable pep517 for ryu in case build backend changes
        print("[vm1-setup] Retry with --no-use-pep517 for Ryu...")
        pip_install(["install", "--no-build-isolation", "--no-use-pep517", "ryu==4.34"])
        pip_install(["install", "--no-build-isolation", "-r", str(req)])

    print("\n[vm1-setup] DONE ✅")
    print(f"Next: sudo -E python3 {REPO_ROOT/'runbook_vm1_run.py'}")
    if args.with_grafana:
        print("Grafana/Prometheus will be started by the run runbook with --with-grafana.")


if __name__ == "__main__":
    main()
