# Hybrid SDN Load Balancer (2-VM) — GitHub-ready bundle

This repo is designed for a **2 VM** deployment:

- **VM1 (Controller VM)**: **OS-Ken** (OpenStack-maintained fork of Ryu) SDN controller + Hybrid RR/GA load balancer + Prometheus metrics + optional Grafana stack.
- **VM2 (Data Plane VM)**: Mininet + OVS + 1 client host + 3 backend hosts + traffic generator.

Why OS-Ken (not `ryu`)? The latest `ryu` release on PyPI is old and fails to install cleanly on Ubuntu 22.04 / Python 3.10. OS-Ken provides the same OpenFlow controller API but supports modern Python.

## What’s in this repo

- `vm1_controller_project.zip` — VM1 controller project (code + config + requirements)
- `vm2_dataplane_project.zip` — VM2 dataplane project (Mininet topology + tools)
- `runbook_vm1_setup.py` — VM1 setup (APT + venv + pip install)
- `runbook_vm1_run.py` — VM1 run (starts OS-Ken controller: OpenFlow + REST + metrics)
- `runbook_vm2_setup.py` — VM2 setup (APT install Mininet/OVS/iperf3)
- `runbook_vm2_run.py` — VM2 run (discovers controller via REST /discover, starts Mininet, optional load)

---

## Network assumption (important)

Your inter-VM traffic is on **192.168.56.0/24** (VirtualBox host-only style network). These runbooks:

- Prefer VM1's **192.168.56.x** IP for advertising
- Prefer scanning **192.168.56.0/24** on VM2 for discovery

If your lab uses a different subnet, you can override with `--prefer-iface`, `--advertise-ip`, or `--controller-ip`.

---

## VM1 (Controller) — Setup + Run

### Setup (run once)
```bash
sudo -E python3 runbook_vm1_setup.py
```

Optional (Grafana/Prometheus UI stack via Docker):
```bash
sudo -E python3 runbook_vm1_setup.py --with-grafana
```

### Run (keeps running until Ctrl+C)
```bash
sudo -E python3 runbook_vm1_run.py
```

Optional:
```bash
sudo -E python3 runbook_vm1_run.py --with-grafana
```

VM1 ports:
- OpenFlow: `6653/tcp` (or as configured in `.deploy/vm1_controller/config/default.yaml`)
- REST API: `8080/tcp` (for `/health`, `/discover`, `/status`)
- Prometheus metrics: `9100/tcp` (exposed by the controller app)
- Optional Docker UI stack: Grafana `3000/tcp`, Prometheus UI `9090/tcp`

---

## VM2 (Data Plane) — Setup + Run

### Setup (run once)
```bash
sudo -E python3 runbook_vm2_setup.py
```

### Run simulation
Auto-discovers VM1 by scanning 192.168.56.0/24 (and a couple other local private nets) for:
`http://<ip>:8080/discover`

```bash
sudo -E python3 runbook_vm2_run.py --start-load --cli
```

If discovery is blocked, specify controller explicitly:
```bash
sudo -E python3 runbook_vm2_run.py --controller-ip <VM1_IP> --start-load --cli
```

---

## Notes for OpenStack / security groups

If you deploy as OpenStack instances, ensure the security group allows VM2 → VM1 inbound:
- OpenFlow port (default 6653/tcp)
- REST port (default 8080/tcp) for discovery
- Metrics port (default 9100/tcp) if you want remote Prometheus scraping
