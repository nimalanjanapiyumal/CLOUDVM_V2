# Hybrid SDN Load Balancing (2-VM Deployment)

This repository contains **two deployment artifacts (ZIPs)** and **two single-file Python runbooks**:

- **VM1 (Controller VM)**: `vm1_controller_project.zip` + `runbook_vm1_controller.py`
  - Ryu SDN controller (OpenFlow 1.3)
  - Hybrid RR + GA load balancer
  - Flow rule management to steer VIP traffic
  - Monitoring loop + Prometheus metrics + REST API
  - Optional Prometheus+Grafana via Docker (for visualization)

- **VM2 (Data Plane VM)**: `vm2_dataplane_project.zip` + `runbook_vm2_dataplane.py`
  - Mininet topology + OVS switch
  - Demo backend servers (HTTP port 8080, iperf3 port 5201)
  - Optional traffic generation from Mininet client

## Quick run (Ubuntu 22.04)

VM1 (Controller VM):
```bash
python3 runbook_vm1_controller.py --with-grafana
```

VM2 (Data Plane VM):
```bash
sudo -E python3 runbook_vm2_dataplane.py --start-load --cli
```

The VM2 runbook will attempt controller auto-discovery by scanning the local /24 network for the controller REST endpoint.
If your network is not /24, provide the controller IP explicitly:
```bash
sudo -E CONTROLLER_IP=<VM1_IP> python3 runbook_vm2_dataplane.py --start-load --cli
```

## Endpoints

On VM1 after start:
- REST: `http://<VM1_IP>:8080/status`
- Discover: `http://<VM1_IP>:8080/discover`
- Metrics: `http://<VM1_IP>:9100/metrics`
- (Optional) Grafana: `http://<VM1_IP>:3000` (admin/admin)
- (Optional) Prometheus: `http://<VM1_IP>:9090`

