#!/usr/bin/env python3
"""VM1 (Controller) - Run runbook

Starts:
  - OS-Ken controller (OpenFlow + REST)
  - Prometheus metrics endpoint (served by the app itself)

This runbook purposely advertises the inter-VM IP on **192.168.56.0/24**
(if present), because your VMs communicate over that network.

Run:
  sudo -E python3 runbook_vm1_run.py

Optional:
  sudo -E python3 runbook_vm1_run.py --prefer-iface enp0s3
  sudo -E python3 runbook_vm1_run.py --with-grafana
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEPLOY_DIR = REPO_ROOT / ".deploy" / "vm1_controller"
APP_DIR = DEPLOY_DIR / "app"
CONF_PATH = DEPLOY_DIR / "config" / "default.yaml"
VENV_DIR = DEPLOY_DIR / ".venv"


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, text=True)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)
    return p


def venv_bin(name: str) -> str:
    return str(VENV_DIR / "bin" / name)


def venv_python() -> str:
    return venv_bin("python")


def _parse_ip_addrs() -> list[tuple[str, str, int]]:
    """Return list of (iface, ip, prefixlen) for global IPv4 addresses."""
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "scope", "global"], text=True)
    except Exception:
        return []
    res: list[tuple[str, str, int]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        inet = parts[3]
        if "/" not in inet:
            continue
        ip, plen = inet.split("/", 1)
        try:
            res.append((iface, ip, int(plen)))
        except Exception:
            continue
    return res


def choose_advertise_ip(prefer_iface: str | None = None) -> str:
    """Pick the IP VM2 should use to reach VM1.

    Priority:
      1) interface specified by --prefer-iface
      2) any IP in 192.168.56.0/24 (your inter-VM network)
      3) any private RFC1918 address (192.168/16, 10/8, 172.16/12)
      4) hostname fallback
    """
    addrs = _parse_ip_addrs()

    if prefer_iface:
        for iface, ip, _plen in addrs:
            if iface == prefer_iface:
                return ip

    preferred_nets = [
        ipaddress.ip_network("192.168.56.0/24"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
    ]

    ranked: list[tuple[int, str, str]] = []
    for iface, ip, _plen in addrs:
        try:
            ip_obj = ipaddress.ip_address(ip)
        except Exception:
            continue
        rank = 999
        for i, net in enumerate(preferred_nets):
            if ip_obj in net:
                rank = i
                break
        ranked.append((rank, iface, ip))

    ranked.sort(key=lambda t: (t[0], t[1]))
    if ranked and ranked[0][0] != 999:
        return ranked[0][2]

    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def load_yaml(path: Path) -> dict:
    try:
        import yaml  # installed in venv

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def start_grafana_stack() -> None:
    docker_dir = APP_DIR / "docker"
    compose = docker_dir / "docker-compose.yml"
    if not compose.exists():
        print("[vm1-run] No docker-compose.yml inside the controller project; skipping Grafana/Prometheus stack.")
        return

    # Try docker-compose, then docker compose
    try:
        run(["docker-compose", "-f", str(compose), "up", "-d"], cwd=docker_dir)
        print("[vm1-run] Grafana/Prometheus started via docker-compose.")
        return
    except Exception:
        pass

    try:
        run(["docker", "compose", "-f", str(compose), "up", "-d"], cwd=docker_dir)
        print("[vm1-run] Grafana/Prometheus started via docker compose.")
        return
    except Exception as e:
        print("[vm1-run] Could not start Grafana/Prometheus. Ensure docker is installed and running.")
        print(f"          Error: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="VM1 run: start OS-Ken controller (OpenFlow + REST + metrics)")
    ap.add_argument(
        "--advertise-ip",
        default=os.environ.get("ADVERTISE_IP", ""),
        help="IP to advertise (default: auto; prefers 192.168.56.x)",
    )
    ap.add_argument(
        "--prefer-iface",
        default=os.environ.get("PREFER_IFACE", ""),
        help="Prefer a specific interface name (e.g., enp0s3)",
    )
    ap.add_argument("--with-grafana", action="store_true", help="Start Prometheus+Grafana via Docker (if installed).")
    args = ap.parse_args()

    if not DEPLOY_DIR.exists() or not VENV_DIR.exists() or not APP_DIR.exists():
        print("[vm1-run] Missing deployment. Run setup first:\n  sudo -E python3 runbook_vm1_setup.py")
        raise SystemExit(1)

    advertise_ip = args.advertise_ip.strip()
    if not advertise_ip or advertise_ip.lower() == "auto":
        prefer_iface = args.prefer_iface.strip() or None
        advertise_ip = choose_advertise_ip(prefer_iface)

    cfg = load_yaml(CONF_PATH)
    ctrl = (cfg.get("controller") or {})
    vip = (cfg.get("vip") or {})

    of_port = int(ctrl.get("of_listen_port", 6653))
    rest_port = int(ctrl.get("rest_port", 8080))
    metrics_port = int(ctrl.get("metrics_port", 9100))

    if args.with_grafana:
        start_grafana_stack()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(APP_DIR) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["HYBRID_LB_CONFIG"] = str(CONF_PATH)

    osken_mgr = Path(venv_bin("osken-manager"))
    # In some environments the console_script wrapper is missing even when os-ken is installed.
    # We can always launch the manager via `python -m os_ken.cmd.manager`.
    mgr_prefix: list[str]
    if osken_mgr.exists():
        mgr_prefix = [str(osken_mgr)]
    else:
        mgr_prefix = [venv_python(), "-m", "os_ken.cmd.manager"]
        print("[vm1-run] WARNING: osken-manager script not found; using python -m os_ken.cmd.manager instead.")

    cmd = [
        *mgr_prefix,
        "--ofp-listen-host",
        "0.0.0.0",
        "--ofp-tcp-listen-port",
        str(of_port),
        "--wsapi-host",
        "0.0.0.0",
        "--wsapi-port",
        str(rest_port),
        "--verbose",
        "hybrid_lb.controller.ryu_hybrid_lb",
    ]

    print("[vm1-run] Starting SDN Controller (OS-Ken)...")
    print(f"         Inter-VM IP (advertise): {advertise_ip}")
    print(f"         OpenFlow: tcp://{advertise_ip}:{of_port}")
    print(f"         REST API: http://{advertise_ip}:{rest_port}/health")
    print(f"         Metrics : http://{advertise_ip}:{metrics_port}/metrics")
    print(f"         VIP     : {vip.get('ip', '10.0.0.100')}")
    print("[vm1-run] Command:")
    print("         " + " ".join(cmd))
    print("\n[vm1-run] Stop with Ctrl+C\n")

    # Foreground
    try:
        p = subprocess.Popen(cmd, env=env, cwd=str(APP_DIR))
        while True:
            rc = p.poll()
            if rc is not None:
                raise SystemExit(rc)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            p.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
