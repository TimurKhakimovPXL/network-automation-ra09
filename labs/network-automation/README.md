# Network Automation — Flexible Multi-Domain Engine

> Dispatcher-based, idempotent configuration engine for Cisco IOS XE using RESTCONF and NETCONF.

---

## Overview

Universal automation engine for full network deployment. Desired state is declared in `changes.yaml`.
The dispatcher routes each change to the correct domain handler. The script itself never changes —
only the YAML does.

Supports complete rack deployment: interfaces, routing, switching, DHCP, and gateway redundancy.

Tested on: Cisco IOS XE ISR4200 (16.8+)

---

## Workflow

```
changes.yaml (desired state)
        │
        ▼
   automate.py (dispatcher)
        │
        ├── interface_description  → RESTCONF read → compare → NETCONF write → verify
        ├── interface_ip           → RESTCONF read → compare → NETCONF write → verify
        ├── interface_switchport   → RESTCONF read → compare → NETCONF write → verify
        ├── interface_state        → RESTCONF read → compare → NETCONF write → verify
        ├── ospf                   → RESTCONF read → compare → NETCONF write → verify
        ├── static_route           → RESTCONF read → compare → NETCONF write → verify
        ├── vlan                   → RESTCONF read → compare → NETCONF write → verify
        ├── etherchannel           → RESTCONF read → compare → NETCONF write → verify
        ├── dhcp_server            → RESTCONF read → compare → NETCONF write → verify
        ├── dhcp_relay             → RESTCONF read → compare → NETCONF write → verify
        └── hsrp                   → RESTCONF read → compare → NETCONF write → verify
                                                        │
                                                        ▼
                                                  report.json
```

---

## Repository Structure

```
labs/network-automation/
├── automate.py          # Universal entry point — run this
├── changes.yaml         # Desired state — only file you edit day to day
├── report.json          # Generated on each run, do not edit
├── requirements.txt
├── .env.example         # Copy to .env and fill in credentials
└── handlers/
    ├── interface_description.py
    ├── interface_ip.py
    ├── interface_switchport.py
    ├── interface_state.py
    ├── ospf.py
    ├── static_routes.py
    ├── vlan.py
    ├── etherchannel.py
    ├── dhcp_server.py
    ├── dhcp_relay.py
    └── hsrp.py
```

---

## Installation

```bash
cd labs/network-automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your credentials
```

---

## Configuration

Edit `changes.yaml` to declare desired state. Supported change types:

### interface_description

```yaml
- type: interface_description
  interface_type: GigabitEthernet
  interface_name: "0/0/0"
  description: RA09-L management interface
```

### interface_ip

```yaml
- type: interface_ip
  interface_type: GigabitEthernet
  interface_name: "0/0/1"
  ip: 10.199.65.17
  mask: 255.255.255.224
```

### interface_switchport

```yaml
# Access port
- type: interface_switchport
  interface_type: GigabitEthernet
  interface_name: "1/0/3"
  mode: access
  access_vlan: 92

# Trunk port
- type: interface_switchport
  interface_type: GigabitEthernet
  interface_name: "1/0/24"
  mode: trunk
  native_vlan: 99
  allowed_vlans: "91-93,99"
```

### interface_state

```yaml
- type: interface_state
  interface_type: GigabitEthernet
  interface_name: "0/0/1"
  state: up       # up | down
```

### ospf

```yaml
- type: ospf
  process_id: 1
  router_id: 172.17.9.2
  networks:
    - prefix: 172.17.9.0
      wildcard: 0.0.0.15
      area: 0
```

### static_route

```yaml
- type: static_route
  routes:
    - prefix: 0.0.0.0
      mask: 0.0.0.0
      next_hop: 10.199.65.1
      description: Default route via backbone
```

### vlan

```yaml
- type: vlan
  vlans:
    - id: 91
      name: Management
    - id: 92
      name: Data_Users
```

### etherchannel

```yaml
- type: etherchannel
  channel_id: 1
  mode: active
  protocol: lacp
  description: Uplink to distribution
  members:
    - interface_type: GigabitEthernet
      interface_name: "1/0/1"
    - interface_type: GigabitEthernet
      interface_name: "1/0/2"
```

### dhcp_server

```yaml
- type: dhcp_server
  excluded:
    - start: 172.17.9.1
      end: 172.17.9.20
  pools:
    - name: RA09-L-Data
      network: 172.17.9.16
      mask: 255.255.255.240
      default_router: 172.17.9.17
      dns_servers:
        - 10.199.64.66
      lease_days: 1
```

### dhcp_relay

```yaml
- type: dhcp_relay
  interface_type: Vlan
  interface_name: "92"
  helper_addresses:
    - 10.199.64.66
```

### hsrp

```yaml
- type: hsrp
  interface_type: GigabitEthernet
  interface_name: "0/0/0"
  group: 1
  version: 2
  priority: 110       # higher = preferred active router
  preempt: true
  virtual_ip: 172.17.9.1
```

---

## Usage

```bash
python3 automate.py
```

Reads `changes.yaml`, writes `report.json` on completion.

---

## Adding a New Domain

1. Create `handlers/<domain>.py` implementing `handle(device_params, device_name, change) -> dict`
2. Import it and register it in `HANDLERS` in `automate.py`

That's it — no other files change.

---

## Output

```json
{
  "generated_at": "2026-04-24T10:00:00",
  "total_tasks": 8,
  "success": 6,
  "already_correct": 2,
  "failed": 0,
  "results": [...]
}
```

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | Desired state already present, no change made |
| `interface_not_found` | RESTCONF returned 404 for the interface |
| `read_failed` | RESTCONF GET failed |
| `edit_failed` | NETCONF edit-config failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value |
| `unknown_type` | No handler registered for that change type |
| `missing_type` | Change entry has no type field |

---

## Known Issues Fixed — 2026-04-26

The following bugs were identified and corrected prior to hardware validation.

| File | Issue | Fix |
|---|---|---|
| `automate.py` | `device_params` used `"iosxe"` — wrong ncclient handler | Changed to `"csr"` |
| `automate.py` | `load_dotenv()` resolved from CWD — breaks if not run from repo root | Explicit path relative to script file |
| All interface handlers | `<n>` used as NETCONF key element instead of `<name>` | Replaced with `<name>` across all payloads |
| `handlers/hsrp.py` | Priority extracted without `int()` cast — type mismatch on IOS XE 16.8 | Explicit `int()` cast on extraction and comparison |
| `handlers/ospf.py` | RESTCONF key `Cisco-IOS-XE-native:ospf` never matched — OSPF module uses its own namespace | Fixed to `Cisco-IOS-XE-ospf:ospf`, confirmed across IOS XE 16.8–17.5 from YangModels repo |

The OSPF fix is the most impactful: without it, the idempotency check always fails silently and OSPF config is pushed on every run regardless of device state.

---

## Dependencies

| Package | Purpose |
|---|---|
| `ncclient` | NETCONF client |
| `requests` | RESTCONF HTTP client |
| `PyYAML` | Desired state parsing |
| `python-dotenv` | Credential loading from .env |
| `urllib3` | TLS warning suppression for self-signed certs |

---

## Technologies

- [RESTCONF (RFC 8040)](https://datatracker.ietf.org/doc/html/rfc8040)
- [NETCONF (RFC 6241)](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- YANG model: `Cisco-IOS-XE-native`

---

## Course Context

This lab is part of the **NetAcad DEVASC** (DevNet Associate) curriculum — PXL DEVNET / RA09.
