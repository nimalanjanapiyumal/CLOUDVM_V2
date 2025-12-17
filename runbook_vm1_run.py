\
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
DEPLOY_DIR = REPO_ROOT / ".deploy" / "vm1_controller"
APP_DIR = DEPLOY_DIR / "app"
CONF_PATH = DEPLOY_DIR / "config" / "default.yaml"
VENV_DIR = DEPLOY_DIR / ".venv"

DISCOVERY_PORT = 8080


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, text=True)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)
    return p


def venv_bin(name: str) -> str:
    return str(VENV_DIR / "bin" / name)


def _parse_ip_addrs() -> list[tuple[str, str, int]]:
    """Return list of (iface, ip, prefixlen) for global IPv4 addresses."""
    # Example line: "2: enp0s8    inet 10.0.3.15/24 brd 10.0.3.255 scope global dynamic noprefixroute enp0s8"
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


def choose_advertise_ip(prefer_iface: str | None = None) -> str:
    addrs = _parse_ip_addrs()
    if prefer_iface:
        for iface, ip, _plen in addrs:
            if iface == prefer_iface:
                return ip

    # Prefer RFC1918 with order: 10/8 -> 172.16/12 -> 192.168/16
    preferred_ranges = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ]
    ranked = []
    for iface, ip, _plen in addrs:
        try:
            ip_obj = ipaddress.ip_address(ip)
        except Exception:
            continue
        rank = 999
        for i, net in enumerate(preferred_ranges):
            if ip_obj in net:
                rank = i
                break
        ranked.append((rank, iface, ip))
    ranked.sort(key=lambda t: (t[0], t[1]))
    if ranked:
        return ranked[0][2]
    # fallback: hostname IP
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def load_yaml_minimal(path: Path) -> dict:
    # Avoid extra deps in runbook; config is YAML but mostly JSON-compatible.
    # We'll parse a subset: this is enough for /discover response.
    try:
        import yaml  # provided in venv
        return yaml.safe_load(path.read_text())
    except Exception:
        return {}


class DiscoveryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/discover", "/status", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return
        payload = self.server.payload()
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


def start_discovery_server(payload_fn):
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", DISCOVERY_PORT), DiscoveryHandler)
    httpd.payload = payload_fn  # type: ignore
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def start_grafana_stack():
    docker_dir = APP_DIR / "docker"
    compose = docker_dir / "docker-compose.yml"
    if not compose.exists():
        print("[vm1-run] No docker compose file found; skipping Grafana/Prometheus.")
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
        print(f"           Error: {e}")


def main():
    ap = argparse.ArgumentParser(description="VM1 run: starts discovery API + Ryu controller.")
    ap.add_argument("--advertise-ip", default=os.environ.get("ADVERTISE_IP", ""), help="IP to advertise to VM2 (default: auto).")
    ap.add_argument("--prefer-iface", default=os.environ.get("PREFER_IFACE", ""), help="Prefer a specific interface (e.g., enp0s8).")
    ap.add_argument("--with-grafana", action="store_true", help="Start Prometheus+Grafana via Docker (if installed).")
    args = ap.parse_args()

    if not DEPLOY_DIR.exists() or not VENV_DIR.exists():
        print("[vm1-run] Missing deployment. Run setup first:\n  sudo -E python3 runbook_vm1_setup.py")
        sys.exit(1)

    advertise_ip = args.advertise_ip.strip()
    if not advertise_ip or advertise_ip.lower() == "auto":
        prefer_iface = args.prefer_iface.strip() or None
        advertise_ip = choose_advertise_ip(prefer_iface=prefer_iface)

    cfg = load_yaml_minimal(CONF_PATH)
    vip = (cfg.get("vip") or {})
    ctrl = (cfg.get("controller") or {})
    backends = cfg.get("backends") or []

    openflow_port = int(ctrl.get("openflow_port", 6653))
    metrics_port = int(ctrl.get("metrics_port", 9100))

    def payload():
        return {
            "controller_ip": advertise_ip,
            "openflow_port": openflow_port,
            "metrics_port": metrics_port,
            "vip": {"ip": vip.get("ip", "10.0.0.100"), "tcp_port": vip.get("tcp_port", 8000)},
            "backends": backends,
            "ts": time.time(),
        }

    start_discovery_server(payload)
    print(f"[vm1-run] Discovery API listening on 0.0.0.0:{DISCOVERY_PORT}")
    print(f"[vm1-run] Advertise IP to dataplane: {advertise_ip}")

    if args.with_grafana:
        start_grafana_stack()
        print("[vm1-run] Grafana: http://<VM1_IP>:3000 (admin/admin)  Prometheus: http://<VM1_IP>:9090")

    # Start Ryu controller
    env = os.environ.copy()
    env["PYTHONPATH"] = str(APP_DIR) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["HYBRID_LB_CONFIG"] = str(CONF_PATH)

    ryu_manager = venv_bin("ryu-manager")
    if not Path(ryu_manager).exists():
        print("[vm1-run] ryu-manager not found in venv. Re-run setup.")
        sys.exit(1)

    cmd = [ryu_manager, "--ofp-tcp-listen-port", str(openflow_port), "hybrid_lb.controller.ryu_hybrid_lb"]
    print("[vm1-run] Starting Ryu controller:")
    print("         " + " ".join(cmd))
    print(f"[vm1-run] Metrics: http://{advertise_ip}:{metrics_port}/metrics")
    print("[vm1-run] Stop with Ctrl+C\n")

    # Foreground
    try:
        p = subprocess.Popen(cmd, env=env, cwd=str(APP_DIR))
        p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            p.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
