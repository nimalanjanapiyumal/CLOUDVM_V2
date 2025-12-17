# Hybrid SDN Load Balancer (2-VM) — GitHub-ready bundle

This repo is designed for a **2 VM** deployment:

- **VM1 (Controller VM)**: Ryu SDN controller + Hybrid RR/GA load balancer + Prometheus metrics + optional Grafana/Prometheus via Docker.
- **VM2 (Data Plane VM)**: Mininet + OVS + 1 client host + 3 backend hosts + traffic generators.

## Files you will find in the repo

- `vm1_controller_project.zip` — VM1 controller project (code + config + requirements)
- `vm2_dataplane_project.zip` — VM2 dataplane project (Mininet topology + tools)
- `runbook_vm1_setup.py` — VM1 setup (APT + venv + pip install; fixes Ryu/setuptools issue)
- `runbook_vm1_run.py` — VM1 run (starts controller + discovery API)
- `runbook_vm2_setup.py` — VM2 setup (APT install Mininet/OVS/iperf3)
- `runbook_vm2_run.py` — VM2 run (starts Mininet topology + optional load)

---

## VM1 (Controller) — Setup + Run

### Setup (run once)
```bash
sudo -E python3 runbook_vm1_setup.py
```

### Run (keeps running until Ctrl+C)
```bash
sudo -E python3 runbook_vm1_run.py --with-grafana
```

VM1 ports:
- OpenFlow: `6653/tcp`
- Discovery API: `8080/tcp`
- Prometheus metrics: `9100/tcp`
- Optional: Grafana `3000/tcp`, Prometheus UI `9090/tcp`

---

## VM2 (Data Plane) — Setup + Run

### Setup (run once)
```bash
sudo -E python3 runbook_vm2_setup.py
```

### Run simulation
Auto-discovers controller by scanning the local /24 for `http://<ip>:8080/discover`:

```bash
sudo -E python3 runbook_vm2_run.py --start-load --cli
```

If discovery is blocked, specify controller explicitly:
```bash
sudo -E python3 runbook_vm2_run.py --controller-ip <VM1_IP> --start-load --cli
```

---

## Important note on multiple NICs / OpenStack interfaces

If VM1 has multiple NICs (for example: management + internal network), set the advertised IP:
```bash
sudo -E python3 runbook_vm1_run.py --advertise-ip <VM1_INTERNAL_IP>
```

Or set it via environment:
```bash
export ADVERTISE_IP=<VM1_INTERNAL_IP>
sudo -E python3 runbook_vm1_run.py
```
