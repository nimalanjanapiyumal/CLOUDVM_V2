#!/usr/bin/env python3
"""VM2 (Data Plane) - Run runbook

What this does:
  1) Discovers VM1 controller by scanning the inter-VM network (prefers 192.168.56.0/24)
  2) Calls VM1 REST endpoint /discover to get OpenFlow port + VIP details
  3) Starts Mininet + OVS, points switch to the controller, starts backend servers
  4) Optionally generates HTTP load from h1 -> VIP

Run:
  sudo -E python3 runbook_vm2_run.py --start-load --cli

If auto-discovery fails, specify the controller IP manually:
  sudo -E python3 runbook_vm2_run.py --controller-ip 192.168.56.121
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent
DEPLOY_DIR = REPO_ROOT / ".deploy" / "vm2_dataplane"
APP_DIR = DEPLOY_DIR / "app"

DEFAULT_REST_PORT = 8080


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


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


def _net_rank(net: ipaddress.IPv4Network) -> tuple[int, int]:
    """Sort key for candidate networks.

    Priority:
      1) 192.168.56.0/24 (your inter-VM network)
      2) other 192.168.0.0/16
      3) 10.0.0.0/8
      4) 172.16.0.0/12

    Second key: prefer /24 or smaller scans.
    """
    if net.subnet_of(ipaddress.ip_network("192.168.56.0/24")) or net == ipaddress.ip_network("192.168.56.0/24"):
        p = 0
    elif net.subnet_of(ipaddress.ip_network("192.168.0.0/16")):
        p = 1
    elif net.subnet_of(ipaddress.ip_network("10.0.0.0/8")):
        p = 2
    elif net.subnet_of(ipaddress.ip_network("172.16.0.0/12")):
        p = 3
    else:
        p = 9
    return (p, net.prefixlen)


def candidate_networks() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    for _iface, ip, plen in _parse_ip_addrs():
        try:
            ipi = ipaddress.ip_interface(f"{ip}/{plen}")
            if not ipi.ip.is_private:
                continue
            net = ipi.network
            # Avoid scanning very large nets; shrink to /24.
            if net.prefixlen < 24:
                net = ipaddress.ip_network(f"{ip}/24", strict=False)
            nets.append(net)
        except Exception:
            continue

    # Always include the expected inter-VM network first (even if not bound),
    # because VirtualBox host-only typically uses 192.168.56.0/24.
    nets.append(ipaddress.ip_network("192.168.56.0/24"))

    uniq = sorted(set(nets), key=_net_rank)
    return uniq[:4]


def _tcp_open(ip: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _fetch_discover(ip: str, rest_port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urlopen(f"http://{ip}:{rest_port}/discover", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        # Expected shape from HybridLBRestController.discovery_payload():
        # {"vip": {...}, "controller": {...}}
        if isinstance(data, dict) and "controller" in data and "vip" in data:
            return data
    except Exception:
        return None
    return None


def discover_controller(rest_port: int = DEFAULT_REST_PORT) -> tuple[str, dict]:
    """Find a controller REST endpoint by scanning candidate networks."""
    nets = candidate_networks()
    for net in nets:
        hosts = [str(h) for h in net.hosts()]
        print(f"[vm2-run] Scanning {net} for controller REST on port {rest_port} ...")

        # Parallel TCP scan
        with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
            futs = {ex.submit(_tcp_open, ip, rest_port): ip for ip in hosts}
            for fut in concurrent.futures.as_completed(futs):
                ip = futs[fut]
                try:
                    if not fut.result():
                        continue
                except Exception:
                    continue

                payload = _fetch_discover(ip, rest_port)
                if payload:
                    return ip, payload

    raise RuntimeError(
        "Controller discovery failed. Provide --controller-ip <VM1_IP> or verify VM1 REST is reachable."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="VM2 run: start Mininet dataplane and run the simulation")
    ap.add_argument(
        "--controller-ip",
        default=os.environ.get("CONTROLLER_IP", ""),
        help="VM1 controller IP (if empty, auto-discovery is attempted)",
    )
    ap.add_argument(
        "--controller-rest-port",
        type=int,
        default=int(os.environ.get("CONTROLLER_REST_PORT", str(DEFAULT_REST_PORT))),
        help="VM1 REST port for discovery (/discover) (default: 8080)",
    )
    ap.add_argument("--start-load", action="store_true", help="Run HTTP load from h1 to VIP after startup")
    ap.add_argument("--duration", type=int, default=20, help="Load duration (seconds) if --start-load")
    ap.add_argument("--concurrency", type=int, default=20, help="Concurrent client workers if --start-load")
    ap.add_argument("--cli", action="store_true", help="Drop into Mininet CLI")
    args = ap.parse_args()

    if not is_root():
        print("[vm2-run] Please run as root (Mininet requires it):\n  sudo -E python3 runbook_vm2_run.py")
        raise SystemExit(1)

    topo = APP_DIR / "topology" / "mininet_stack.py"
    if not topo.exists():
        print("[vm2-run] Missing deployment. Run setup first:\n  sudo -E python3 runbook_vm2_setup.py")
        raise SystemExit(1)

    controller_ip = args.controller_ip.strip()
    rest_port = int(args.controller_rest_port)

    payload: dict | None = None
    if not controller_ip:
        print("[vm2-run] No controller IP provided; attempting auto-discovery...")
        controller_ip, payload = discover_controller(rest_port)
        print(f"[vm2-run] Discovered controller at {controller_ip}:{rest_port}")

    controller_port = 6653
    vip_ip = "10.0.0.100"
    http_port = 8080

    if payload:
        ctrl = payload.get("controller") or {}
        vip = payload.get("vip") or {}

        controller_port = int(ctrl.get("of_listen_port", controller_port))
        vip_ip = str(vip.get("ip", vip_ip))

        # Pick an HTTP service port if present
        services = vip.get("services") or []
        if isinstance(services, list):
            # Prefer 8080 if present, else first int-ish
            if 8080 in services:
                http_port = 8080
            else:
                for s in services:
                    try:
                        http_port = int(s)
                        break
                    except Exception:
                        continue

    cmd = [
        "python3",
        str(topo),
        "--controller-ip",
        controller_ip,
        "--controller-port",
        str(controller_port),
        "--vip",
        vip_ip,
        "--http-port",
        str(http_port),
    ]

    if args.start_load:
        cmd += ["--start-load", "--duration", str(args.duration), "--concurrency", str(args.concurrency)]
    if args.cli:
        cmd += ["--cli"]

    print("[vm2-run] Starting Mininet simulation:")
    print("         " + " ".join(cmd))
    print("[vm2-run] Stop with Ctrl+C\n")

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(e.returncode)


if __name__ == "__main__":
    main()
