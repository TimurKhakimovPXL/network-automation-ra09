# Network Automation — Flexible Multi-Domain Engine

> Dispatcher-based, idempotent configuration engine for Cisco IOS XE using RESTCONF and NETCONF.

---

## Overview

Universal automation engine that replaces single-purpose scripts. Desired state is declared
in `changes.yaml`. The dispatcher routes each change to the correct domain handler.
The script itself never changes — only the YAML does.

Tested on: Cisco IOS XE ISR4200 (16.8+)

---

## Workflow

```
changes.yaml (desired state)
        │
        ▼
   automate.py (dispatcher)
        │
        ├── interface_description → RESTCONF read → compare → NETCONF write → verify
        ├── ospf                  → RESTCONF read → compare → NETCONF write → verify
        ├── static_route          → RESTCONF read → compare → NETCONF write → verify
        ├── vlan                  → RESTCONF read → compare → NETCONF write → verify
        └── etherchannel          → RESTCONF read → compare → NETCONF write → verify
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
    ├── ospf.py
    ├── static_routes.py
    ├── vlan.py
    └── etherchannel.py
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
      next_hop: 172.17.9.1
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
      interface_name: "0/1"
    - interface_type: GigabitEthernet
      interface_name: "0/2"
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
  "total_tasks": 3,
  "success": 2,
  "already_correct": 1,
  "failed": 0,
  "results": [...]
}
```

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | Desired state already present, no change made |
| `edit_failed` | NETCONF edit-config failed |
| `verify_mismatch` | Change applied but verification returned unexpected value |
| `read_failed` | RESTCONF GET failed |
| `unknown_type` | No handler registered for that change type |

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
