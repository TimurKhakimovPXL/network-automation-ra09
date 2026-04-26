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
├── debug/               # Generated on verify_mismatch — raw RESTCONF responses
├── requirements.txt
├── .env.example         # Copy to .env and fill in credentials
└── handlers/
    ├── _normalize.py    # Value coercion helpers (int, str, ipv4, mask, as_list, ...)
    ├── _debug.py        # Per-run RESTCONF response capture
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

> **Note:** `dhcp_relay` uses **additive** semantics. Helpers declared in YAML are added if missing,
> but helpers present on the device that are not in YAML are **not** removed. This is a deliberate
> safety choice — silently removing a stray `ip helper-address` could break DHCP for users on that
> interface. To remove a helper, do it via CLI and re-run the automation to verify desired entries.

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

## Change Ordering and Dependencies

**Order in `changes.yaml` matters.** Changes execute top-to-bottom in the order they appear.
The engine does not perform automatic dependency resolution — ordering is the operator's responsibility,
expressed declaratively in YAML. Author the layer-1/2/3 stack bottom-up:

1. `interface_description`, `interface_ip` — independent
2. `interface_state` — bring interface up after IP is assigned
3. `static_route` — depends on egress interface being up
4. `ospf` — depends on participating interfaces being addressed and up
5. `hsrp` — depends on the underlying interface
6. `dhcp_server` — depends on the gateway being live

For switches: `vlan` first, then `etherchannel` and `interface_switchport` (referencing those VLANs),
then `dhcp_relay`.

### Optional `id` and `depends_on`

To prevent cascade failures, tag changes with an `id` and declare prerequisites with `depends_on`:

```yaml
- id: c01-ip-wan
  type: interface_ip
  interface_type: GigabitEthernet
  interface_name: "0/0/1"
  ip: 10.199.65.17
  mask: 255.255.255.224

- id: c01-state-wan
  type: interface_state
  depends_on: [c01-ip-wan]
  interface_type: GigabitEthernet
  interface_name: "0/0/1"
  state: up

- id: c01-ospf
  type: ospf
  depends_on: [c01-ip-wan, c01-state-wan]
  process_id: 1
  ...
```

When the dispatcher reaches a change with `depends_on`, it checks each declared prerequisite. If any
prerequisite has a status outside `(success, already_correct)` the change is skipped with status
`skipped_due_to_dependency`. Skipped changes also propagate — anything depending on a skipped change
is itself skipped.

This means when `interface_ip` fails on a fresh device, `hsrp` and `ospf` no longer run blindly
against an interface with no address. The operator sees one root-cause failure and a list of skipped
consequences, instead of confusing secondary errors.

`id` is a free-form string scoped per device. `depends_on` accepts either a single id or a list.
Both are optional.

---

## Debug Capture

When a handler returns `verify_mismatch`, it writes the raw RESTCONF response body to
`debug/<run-timestamp>/<device>/<seq>_<change-type>_verify.json`. The file contains the HTTP status,
request URL, parsed JSON body (or first 8 KiB of text), and the change definition that produced
the mismatch.

This is the diagnostic of last resort. When a comparison fails against real hardware, the raw
response shows directly whether the device returned a single dict instead of a list, omitted a leaf
the parser expected, or normalised a value differently than expected.

To capture every read (not just mismatches), set `DEBUG_CAPTURE=1` in the environment before running:

```bash
DEBUG_CAPTURE=1 python3 automate.py
```

Useful for the first hardware run against a new platform. Disable afterwards to reduce noise.

---

## Usage

```bash
python3 automate.py
```

Reads `changes.yaml`, writes `report.json` on completion.

---

## Adding a New Domain

1. Create `handlers/<domain>.py` implementing `handle(device_params, device_name, change) -> dict`
2. Import the helpers you need:
   ```python
   from . import _normalize as norm
   from . import _debug
   ```
3. Use `norm.as_list()` at every site where you iterate a value extracted from RESTCONF JSON
4. Use `norm.normalize_*()` on both sides of every `_states_match` comparison
5. Call `_debug.capture(device_name, "<type>", "verify", response, change=change, force=True)`
   in the `verify_mismatch` branch
6. Import and register the handler in `HANDLERS` in `automate.py`

That's it — no other files change.

---

## Output

