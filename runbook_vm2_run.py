\
#!/usr/bin/env python3
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


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _parse_ip_addrs() -> list[tuple[str, str, int]]:
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "scope", "global"], text=True)
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        inet = parts[3]  # ip/prefix
        if "/" not in inet:
            continue
        ip, plen = inet.split("/", 1)
        try:
            res.append((iface, ip, int(plen)))
        except Exception:
            continue
    return res


def _rank_private(ip: str) -> int:
    ip_obj = ipaddress.ip_address(ip)
    if ip_obj in ipaddress.ip_network("10.0.0.0/8"):
        return 0
    if ip_obj in ipaddress.ip_network("172.16.0.0/12"):
        return 1
    if ip_obj in ipaddress.ip_network("192.168.0.0/16"):
        return 2
    return 9


def candidate_networks() -> list[ipaddress.IPv4Network]:
    nets = []
    for _iface, ip, plen in _parse_ip_addrs():
        try:
            ipi = ipaddress.ip_interface(f"{ip}/{plen}")
            if ipi.ip.is_private:
                nets.append(ipi.network)
        except Exception:
            continue
    # Prefer smaller networks first (faster scan), and private rank
    nets = sorted(set(nets), key=lambda n: (n.prefixlen, _rank_private(str(list(n.hosts())[0])) if n.num_addresses > 2 else 9))
    # If very large, fall back to /24 of the interface IP to avoid huge scans
    final = []
    for net in nets:
        if net.prefixlen < 24:
            # shrink to /24 using the first address as anchor
            anchor = list(net.hosts())[0]
            final.append(ipaddress.ip_network(f"{anchor}/24", strict=False))
        else:
            final.append(net)
    return final[:3]  # scan up to 3 candidate nets


def _tcp_open(ip: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def discover_controller() -> tuple[str, dict]:
    """Find a controller running discovery API on port 8080."""
    nets = candidate_networks()
    if not nets:
        raise RuntimeError("No private IPv4 networks detected for discovery. Use --controller-ip.")

    for net in nets:
        hosts = [str(h) for h in net.hosts()]
        # quick parallel TCP scan
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            futs = {ex.submit(_tcp_open, ip, 8080): ip for ip in hosts}
            for fut in concurrent.futures.as_completed(futs):
                ip = futs[fut]
                try:
                    if fut.result():
                        # try GET /discover
                        try:
                            with urlopen(f"http://{ip}:8080/discover", timeout=1.0) as r:
                                data = json.loads(r.read().decode("utf-8"))
                            # validate minimally
                            if "openflow_port" in data and "controller_ip" in data:
                                return data["controller_ip"], data
                        except Exception:
                            continue
                except Exception:
                    continue

    raise RuntimeError("Controller discovery failed. Use --controller-ip <VM1_IP>.")


def main():
    ap = argparse.ArgumentParser(description="VM2 run: starts Mininet dataplane and runs the simulation.")
    ap.add_argument("--controller-ip", default=os.environ.get("CONTROLLER_IP", ""), help="VM1 controller IP (if empty, auto-discovery is attempted).")
    ap.add_argument("--start-load", action="store_true", help="Run HTTP load from h1 to VIP after startup.")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--rps", type=int, default=20)
    ap.add_argument("--cli", action="store_true", help="Drop into Mininet CLI.")
    args = ap.parse_args()

    if not is_root():
        print("[vm2-run] Please run as root (Mininet requires it):\n  sudo -E python3 runbook_vm2_run.py")
        sys.exit(1)

    topo = APP_DIR / "topology" / "mininet_stack.py"
    if not topo.exists():
        print("[vm2-run] Missing deployment. Run setup first:\n  sudo -E python3 runbook_vm2_setup.py")
        sys.exit(1)

    controller_ip = args.controller_ip.strip()
    discover_payload = None
    if not controller_ip:
        print("[vm2-run] No controller IP provided; attempting discovery on port 8080 ...")
        controller_ip, discover_payload = discover_controller()
        print(f"[vm2-run] Discovered controller at {controller_ip} (payload={discover_payload})")

    cmd = ["python3", str(topo), "--controller-ip", controller_ip]
    if args.start_load:
        cmd += ["--start-load", "--duration", str(args.duration), "--rps", str(args.rps)]
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