```json
{
  "generated_at": "2026-04-24T10:00:00",
  "total_tasks": 8,
  "success": 6,
  "already_correct": 2,
  "skipped": 0,
  "failed": 0,
  "results": [...]
}
```

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | Desired state already present, no change made |
| `skipped_due_to_dependency` | A `depends_on` prerequisite did not finish successfully |
| `interface_not_found` | RESTCONF returned 404 for the interface |
| `read_failed` | RESTCONF GET failed |
| `edit_failed` | NETCONF edit-config failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value — debug capture written |
| `unknown_type` | No handler registered for that change type |
| `missing_type` | Change entry has no type field |
| `invalid_input` | A required field had an invalid value (e.g. non-integer where int required) |
| `handler_exception` | Handler raised an unexpected exception — full traceback in result |

---

## Known Issues Fixed — 2026-04-26

All fixes are on `feature/flexible-automation-engine` and committed.

**Round 1 — Pre-hardware fixes:**

| File | Issue | Fix |
|---|---|---|
| `automate.py` | `device_params` used `"iosxe"` — wrong ncclient handler | Changed to `"csr"` |
| `automate.py` | `load_dotenv()` resolved from CWD — breaks if not run from repo root | Explicit path relative to script file |
| All interface handlers | NETCONF key element written as `&lt;n&gt;` instead of `&lt;name&gt;` (the actual YANG list key for interfaces) | Replaced `&lt;n&gt;...&lt;/n&gt;` with `&lt;name&gt;...&lt;/name&gt;` across all interface payloads |
| `handlers/hsrp.py` | Priority extracted without `int()` cast — type mismatch on IOS XE 16.8 | Explicit `int()` cast on extraction and comparison |
| `handlers/ospf.py` | RESTCONF key `Cisco-IOS-XE-native:ospf` never matched | Fixed to `Cisco-IOS-XE-ospf:ospf` |

**Round 2 — YANG model audit (source files verified for IOS XE 16.8.1 and 17.3.1):**

| File | Issue | Fix |
|---|---|---|
| `handlers/hsrp.py` | `xmlns="Cisco-IOS-XE-hsrp"` on `<standby>` — namespace does not exist | Removed xmlns — standby inherits native namespace on both 16.x and 17.x |
| `handlers/ospf.py` | Network element `<mask>` correct on 16.x but wrong on 17.x (`<wildcard>`) | Runtime version detection from NETCONF capabilities — branches per version |
| `handlers/dhcp_server.py` | default-router, dns-server, lease all changed structure between 16.x and 17.x | Runtime version detection — correct XML structure per version |

7 other handlers verified clean: `interface_description`, `interface_ip`, `interface_state`, `interface_switchport`, `dhcp_relay`, `etherchannel`, `vlan`, `static_routes`.

**Round 3 — Pre-hardware hardening (runtime data-shape and cascade-failure defences):**

| Area | Issue | Fix |
|---|---|---|
| All handlers | Cisco RESTCONF returns single-entry YANG lists as a dict — parser crashes on `for entry in value` | New `handlers/_normalize.py::as_list()` defensive cast at every list-iteration site |
| All handlers | Type/format drift between Cisco's representation and YAML values — `91` vs `"91"`, `"  desc  "` vs `"desc"`, zero-padded IPs | Centralised `_normalize` helpers (`normalize_int`, `normalize_str`, `normalize_bool`, `normalize_ipv4`, `normalize_mask`, `normalize_iface_name`) applied to both sides of every comparison |
| All handlers | `verify_mismatch` against real hardware impossible to debug without raw response body | New `handlers/_debug.py` writes raw RESTCONF response to `debug/<run-timestamp>/<device>/` on every mismatch. Verbose mode with `DEBUG_CAPTURE=1` |
| `automate.py` | Cascade failures — `hsrp` running on an interface whose `interface_ip` failed earlier | `depends_on` skip logic with new status `skipped_due_to_dependency`. Skipped changes propagate to their own dependents |
| `automate.py` | `handler_exception` only recorded `str(e)` — no way to find the failing line in unattended runs | `traceback.format_exc()` recorded in result dict alongside `str(e)` |
| `automate.py` | Report counters lumped skipped tasks into `failed` — read as if everything broke | `skipped` counted separately from `failed` in `report.json` |

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
