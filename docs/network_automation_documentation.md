---
title: Network Automation Project: Technical Documentation
author: Timur Khakimov
supervisor: Wim Leppens
institution: PXL University of Applied Sciences
group: DEVNET
date: 2026-05-18
status: active
tags:
  - network-automation
  - devnet
  - pxl
  - netconf
  - restconf
  - cisco-ios-xe
  - python
  - ztp
repository: https://github.com/TimurKhakimovPXL/network-automation-ra09
---

# Network Automation Project: Technical Documentation

## Table of Contents

- [[#1. Project Overview]]
- [[#2. Lab Infrastructure]]
- [[#3. Automation Engine]]
- [[#3.5 Known Issues Fixed]]
- [[#3.6 YANG Suite: Local Installation]]
- [[#3.7 YANG Model Audit: Handler Verification]]
- [[#4. Full Architecture]]
- [[#5. Infrastructure Confirmation]]
- [[#6. Installation and Usage]]
- [[#7. References]]

---

## 1. Project Overview

### 1.1 Goal

This project automates configuration of the PXL lab devices. Blank IOS XE
devices bootstrap through ZTP, after which the controller manages them over
NETCONF and RESTCONF.

The teacher currently configures all lab devices manually after each wipe. This project replaces that process with a system where devices configure themselves after boot, and ongoing changes are pushed programmatically from a central Ubuntu automation controller.

### 1.2 Scope

- **Day-0 bootstrap:** bring a wiped, unconfigured Cisco IOS XE device to a reachable, NETCONF/RESTCONF-enabled state automatically via ZTP
- **Day-N configuration:** push full desired-state configuration to 10+ devices simultaneously using a YAML-driven Python automation engine
- **Idempotency:** re-running the automation produces no unintended changes if the device is already in the desired state
- **Reporting:** every run produces a structured JSON report documenting what changed, what was skipped, and what failed
- **Optional extension:** firmware version enforcement using NETCONF-driven IOS XE image upgrades via the existing TFTP server

### 1.3 Technologies

| Technology | Role |
|---|---|
| RESTCONF (RFC 8040) | Read current device state over HTTPS using YANG models |
| NETCONF (RFC 6241) | Apply configuration changes over SSH (port 830) |
| YANG | Data model: `Cisco-IOS-XE-native` |
| Python 3.8+ | Automation scripting language |
| ncclient | Python NETCONF client library |
| requests | Python HTTP client for RESTCONF calls |
| PyYAML | Desired state file parsing |
| TFTP | Bootstrap script delivery for ZTP |
| Cisco IOS XE ZTP | On-boot auto-provisioning using DHCP option 67 |
| IOS XE Guest Shell | Python execution environment on-device for ZTP |

---

## 2. Lab Infrastructure

### 2.1 Network Topology

The PXL lab is structured around student racks **RA01 through RA10**, connected to a central backbone. Each student rack contains two routers (`C01-R01` and `C02-R01`) and two access switches (`A01-SW01` and `A02-SW01`). All routers uplink via GigabitEthernet 0/0/1 to the backbone switch `LAB-BR-A-SW08`, which connects upstream to `LAB-BR-C-R03` at `10.199.65.100`.

A Data Center segment hosts shared infrastructure services used by all racks.

### 2.2 Data Center Services

| Service | IP Address | Role |
|---|---|---|
| DHCP / DNS / NTP | `10.199.64.66` | IP assignment, name resolution, time sync |
| TFTP Server | `10.199.64.134` | ZTP bootstrap script delivery (`ztp.py`) |
| YANG Suite | `10.125.100.231:8443` | YANG model browser and NETCONF testing |
| ESXi Host | `10.199.64.37` | Virtualisation host: Ubuntu automation controller VM |

Domain: `data.labnet.local`

### 2.3 Per-Rack Addressing Scheme

> In the addressing examples, **X** is the rack number from 1 to 10. RA09
> (`X=9`) is used as the worked example.

#### WAN Connectivity (Gig 0/0/1)

Each router's uplink interface receives a static IP in the `10.199.65.0/27` range, assigned per rack:

| Device | IP Address |
|---|---|
| `LAB-RA0X-C01-R01` Gig 0/0/1 | `10.199.65.(2X-1)/27` |
| `LAB-RA0X-C02-R01` Gig 0/0/1 | `10.199.65.(2X)/27` |

**RA09 example (X=9):**

| Device | Interface | IP Address | Purpose |
|---|---|---|---|
| LAB-RA09-C01-R01 | Gig 0/0/1 | `10.199.65.117/27` | WAN uplink |
| LAB-RA09-C02-R01 | Gig 0/0/1 | `10.199.65.118/27` | WAN uplink |

#### Management Interface (Gig 0/0/0)

The management interface connects to the local rack switch on the management VLAN. The first usable host address in the management subnet is assigned to the router.

**RA09 example:** management subnet `172.17.9.0/28`, router management IP `172.17.9.2`: confirmed working against real hardware.

### 2.4 VLAN and Subnet Scheme

Each rack gets its own VLAN and subnet block based on rack number X.

#### RA0X-L (Left side)

| VLAN | Name | Subnet |
|---|---|---|
| X1 | Management | `172.17.X.0/28` |
| X2 | Data_Users | `172.17.X.16/28` |
| X3 | Voice_Users | `172.17.X.32/28` |
| X4 | Reserved | `172.17.X.48/28` |
| 99 | Native | N/A |

#### RA0X-R (Right side)

| VLAN | Name | Subnet |
|---|---|---|
| X5 | Management | `172.17.X.64/28` |
| X6 | Data_Users | `172.17.X.80/28` |
| X7 | Voice_Users | `172.17.X.96/28` |
| X8 | Reserved | `172.17.X.112/28` |
| 99 | Native | N/A |

#### IP Address Assignment Structure

| Position | Assigned to |
|---|---|
| 1st | HSRP/VRRP default gateway |
| 2nd | LAB-BR-C01-R01 |
| 3rd | LAB-BR-C02-R01 |
| 4th | LAB-RA0X-A01-SW01 (mgmt subnet only) |
| 5th | LAB-RA0X-A01-SW02 (mgmt subnet only) |
| 6th | LAB-RA0X-A01-SW03 (mgmt subnet only) |
| .21–.30 | DHCP pool: Data_Users (RA0X-L only) |
| .85–.94 | DHCP pool: Data_Users (RA0X-R only) |

### 2.5 Physical Patching

| Device | Router Port | Switch Port |
|---|---|---|
| LAB-RA09-C01-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/17 |
| LAB-RA09-C02-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/18 |

---

## 3. Automation Engine

### 3.1 Repository Structure

```
network-automation-ra09/
├── README.md                          # Repository index
├── dispatch.py                        # Single registration site for HANDLERS (shared registry)
│
├── intent/                            # GitOps control surface (production)
│   ├── class_state.yaml               # Supervisor edits this file
│   └── profiles/                      # Reusable device-state declarations
│
├── infra/                             # Hardware as code (production)
│   ├── inventory.yaml                 # Device catalogue (single source)
│   └── dhcp_reservations.yaml         # MAC → IP bindings
│
├── reconciler/                        # Continuous reconciliation loop (production)
│   ├── reconciler.py                  # systemd entry point on lab-dc-h-vm09
│   ├── state_resolver.py              # intent + inventory → target state
│   ├── git_watcher.py                 # Git pull and SHA tracking
│   └── systemd/
│       └── network-reconciler.service
│
├── scripts/                           # Operational tooling
│   ├── apply_dhcp_reservations.py     # Renders Windows DHCP config from inventory
│   └── manual_reconcile.py            # One-shot reconcile with --dry-run
│
├── docs/                              # System documentation
│   ├── architecture.md
│   ├── oob_network_design.md
│   ├── operator_guide.md
│   ├── network_automation_documentation.md   # This file
│   └── troubleshooting/
│
└── labs/                              # Dev and historical area
    ├── ra09-interface-description/    # Day-N: interface description automation (tested)
    │   ├── automate_interface_desc.py
    │   ├── changes.yaml
    │   ├── report.json
    │   ├── requirements.txt
    │   └── README.md
    ├── network-automation/            # Day-N: flexible multi-domain engine (invoked by reconciler)
    │   ├── README.md
    │   ├── automate.py                # Single-device CLI debug surface
    │   ├── changes.yaml               # Input for the CLI debug surface
    │   ├── report.json                # Generated by the CLI debug surface
    │   ├── requirements.txt
    │   └── handlers/
    │       ├── __init__.py
    │       ├── _normalize.py          # Value coercion (int, str, ipv4, mask, as_list, ...)
    │       ├── _debug.py              # Per-run RESTCONF response capture
    │       ├── _xml.py                # XML escape + interface-type whitelist
    │       ├── interface_description.py
    │       ├── interface_ip.py
    │       ├── interface_switchport.py
    │       ├── interface_state.py
    │       ├── ospf.py
    │       ├── static_routes.py
    │       ├── vlan.py
    │       ├── etherchannel.py
    │       ├── dhcp_server.py
    │       ├── dhcp_relay.py
    │       └── hsrp.py
    └── ztp/                           # Day-0: Zero Touch Provisioning bootstrap
        ├── ztp.py
        └── README.md
```

The `intent/`, `infra/`, `reconciler/`, and `scripts/` directories form the production
deployment path: the GitOps loop running on `lab-dc-h-vm09`. The `labs/` directory is the
dev and historical area, holding the engine and earlier single-domain and Day-0 work.
The reconciler invokes it in production.

### 3.2 Lab: ra09-interface-description (Day-N, tested)

This lab implements the core automation pattern for the project: a YAML-driven, idempotent configuration push using RESTCONF for reads and NETCONF for writes. Developed and tested against real Cisco IOS XE hardware in the RA09 rack. This lab is the direct origin of the flexible engine in section 3.3: the pattern is identical, the scope is extended.

#### 3.2.1 Automation Workflow

1. Read current interface state from the device via RESTCONF GET
2. Compare the actual description against the desired description from YAML
3. Skip the change if the device is already in the desired state (idempotent)
4. Apply the change via NETCONF `edit-config` targeting the running configuration
5. Verify the change by performing a second RESTCONF GET
6. Record the outcome (`success`, `already_correct`, or error) in `report.json`

#### 3.2.2 File: `automate_interface_desc.py`

| Function | Responsibility |
|---|---|
| `load_yaml_file()` | Reads and parses the `changes.yaml` desired state file |
| `build_device_params()` | Constructs ncclient connection parameters from device config |
| `restconf_get_interface()` | Reads a single interface via RESTCONF GET with URL-encoded interface name |
| `extract_description()` | Parses the description field from the RESTCONF JSON response |
| `netconf_edit_description()` | Applies the interface description via NETCONF `edit-config` with inline XML payload |
| `process_change()` | Orchestrates the full read-compare-write-verify cycle for one interface |
| `write_report()` | Serialises results to `report.json` |
| `main()` | Entry point: loads YAML, iterates devices and changes, aggregates report |

#### 3.2.3 YANG Model and RESTCONF URL

```
GET https://{host}/restconf/data/
    Cisco-IOS-XE-native:native/interface/
    {interface_type}={url_encoded_interface_name}
```

The interface name is URL-encoded using `urllib.parse.quote()` to handle forward slashes in identifiers such as `0/0/0`.

#### 3.2.4 NETCONF Payload

```xml
<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
    <interface>
      <GigabitEthernet>
        <name>0/0/0</name>
        <description>RA09-L management interface</description>
      </GigabitEthernet>
    </interface>
  </native>
</config>
```

#### 3.2.5 Error Handling

| Status | Meaning |
|---|---|
| `success` | Change applied and verified via RESTCONF |
| `already_correct` | Desired state already present, no change made |
| `interface_not_found` | RESTCONF returned HTTP 404 for the interface |
| `read_failed` | RESTCONF GET failed (connection, timeout, auth) |
| `edit_failed` | NETCONF `edit-config` RPC failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value |

#### 3.2.6 File: `changes.yaml`

```yaml
devices:
  - name: LAB-RA09-C01-R01
    host: 172.17.9.2
    changes:
      - interface_type: GigabitEthernet
        interface_name: "0/0/0"
        description: RA09-L management interface
```

> Credentials come from `LAB_USER` and `LAB_PASS` in `.env`, not from
> `changes.yaml`. Copy `.env.example` to `.env` before running the script.

#### 3.2.7 File: `report.json`

```json
{
  "generated_at": "2026-04-21T09:52:50",
  "total_tasks": 1,
  "success": 0,
  "already_correct": 1,
  "failed": 0,
  "results": [
    {
      "device_name": "LAB-RA09-C01-R01",
      "interface_type": "GigabitEthernet",
      "interface_name": "0/0/0",
      "desired_description": "RA09-L management interface",
      "changed": false,
      "verified": true,
      "status": "already_correct"
    }
  ]
}
```

### 3.3 Lab: ztp (Day-0)

`ztp.py` is the Zero Touch Provisioning script delivered to a wiped IOS XE
device during its first boot.

#### 3.3.1 How It Works

When a wiped device boots, IOS XE has no startup config. It enters ZTP mode, sends a DHCP discover, and receives an IP address. If the DHCP server includes option 67 pointing to `tftp://10.199.64.134/ztp.py`, the device downloads and executes that script inside Guest Shell.

#### 3.3.2 Device identification

The script identifies the device automatically from the DHCP-assigned IP using the PXL addressing scheme. No MAC address list is required in the script itself: MACs only need to be configured in the DHCP server for static reservations.

```
DHCP IP 172.17.X.2  → LAB-RA0X-C01-R01  (rack X, left side)
DHCP IP 172.17.X.66 → LAB-RA0X-C02-R01  (rack X, right side)
```

The rack number `X` is the third octet of the address, so the same `ztp.py`
file can identify all 20 rack routers. MAC reservations remain on the DHCP
server rather than in the script.

#### 3.3.3 What the Script Configures

- Hostname derived from rack and side
- Enable secret and local admin credentials
- Management interface IP (static, matching DHCP assignment)
- Default route via rack gateway
- `ip domain-name data.labnet.local`
- RSA 2048-bit key (with IOS XE 16.8 compatibility check)
- `ip ssh version 2`
- VTY lines: SSH only
- `no ip http server`: disables plaintext HTTP on port 80
- `ip http secure-server`: enables HTTPS on port 443 (required for RESTCONF)
- `netconf-yang`
- `restconf`
- `write memory`

#### 3.3.4 IOS XE Version Compatibility

| Feature | 16.8 | 17.3 |
|---|---|---|
| ZTP | Yes | Yes |
| Guest Shell | Yes | Yes |
| `cli` module | Yes | Yes |
| NETCONF / RESTCONF | Yes | Yes |
| RSA key gen in Guest Shell | Sometimes unreliable | Stable |

The script handles the 16.8 RSA issue by checking whether a key already exists before attempting generation. If generation fails, it logs a warning and continues: NETCONF and RESTCONF work without SSH being fully configured.

#### 3.3.5 Log File

Every step is logged to `bootflash:ztp.log` with timestamps. This file persists across reboots. If ZTP fails, connect via console and run:

```
more bootflash:ztp.log
```

### 3.4 Lab: network-automation (Day-N)

This engine extends the interface-description lab with a dispatcher and 11
domain handlers. `changes.yaml` provides input for direct CLI runs; production
input is rendered from intent and profile files by the reconciler.

#### 3.4.1 Architecture

```
(a) Reconciler (production)              (b) CLI debug
─────────────────────────────            ──────────────────
intent/class_state.yaml                  changes.yaml
intent/profiles/*.yaml                          │
infra/inventory.yaml                            │
        │                                       │
        ▼                                       ▼
  state_resolver                          automate.py
        │                                       │
        ▼                                       │
  per-device change list                        │
        │                                       │
        └──────────────────┬────────────────────┘
                           ▼
                  ┌──────────────────┐
                  │ HANDLERS         │  ← dispatch.py (repo root)
                  │ (single registry)│
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ handlers/        │
                  │   (11 domains)   │
                  └──────────────────┘
                           │
            RESTCONF read → compare → NETCONF write → verify
```

#### 3.4.2 Handler Registry

Each domain is a self-contained module in `handlers/`. Adding a new domain requires two steps: create the handler file, register it in `HANDLERS` in `dispatch.py` at the repo root. No other files change.

| Handler | Domain | YANG path |
|---|---|---|
| `interface_description` | Interface descriptions | `native/interface/{type}={name}` |
| `interface_ip` | IPv4 address assignment | `native/interface/{type}={name}/ip/address` |
| `interface_switchport` | Access / trunk mode and VLANs | `native/interface/{type}={name}/switchport` |
| `interface_state` | Shutdown / no shutdown | `native/interface/{type}={name}/shutdown` |
| `ospf` | OSPF process, router-id, networks | Revision-selected: legacy `native/router/Cisco-IOS-XE-ospf:ospf={id}` or wrapped `native/router/Cisco-IOS-XE-ospf:router-ospf/ospf/process-id={id}` |
| `static_route` | IPv4 static routes | `native/ip/route` |
| `vlan` | VLAN definitions on switches | `native/vlan/vlan-list` |
| `etherchannel` | Port-channel and member interfaces | `native/interface/Port-channel={id}` |
| `dhcp_server` | DHCP pools, exclusions, DNS, gateway | `native/ip/dhcp/pool={name}` |
| `dhcp_relay` | ip helper-address on SVIs | `native/interface/{type}={name}/ip/helper-address` |
| `hsrp` | Gateway redundancy | `native/interface/{type}={name}/standby` |

#### 3.4.3 Idempotency and Error Handling

Every handler follows the same four-step cycle: read current state via RESTCONF, compare against desired state, write only if a delta exists via NETCONF, verify via a second RESTCONF read. A failure in one task is recorded in `report.json` and the run continues: no single device failure aborts the rest.

**Exception: `dhcp_relay` uses additive semantics:**
The `dhcp_relay` handler adds missing helper addresses but does not remove
undeclared ones. Remove an unwanted helper manually, then run the handler again
to verify the intended entries.

#### 3.4.4 Status Values

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | Desired state already present, no change made |
| `skipped_due_to_dependency` | A `depends_on` prerequisite did not finish successfully: see 3.4.5 |
| `interface_not_found` | RESTCONF returned 404 |
| `read_failed` | RESTCONF GET failed |
| `edit_failed` | NETCONF edit-config failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value: debug capture written, see 3.4.6 |
| `unknown_type` | No handler registered for that change type |
| `missing_type` | Change entry has no type field |
| `invalid_input` | A required field had an invalid value (e.g. non-integer where integer required) |
| `handler_exception` | Handler raised an unexpected exception: full traceback recorded in result |

#### 3.4.5 Change Ordering and Dependencies

**Order in `changes.yaml` is significant.** Changes execute in the order they appear in `device.changes`. The engine does not perform automatic dependency resolution; ordering is the operator's responsibility, expressed declaratively in YAML. This is the same model Ansible playbooks use, and is the right one for network configuration: the operator can reason about it, and there's no surprise from a solver picking a different order than expected.

The canonical layer-bottom-up order for an IOS XE router is:

1. `interface_description`: does not depend on anything else
2. `interface_ip`: does not depend on anything else
3. `interface_state`: depends on `interface_ip` (bringing an interface up before assigning its address risks a transient routing flap)
4. `static_route`: depends on the egress interface being up
5. `ospf`: depends on participating interfaces having IPs and being up
6. `hsrp`: depends on the underlying interface being addressed and described
7. `dhcp_server`: depends on the gateway being live (HSRP virtual IP for redundant designs)

For switches: `vlan` first, then `etherchannel` and `interface_switchport` (both of which can reference the VLANs created above), then `dhcp_relay` (which references SVI interfaces).

**Optional `id` and `depends_on` fields prevent cascade failures.**

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
```

When `depends_on` is present, the dispatcher checks each declared prerequisite before invoking the handler. If any prerequisite has a status outside `(success, already_correct)` the change is skipped and recorded with status `skipped_due_to_dependency`. The skip itself is also recorded as a non-success status, so any subsequent change depending on the skipped change will also be skipped: failures cascade only as far as the dependency chain.

`id` is a free-form string, scoped per device. `depends_on` accepts either a
single id or a list. Both are optional. Changes without an `id` still run in
document order, but another change cannot reference them as a prerequisite.

If `interface_ip` fails, dependent HSRP and OSPF tasks are skipped. The report
shows the original error and identifies the tasks that were not attempted.

#### 3.4.6 Debug Capture

When a handler returns `verify_mismatch`, it writes the raw RESTCONF response body to a file under `debug/<run-timestamp>/<device-name>/<seq>_<change-type>_verify.json`. The file contains the HTTP status, the request URL, the parsed JSON body (or first 8 KiB of text if not JSON), and a copy of the change definition that produced the mismatch.

The raw response helps diagnose list-versus-dict responses, missing leaves,
unexpected key names, and device-side value formatting.

Set `DEBUG_CAPTURE=1` to capture every read while testing a new platform. Disable
it afterwards to avoid unnecessary files. Capture errors are logged but do not
change the handler result.

#### 3.4.7 Value Normalisation

Cisco IOS XE returns RESTCONF and NETCONF values in shapes that do not always
match `changes.yaml`. A comparison can fail when the device returns `91` as an
integer and YAML supplies `"91"` as a string. It can also fail when the device
omits a YANG default that is written explicitly in YAML.

`handlers/_normalize.py` contains the conversion rules. Both the RESTCONF value
and the YAML value pass through the same helper before comparison.

| Helper | Purpose |
|---|---|
| `normalize_int(value)` | Coerce to int. `"91"` and `91` both → `91`. Returns `None` for non-numeric input |
| `normalize_str(value)` | Coerce to stripped string. `"  desc  "` → `"desc"` |
| `normalize_bool(value)` | Accept `True`/`"true"`/`"1"`/`1`. Returns `None` for unrecognised input: distinct from `False` |
| `normalize_ipv4(value)` | Validate and canonicalise dotted-decimal. Rejects zero-padded octets per CVE-2021-29921 |
| `normalize_mask(value)` | Canonicalise to dotted-decimal. Accepts both `"24"` and `"255.255.255.0"` |
| `as_list(value)` | Coerce single-dict to list-of-one. Handles Cisco's RESTCONF quirk where a YANG list with exactly one entry returns as a dict instead of a list |
| `normalize_iface_name(value)` | Strip type prefix. `"GigabitEthernet0/0/0"` → `"0/0/0"` |

Cisco sometimes returns a one-entry YANG list as a mapping. Handlers use
`as_list()` before iterating those values so one-entry and multi-entry responses
follow the same path.


### 3.5 Validation findings

The following issues were found during code review and the first hardware runs.
All fixes described here are now on `main`.

#### 3.5.1 NETCONF Key Element: `<n>` vs `<name>`

**Affected files:** all handlers in `handlers/` except `ospf.py`, `static_routes.py`, `vlan.py`, `dhcp_server.py`

The YANG model `Cisco-IOS-XE-native` uses `<name>` as the list key element for interface identification in NETCONF payloads. The flexible engine handlers were incorrectly using `<n>`. IOS XE may silently accept XML with unrecognised elements and return `<ok/>` without writing anything to the running config: meaning the script would report `success` for a change that never applied.

Corrected payload (all interface handlers):

```xml
<GigabitEthernet>
  <name>0/0/0</name>
  <description>RA09-L management interface</description>
</GigabitEthernet>
```

This is consistent with the reference implementation in `ra09-interface-description/automate_interface_desc.py`, which was tested against real hardware.

#### 3.5.2 ncclient Device Handler: `iosxe` vs `csr`

**Affected file:** `automate.py`

ncclient uses a `device_params` dict to select an internal handler class that applies device-specific NETCONF framing workarounds. The dispatcher was using `{"name": "iosxe"}`. The correct value for Cisco IOS XE is `{"name": "csr"}`: named after the CSR1000v, the original IOS XE platform in ncclient's codebase.

On IOS XE 16.8, the `csr` handler correctly negotiates the `]]>]]>` NETCONF 1.0 message delimiter. The `iosxe` alias has less field testing across older versions and can cause framing inconsistencies. The reference implementation uses `"csr"` and works.

#### 3.5.3 `load_dotenv()` Path Resolution

**Affected file:** `automate.py`

Bare `load_dotenv()` resolves `.env` from the current working directory at runtime. If the script is invoked from any directory other than the repo root, credentials silently fail to load and the script exits with an error. Fixed to use an explicit path relative to the script file itself:

```python
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")
```

This mirrors the pattern in `automate_interface_desc.py` and works regardless of invocation directory.

#### 3.5.4 HSRP Priority Type Consistency

**Affected file:** `handlers/hsrp.py`

On some IOS XE 16.8 builds, numeric YANG fields are returned as strings in RESTCONF JSON responses. The `_extract_hsrp()` function was returning `priority` as whatever type the JSON contained, while the desired state dict always produced an `int` from YAML. A type mismatch (`"110" != 110`) would cause the handler to always detect a delta and push HSRP config on every run even when the device was already correctly configured.

Fixed by applying an explicit `int()` cast on both the extracted value and the desired value.

---

#### 3.5.5 Pre-Hardware Validation Checklist

Before running the flexible engine against real hardware, confirm the following from the automation controller:

**1. Verify ncclient connects and inspect device capabilities:**

```python
from ncclient import manager
from dotenv import load_dotenv
import os

load_dotenv()

m = manager.connect(
    host="172.17.9.2", port=830,
    username=os.environ["LAB_USER"],
    password=os.environ["LAB_PASS"],
    hostkey_verify=False,
    device_params={"name": "csr"},
    allow_agent=False, look_for_keys=False,
)
for cap in m.server_capabilities:
    print(cap)
m.close_session()
```

**2. Check datastore capabilities:**

Look for `urn:ietf:params:netconf:capability:candidate:1.0` and
`:writable-running`. The shared transaction helper selects candidate when it is
advertised and otherwise writes to running.

**3. Confirm the OSPF model revision:**

The handler reads the advertised `Cisco-IOS-XE-ospf` revision. Revisions before
2020-07-01 use the legacy flat path; later revisions use the wrapped
`router-ospf/ospf/process-id` path.


#### 3.5.6 OSPF RESTCONF JSON Key

> **Superseded 2026-05-18: see §3.5.9.** The wrapped/augmented schema
> introduced in commit `974e38c` moved the RESTCONF read path off
> `native/router/ospf={id}` entirely. The top-level JSON key on the
> wrapped path is `Cisco-IOS-XE-ospf:process-id`, not
> `Cisco-IOS-XE-ospf:ospf`. The fix described in this subsection was
> correct against the flat path but is no longer the read path the
> handler uses. Retained for the hardening narrative.

**Affected file:** `handlers/ospf.py`

The `_extract_ospf_state()` function was reading the RESTCONF response using the wrong JSON key:

```python
# Wrong: always returned empty dict
ospf = data.get("Cisco-IOS-XE-native:ospf", {})

# Correct: matches the OSPF module namespace
ospf = data.get("Cisco-IOS-XE-ospf:ospf", {})
```

**Root cause:** OSPF configuration in IOS XE is defined in the augmenting module `Cisco-IOS-XE-ospf` with namespace `http://cisco.com/ns/yang/Cisco-IOS-XE-ospf`. When RESTCONF returns data from a path that resolves into an augmenting module, the JSON key uses that module's namespace: not the native namespace. The RESTCONF GET path `native/router/ospf={id}` resolves into the OSPF augmentation, so the top-level key is `Cisco-IOS-XE-ospf:ospf`.

This was confirmed by inspecting `Cisco-IOS-XE-ospf.yang` directly from the YangModels GitHub repository across IOS XE versions `1681` (16.8.1), `1693` (16.9.3), `1711` (17.1.1), `1731` (17.3.1), and `1751` (17.5.1). The namespace is identical across all versions: no version branching is needed.

**Impact without fix:** `_extract_ospf_state()` always returned an empty dict. The handler always concluded OSPF was not configured and pushed a write on every run, making OSPF non-idempotent. The write itself was correct (NETCONF namespace was already right) but the read-compare phase was broken.

The docstring was also corrected from `Cisco-IOS-XE-ospf-oper / Cisco-IOS-XE-native` to `Cisco-IOS-XE-ospf` with the explicit namespace URI.

#### 3.5.7 Pre-hardware checks: normalisation, list shapes, and dependencies

**Affected files:** all 11 handlers, `automate.py`, plus two new modules `handlers/_normalize.py` and `handlers/_debug.py`.

Code review identified three runtime cases that schema validation does not
cover: RESTCONF list-versus-dict responses, representation differences between
Cisco data and YAML, and dependent tasks running after a prerequisite failed.

These were addressed in five coordinated changes:

1. **`handlers/_normalize.py` (new module).** Centralised value-normalisation helpers: `normalize_int`, `normalize_str`, `normalize_bool`, `normalize_ipv4`, `normalize_mask`, `as_list`, `normalize_iface_name`. Every `_extract_*` parser and every desired-state builder now passes values through these helpers before comparison. See section 3.4.7.

2. **`as_list()` at every list-iteration site.** Cisco RESTCONF may return a
   single-entry YANG list as a mapping. The helper is used by OSPF, VLAN, static
   route, DHCP, relay, and HSRP parsers so both response shapes are accepted.

3. **`depends_on` skip logic.** Both entry points track per-device task outcomes by `id` and skip any later change whose declared prerequisites did not finish in `(success, already_correct)`. Skipped changes record a `skipped_due_to_dependency` status with the unmet prerequisite list, and propagate the skip to anything depending on them. Prevents `hsrp` running on an interface whose `interface_ip` failed earlier, and similar cascade hazards. See section 3.4.5. The shared implementation lives in `dispatch.py` (see §3.5.10): at the time of this round it was duplicated inline in `automate.py`.

4. **Tracebacks in `handler_exception` results.** When a handler raises,
   `automate.py` records `traceback.format_exc()` alongside the exception text.

5. **`handlers/_debug.py`.** A `verify_mismatch` captures the RESTCONF response
   under `debug/<run-timestamp>/<device>/`. See section 3.4.6.

Together, these changes keep failures local to their dependency chain and leave
enough detail in the report and debug directory to reproduce parser problems.

#### 3.5.8 `report.json` Schema Extension

**Affected file:** `automate.py`

The report aggregator now distinguishes `skipped` from `failed` in the top-level counters:

```json
{
  "total_tasks":     12,
  "success":         9,
  "already_correct": 1,
  "skipped":         2,
  "failed":          0
}
```

Previously, any status outside `(success, already_correct)` was lumped into `failed`. With dependency-aware skipping, this conflated genuine failures with consequences-of-failures, making it harder to read the report at a glance. The new `skipped` counter holds anything with status `skipped_due_to_dependency`; `failed` continues to count real errors.

#### 3.5.9 Round 4: OSPF Hardware Validation (2026-05-18)

**Affected file:** `handlers/ospf.py`. Historical commits: `56a0ba7`,
`974e38c`, and `c69e7a7`.

First hardware-validated routing-protocol convergence surfaced three
bugs in sequence: only the first identifiable from code review alone.
The first issue was visible in code review. The next two became clear after
inspecting the augmenting-module hierarchy on the device.

**Issue 1: model-revision-driven element selection.** The handler
originally branched between `<mask>` and `<wildcard>` based on IOS XE
release number (16.x vs 17.x). On `LAB-R11-C01-R01` (17.3.4a) this
returned the wrong choice because the device ships the `2020-07-01`
revision of `Cisco-IOS-XE-ospf`: a release-vs-revision mismatch that
release-number heuristics cannot catch. Replaced with
`_get_ospf_model_revision()` which extracts the revision from the
NETCONF `<hello>` capability advertisement at runtime (regex against
`Cisco-IOS-XE-ospf?module=Cisco-IOS-XE-ospf&revision=(\d{4}-\d{2}-\d{2})`).
Commit `56a0ba7`.

**Issues 2 and 3: wrong YANG container hierarchy.** The RESTCONF read
URL (`native/router/ospf={id}`) targeted the legacy flat schema. The
device's 2020-07-01 model wraps OSPF in an *augmenting* container:
`Cisco-IOS-XE-ospf:router-ospf` under `<native>/<router>`, with `ospf`
as a child container and `process-id` as the keyed list inside it.
Symptoms:

- Read returned `{"errors": [{"error-message": "uri keypath not found"}]}`,
  surfaced as `read_failed` / `verify_failed`.
- Write returned bare `<ok/>` (no warnings). Scalar leaves
  (`process-id`, `router-id`) landed via the device's CLI-translation
  layer, but the structured `<network>` list was silently dropped: the
  parser cannot map structured lists across schema-shape mismatches the
  way it can scalar leaves. RESTCONF GET on the wrapped path confirmed
  `process-id=1` existed with `router-id` set and zero networks.

Diagnostic technique that resolved both: GET the broadest path
(`.../Cisco-IOS-XE-native:native/router`) without keys, inspect the
actual response structure. Captured in
`docs/troubleshooting/restconf-keypath-debugging.md`.

Fix (commit `974e38c`):

- `RESTCONF_BASE` rewritten to
  `.../router/Cisco-IOS-XE-ospf:router-ospf/ospf/process-id={process_id}`.
- `_extract_ospf_state` updated to look for `Cisco-IOS-XE-ospf:process-id`
  at the JSON top level. RFC 8040 specifies a single-element list for
  keyed-list GET; Cisco returns a dict instead. `norm.as_list()`
  handles both shapes.
- `_netconf_edit` payload restructured to nest `<process-id>` inside
  `<ospf>` inside `<router-ospf xmlns="...Cisco-IOS-XE-ospf">` inside
  `<router>` inside `<native>`. The `<network>` child element layout
  itself was already correct from the first investigation; the problem
  was the missing parent container.

**Issue 4: inverted mask-vs-wildcard cutoff.** After commit `974e38c`
the device rejected the edit-config with `expected tag: wildcard, got
tag: mask`. The 2020-11-01 cutoff used in the first fix had the wrong direction:
it reflected field evidence from the flat-schema write path, where the
device's CLI translation layer expected `<mask>`. Once the handler uses
the augmenting container, CLI translation is no longer involved: the
actual YANG schema takes over, and the augmenting `Cisco-IOS-XE-ospf`
module is `<wildcard>`-based at revision 2020-07-01 and every later
revision. Commit `c69e7a7` temporarily forced the wrapped path to use
`<wildcard>`. The current implementation replaces that workaround with
separate legacy and wrapped schema builders selected from the advertised
revision. It uses `<mask>` for the legacy flat schema and `<wildcard>` for the
wrapped schema.

**Post-fix verification on `LAB-R11-C01-R01`.** First reconcile loop
after commit `c69e7a7`: ospf task `status: success`, `changed: true`,
`verified: true`. Second loop: `status: already_correct`,
`changed: false`. RESTCONF GET on the wrapped path returns both
declared networks (192.168.11.1/32 and 192.0.2.0/30) under area 0.
Device-level status: `converged`.

**Outstanding follow-up.** `_get_ospf_model_revision` opens a dedicated
NETCONF session per OSPF task to read capabilities (~1–2s per task).
Acceptable in the lab; flagged for future cleanup (cache the revision
per device, or read it once when the device first becomes reachable).

#### 3.5.10 Round 5: Dependency Cascade Unification (2026-05-20)

**Affected files:** `dispatch.py`, `reconciler/reconciler.py`,
`labs/network-automation/automate.py`. Historical commit: `7c54ba3`.

Profiles define dependencies by
task `id` (`depends_on: gi001-ip`), and `automate.py` had always
implemented it that way. `reconciler/reconciler.py::apply_changes_to_device`
implemented it by **change type** instead: tracking a set called
`failed_types` and looking for `depends_on` strings inside it. The
dependency cascade did not run in production: a failing `interface_ip`
task with `id: gi001-ip` would not block its dependent `interface_state`
or `ospf` tasks, because the literal `"gi001-ip"` never matches
`"interface_ip"` in a set of change-type strings.

Fixes:

1. **Dependency model is ID-only.** Reconciler updated to track per-device
   task outcomes by `id`, matching profile authoring and `automate.py`
   behaviour.
2. **Status name unified.** The reconciler had been emitting
   `"skipped_depends_on"` while `automate.py` emitted
   `"skipped_due_to_dependency"`. Operator tooling now sees one
   consistent name in reports from either entry point.
3. **Shared dependency logic centralised in `dispatch.py`**: the
   neutral file both entry points already imported `HANDLERS` from.
   New helpers: `SUCCESS_STATUSES`, `SKIPPED_STATUS`,
   `check_dependencies()`, `record_outcome()`. Eliminated the
   duplicate-and-diverged implementations.
4. **Reconciler docstring rewritten** to describe the actual (ID-based)
   model instead of the previously documented (incorrect) type-based one.

Verified with a synthetic cascade test: fail → skip cascade,
`already_correct` counts as success, both single-string and list forms
of `depends_on` resolve correctly. Live verification deferred: all
current production tasks are idempotent. The cascade can be tested by adding a
malformed prerequisite to a test profile.

#### 3.5.11 Round 6: DHCP Server YANG Shape, EtherChannel Protocol, OSPF Docstring (2026-05-20)

**Affected files:** `labs/network-automation/handlers/dhcp_server.py`,
`labs/network-automation/handlers/etherchannel.py`,
`labs/network-automation/handlers/ospf.py`. Historical commit: `e348176`. It
also added `tests/`
with 31 pure-function tests.

Three handler fixes verified against vendored Cisco IOS XE YANG
modules (`yang/ios-xe-1731/`, `yang/ios-xe-1681/`) and the upstream
`Cisco-IOS-XE-ethernet` module.

**dhcp_server.py: three confirmed schema bugs on 17.x:**

1. `<network>` payload was missing the `<primary-network>` wrapper.
   Per `yang/ios-xe-1731/Cisco-IOS-XE-dhcp.yang` line 1141, 17.x wraps
   `<number>` and `<mask>` inside `<primary-network>`. The handler was
   emitting them directly, which is the 16.x shape. Same bug applied
   to the RESTCONF parser, which read `network.number / network.mask`
   instead of `network["primary-network"]["number"|"mask"]`.
2. `<excluded-address>` for ranges was missing the
   `<low-high-address-list>` wrapper. Per the same YANG file line 657,
   17.x defines `excluded-address` as a container holding multiple
   list children (`low-address-list`, `low-high-address-list`,
   plus VRF variants).
3. `<pool>` and `<excluded-address>` were emitted without the
   `Cisco-IOS-XE-dhcp` namespace. `Cisco-IOS-XE-dhcp` is a standalone
   module (not a submodule of native) augmenting
   `/ios:native/ios:ip/ios:dhcp`, so its descendants must declare the
   augmenting namespace.

Also added `_validate_change()` to reject malformed IPv4/mask/excluded
ranges with `status=invalid_input` before any device I/O, and rewrote
the module docstring to document the 17.x vs 16.x shape differences
and that the 16.x branch has not been exercised live.

**etherchannel.py: three issues confirmed against
Cisco-IOS-XE-ethernet.yang (1731):**

1. `protocol: lacp/pagp` was accepted in profiles but never written.
   Per the ethernet module lines 258–302, `<channel-group>` and
   `<channel-protocol>` are siblings inside
   `config-interface-ethernet-grouping`. Handler now emits
   `<channel-protocol xmlns="...ethernet">lacp|pagp</channel-protocol>`
   next to `<channel-group>` when protocol is lacp/pagp; for
   `protocol: none` it omits `<channel-protocol>` and forces the
   effective mode to `on` (static channel).
2. No mode/protocol consistency check. `_validate_change` now enforces
   `lacp ↔ {active, passive}`, `pagp ↔ {auto, desirable}`, `none ↔ on`,
   and rejects inconsistent combinations with `invalid_input`.
3. Verification only checked the Port-channel description. Added
   `_verify_members()` which RESTCONF-GETs each member and verifies
   the channel-group number, mode, and channel-protocol (when set).
   `verify_mismatch` now returns the per-member diagnostic list
   instead of a vague description-only check.

**ospf.py: docstring header only.** Code already used the augmented
`native/router/Cisco-IOS-XE-ospf:router-ospf/ospf/process-id={id}` path
since §3.5.9; the docstring still claimed the legacy
`native/router/ospf={id}` path. Behaviour unchanged.

**Tests added:** `tests/` directory with 31 pure-function tests covering
DHCP 17.x pool XML (primary-network wrapper), DHCP 17.x excluded-address
(low-high-address-list wrapper), DHCP 17.x default-router/dns-server/lease
container shapes, DHCP 16.x flat shapes (regression guard), DHCP RESTCONF
parser handling both shapes, DHCP input validation, EtherChannel member
XML (channel-protocol with ethernet namespace on both leaves),
EtherChannel mode/protocol matrix validation, EtherChannel member
RESTCONF parser handling module-qualified and bare keys. All 31 pass.
Run with `python -m pytest tests/ -v` from the repo root.

Live verification deferred for both DHCP server and EtherChannel: no
current profile exercises either handler. Run a live check before adding either
one to a class profile.


### 3.6 YANG Suite: Local Installation

YANG Suite is Cisco's open-source tool for browsing YANG models and testing NETCONF/RESTCONF queries against real devices. It was installed locally on the automation controller (WSL2/Ubuntu) using Podman during the 2026-04-26 session.

#### 3.6.1 Installation

```bash
git clone https://github.com/CiscoDevNet/yangsuite.git
cd yangsuite/docker

# Generate self-signed SSL certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/nginx-self-signed.key \
  -out nginx/nginx-self-signed.cert \
  -subj "/C=BE/ST=Flanders/L=Hasselt/O=PXL/CN=localhost"

# Create environment file
cat > yangsuite/setup.env << 'EOF'
DJANGO_SUPERUSER_USERNAME=replace_with_admin_username
DJANGO_SUPERUSER_PASSWORD=replace_with_strong_password
DJANGO_SUPERUSER_EMAIL=replace_with_admin_email
DJANGO_SETTINGS_MODULE=yangsuite.settings.production
SECRET_KEY=replace_with_random_secret_key
DJANGO_ALLOWED_HOSTS=localhost 127.0.0.1
EOF

# Fix compose file to use local nginx image
sed -i 's/image: nginx:latest/image: localhost\/nginx:latest/' docker-compose.yml

# Allow unprivileged ports (WSL2)
echo 'net.ipv4.ip_unprivileged_port_start=0' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

# Build and start
podman-compose build nginx
podman-compose up -d --no-build

# Create admin user (first run only)
podman exec -it docker_yangsuite_1 bash -c \
  "cd /usr/local/lib/python3.10/dist-packages/yangsuite && \
   python3 manage.py createsuperuser"
```

#### 3.6.2 Access

```
URL:      https://localhost:8443
Username: value set during installation
Password: value set during installation
```

Accept the self-signed certificate warning in the browser.

#### 3.6.3 Loading IOS XE YANG Models

In YANG Suite, go to **Setup → YANG files and repositories → New repository → Git tab** and import using:

```
Repository URL:              https://github.com/YangModels/yang
Git branch:                  main
Directory within repository: vendor/cisco/xe/1681
Include subdirectories:      unchecked
```

IOS XE version directory naming: version dots removed, e.g. 16.8.1 → `1681`, 17.3.1 → `1731`.

#### 3.6.4 Starting After Reboot

Podman containers do not survive WSL2 restarts. Start them manually:

```bash
cd ~/YANG-suite/yangsuite/docker
podman-compose up -d --no-build
```


### 3.7 YANG Model Audit: Handler Verification

All 11 handlers were verified against the actual YANG model source files from the YangModels GitHub repository for both IOS XE 16.8.1 (`1681`) and 17.3.1 (`1731`). YANG files downloaded and inspected: `Cisco-IOS-XE-native`, `Cisco-IOS-XE-interfaces`, `Cisco-IOS-XE-ip`, `Cisco-IOS-XE-ospf`, `Cisco-IOS-XE-dhcp`, `Cisco-IOS-XE-ethernet`, `Cisco-IOS-XE-vlan`.

#### 3.7.1 Audit Results

| Handler | Status | Notes |
|---|---|---|
| `interface_description` | Verified | Native submodule, `<name>` key, `<description>`: correct |
| `interface_ip` | Verified | Native submodule, `<ip><address><primary>`: correct |
| `interface_state` | Verified | `<shutdown>` presence leaf: correct |
| `interface_switchport` | Verified | `<switchport><mode>`: correct |
| `dhcp_relay` | Verified | `<ip><helper-address>`: correct |
| `etherchannel` | Fixed in Round 6 | `channel-group` namespace was correct, but `channel-protocol` (sibling leaf in `Cisco-IOS-XE-ethernet`) was never emitted even when `protocol: lacp/pagp` was declared. Verification only checked Port-channel description. See §3.5.11 |
| `vlan` | Verified | `vlan-list` key `id`, leaf `name`: identical on both versions |
| `static_routes` | Verified | `ip-route-interface-forwarding-list`, `fwd-list`, `<name>` for description: confirmed from `Cisco-IOS-XE-ip` submodule |
| `ospf` | Fixed | Both legacy flat and wrapped schemas are selected from the advertised model revision. See 3.7.2 and §3.5.9 |
| `dhcp_server` | Fixed in Rounds 2 and 6 | Version-aware branching (Round 2), then the 17.x `<network>/<primary-network>` and `<excluded-address>/<low-high-address-list>` wrappers plus the Cisco-IOS-XE-dhcp augmenting namespace (Round 6). See §3.7.3 and §3.5.11 |
| `hsrp` | Fixed | Wrong namespace removed: see 3.7.4 |

One item still open for hardware validation:
- `vlan.py` read key `Cisco-IOS-XE-native:vlan-list`: the vlan-list container is augmented in by `Cisco-IOS-XE-vlan`. Production usage to date is `interface_description` only on the C9200L; no `vlan` task has been pushed against a real switch yet.
- `dhcp_server.py` read key: resolved in Round 6. The RESTCONF GET on `native/ip/dhcp/pool={name}` returns `Cisco-IOS-XE-native:pool` as the top-level key per YANG conventions, and the parser now unwraps `network → primary-network → {number, mask}` for 17.x.

#### 3.7.2 OSPF: Version-Aware Network Element

**Affected file:** `handlers/ospf.py`

The OSPF network list key and wildcard element name changed between IOS XE versions:

| Version | YANG key | XML element |
|---|---|---|
| 16.x | `key "ip mask"` | `<mask>` |
| 17.x | `key "ip wildcard"` | `<wildcard>` |

The current handler reads the advertised OSPF model revision and selects one of
two schema families:

```python
schema = LEGACY_SCHEMA if revision < "2020-07-01" else WRAPPED_SCHEMA
wildcard_elem = "mask" if schema == LEGACY_SCHEMA else "wildcard"
```

The report records `ospf_model_revision` and `ospf_schema` for each OSPF task.

> **Update 2026-05-18:** The mask-vs-wildcard policy described in this
> subsection was based on field evidence against the *flat* OSPF write
> path (`native/router/ospf={id}`). On hardware (LAB-R11-C01-R01), the
> handler moved to the augmenting `Cisco-IOS-XE-ospf:router-ospf`
> container: see §3.5.9. In that container, `<wildcard>` is used
> across all revisions we currently target, regardless of the
> 16.x/17.x release distinction this subsection describes. The
> The July 2026 update restored explicit support for the legacy flat schema
> while retaining the wrapped schema used on the validated ISR4221. Selection
> is now based on the model revision rather than the IOS XE release number.

#### 3.7.3 DHCP Server: Version-Aware Pool Structure

**Affected file:** `handlers/dhcp_server.py`

Five structural differences between IOS XE 16.x and 17.x affect the DHCP NETCONF payload. The first three were addressed in Round 2; the last two were added in Round 6 (§3.5.11) after re-reading `yang/ios-xe-1731/Cisco-IOS-XE-dhcp.yang` end to end:

| Field | 16.x structure | 17.x structure |
|---|---|---|
| `default-router` | `leaf-list default-router` | `container default-router { leaf-list default-router-list }` |
| `dns-server` | `leaf-list dns-server` | `container dns-server { leaf-list dns-server-list }` |
| `lease` | `list lease { key "Days"; leaf Days }` | `container lease { choice { container lease-value { leaf days } } }` |
| `network` | `container network { leaf number; leaf mask }` | `container network { container primary-network { leaf number; leaf mask } }` |
| `excluded-address` | flat `list excluded-address` keyed on `low-address` | `container excluded-address` with list `low-high-address-list` (key `low-address, high-address`) for ranges |

In addition, the augmenting-module rule: `Cisco-IOS-XE-dhcp` is a standalone module (not a submodule of native) augmenting `/ios:native/ios:ip/ios:dhcp`. Per YANG/NETCONF, its descendants must declare the augmenting namespace. Round 6 added `xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp"` on the root `<pool>` and `<excluded-address>` elements, matching the convention already used in `ospf.py` (`router-ospf`) and `etherchannel.py` (`channel-group`).

**16.x XML:**
```xml
<excluded-address xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp">
  <low-address>172.17.9.1</low-address>
  <high-address>172.17.9.5</high-address>
</excluded-address>
<pool xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp">
  <id>RA09-L-Data</id>
  <network>
    <number>172.17.9.16</number>
    <mask>255.255.255.240</mask>
  </network>
  <default-router>172.17.9.17</default-router>
  <dns-server>10.199.64.66</dns-server>
  <lease><Days>1</Days></lease>
</pool>
```

**17.x XML:**
```xml
<excluded-address xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp">
  <low-high-address-list>
    <low-address>172.17.9.1</low-address>
    <high-address>172.17.9.5</high-address>
  </low-high-address-list>
</excluded-address>
<pool xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp">
  <id>RA09-L-Data</id>
  <network>
    <primary-network>
      <number>172.17.9.16</number>
      <mask>255.255.255.240</mask>
    </primary-network>
  </network>
  <default-router>
    <default-router-list>172.17.9.17</default-router-list>
  </default-router>
  <dns-server>
    <dns-server-list>10.199.64.66</dns-server-list>
  </dns-server>
  <lease><lease-value><days>1</days></lease-value></lease>
</pool>
```

Both `_extract_pool` (read) and the XML builders branch on the detected version. The version is recorded in `report.json` as `ios_xe_pre_17`. The 16.x branch is YANG-correct against `yang/ios-xe-1681` but has not been exercised live: only `lab-dc-h-vm10` (CSR1000v 16.9.5) is in the 16.x cohort and no current profile assigns it a DHCP task; treat 16.x as best-effort until validated.

#### 3.7.4 HSRP: Wrong Namespace on `<standby>`

**Affected file:** `handlers/hsrp.py`

The NETCONF payload contained `xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-hsrp"` on the `<standby>` element. This namespace does not exist. The `standby` container is defined in `Cisco-IOS-XE-interfaces`, which is a **submodule** of `Cisco-IOS-XE-native`. Submodules inherit their parent module's namespace: `http://cisco.com/ns/yang/Cisco-IOS-XE-native`.

Confirmed from YANG Suite node properties:
```
module:    Cisco-IOS-XE-native
namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-native
xpath:     /native/interface/GigabitEthernet/standby
```

This is identical on both 16.8 and 17.3: no version branching needed. Fix: removed the `xmlns` attribute from `<standby>` entirely.

#### 3.7.5 YANG Suite Usage for Verification

YANG Suite was used to visually confirm the `standby` container namespace. The workflow:

1. **Setup → YANG files and repositories → Git tab**: import `vendor/cisco/xe/1681` from `https://github.com/YangModels/yang`
2. **Setup → YANG module sets**: create set with `Cisco-IOS-XE-native`, run **Locate and add missing dependencies**
3. **Explore → YANG module explorer**: select the module set, load `Cisco-IOS-XE-native`
4. Navigate to `interface/GigabitEthernet/standby`: Node Properties panel shows module and namespace

Note: YANG Suite containers do not persist across WSL2 restarts. Restart with:
```bash
cd ~/YANG-suite/yangsuite/docker
podman-compose up -d --no-build
```


---

## 4. Full Architecture

### 4.1 Overview

The solution is divided into three phases. The automation engine in
`network-automation` implements Phase 3 and is part of `main`.

### 4.2 Phase 1: Day-0: Zero Touch Provisioning

> DHCP option 67 is supported on the school DHCP server. The Ubuntu automation
> controller is `lab-dc-h-vm09` (`10.199.64.90`).

Boot sequence for a wiped device:

```
Device wiped: no config
     |
     v
Powers on → DHCP discover on Gig0/0/0
     |
     v
DHCP responds:
  IP: 172.17.X.2 or 172.17.X.66 (static reservation by MAC)
  option 67: tftp://10.199.64.134/ztp.py
     |
     v
Guest Shell starts → ztp.py executes
  Reads IP → derives rack + side
  Pushes bootstrap config
  Saves (write memory)
  Logs to bootflash:ztp.log
     |
     v
Device reachable via SSH / NETCONF / RESTCONF
```

### 4.3 Phase 2: Inventory Management

Static DHCP reservations map each device's MAC address to a fixed IP, so it
receives the same address after a wipe. Device details are kept in
`infra/inventory.yaml`.

MACs are recorded once during physical setup and entered into the school DHCP server. The `ztp.py` script itself requires no MAC list.

### 4.4 Phase 3: Day-N: Configuration Push

The flexible engine in `labs/network-automation/` handles full desired-state push across all devices. On the controller `lab-dc-h-vm09`, the reconciler resolves `intent/class_state.yaml` against `infra/inventory.yaml` and the relevant profile, then dispatches the per-device change list through the engine's 11 domain handlers:

- Interface configuration (IP addresses, descriptions, shutdown state, switchport mode)
- VLAN definitions on switches
- Routing (OSPF, static routes, default route)
- EtherChannel port aggregation
- DHCP server pools and relay
- HSRP gateway redundancy

Each handler uses a different YANG path but follows the same
read-compare-write-verify pattern. The engine has been exercised against
CSR1000v 16.9.5, Catalyst C9200L 17.6.3, and ISR4221/K9 17.3.4a. Individual
DHCP server, EtherChannel, and VLAN paths still need live coverage as noted in
section 3.7.

### 4.5 Optional Extension: Firmware Version Enforcement

The `Cisco-IOS-XE-install-oper` YANG model exposes software image management via NETCONF. If a device's IOS XE version does not match the target version, the script triggers an install RPC, pulls the image from TFTP at `10.199.64.134`, installs it, and reboots. Day-N config push proceeds after the device comes back online. Executed only when a version mismatch is detected.

### 4.6 Full Pipeline

```
DEVICE WIPED
     |
     v
[PHASE 1: DAY-0: ZTP]
  DHCP -> option 67 -> TFTP fetch ztp.py
  Guest Shell executes ztp.py
  Device gets hostname, IP, SSH, NETCONF, RESTCONF
     |
     v
[OPTIONAL: FIRMWARE CHECK]
  RESTCONF: read IOS XE version
  Mismatch: NETCONF install RPC -> TFTP image -> reboot
  RESTCONF: verify version
     |
     v
[PHASE 3: DAY-N: CONFIG PUSH]
  For each device in the resolved change list:
    RESTCONF GET  -> read current state
    Compare       -> detect delta
    NETCONF write -> apply changes
    RESTCONF GET  -> verify
     |
     +-> production:  reconciler aggregates into
     |                /var/lib/network-automation/reports/latest.json
     |
     +-> CLI debug:   automate.py writes report.json in the working dir
```

### 4.7 Ubuntu Automation Controller

The Ubuntu server `lab-dc-h-vm09` (10.199.64.90, VM on `LAB-DC-H-ESXi02`) is the
central automation controller. The reconciler runs there as a systemd service
(`network-reconciler`) on a 60-second loop: it pulls the repository, resolves
`intent/class_state.yaml` against `infra/inventory.yaml` and the relevant profile, and
dispatches per-device change lists through the engine. Per-run reports land in
`/var/lib/network-automation/reports/`, with `latest.json` as the most recent. The
service logs to the journal; follow live with `sudo journalctl -u network-reconciler -f`.
Runtime dependencies are pinned once in the root `requirements.txt`;
`reconciler/requirements.txt` remains as a compatibility entry point.

---

## 5. Infrastructure Confirmation

The following infrastructure details were confirmed by Wim Leppens:

| # | Question | Answer |
|---|---|---|
| 1 | Can DHCP option 67 be set on the school DHCP server? | **Yes**: confirmed as the Day-0 path |
| 2 | Is an Ubuntu server VM available on the ESXi host? | **Yes**: live as `lab-dc-h-vm09` (10.199.64.90), running the reconciler under systemd |
| 3 | Console access if ZTP not possible? | **Not needed**: option 67 confirmed |

---

## 6. Installation and Usage

### 6.1 Prerequisites

- Python 3.8 or later on the automation controller
- Network reachability to device management IPs on TCP/830 (NETCONF) and TCP/443 (RESTCONF)
- Cisco IOS XE 16.8+ on all target devices
- The following enabled on each target device (applied automatically by ZTP):
  - `netconf-yang`
  - `restconf`
  - `ip ssh version 2`

### 6.2 Setup

```bash
git clone https://github.com/TimurKhakimovPXL/network-automation-ra09.git
cd network-automation-ra09

# Original ra09 lab (tested)
cd labs/ra09-interface-description
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Flexible engine
cd labs/network-automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with credentials
```

### 6.3 ZTP Deployment

Copy `ztp.py` to the TFTP server:

```bash
cp labs/ztp/ztp.py /tftp/ztp.py
```

Configure DHCP option 67 on the school DHCP server to point to:

```
tftp://10.199.64.134/ztp.py
```

### 6.4 Running the Day-N Script

**Original ra09 lab:**
```bash
cd labs/ra09-interface-description
python3 automate_interface_desc.py
```

**Flexible engine: CLI debug surface:**
```bash
cd labs/network-automation
python3 automate.py
```

Reads `changes.yaml` from the working directory and writes `report.json`. Used for single-device handler debugging outside the GitOps loop. For GitOps reconciliation see §6.6.

### 6.5 Verifying Results

Inspect `report.json`. The `total_tasks`, `success`, `already_correct`, and `failed` fields give an immediate overview. The `results` array contains per-task detail including old/new values and any error messages.

### 6.6 Reconciler Path (Production)

Operators do not run `automate.py` directly in production. The reconciler on
`lab-dc-h-vm09` invokes the engine; the CLI in §6.4 is for development and debugging.

**Day-to-day operator loop:**

```bash
git pull
$EDITOR intent/class_state.yaml
git commit -am "configure for tomorrow's class"
git push
# wait ~60s, the reconciler picks up the new SHA and converges
```

**Dry-run before committing:**

```bash
# on the controller (lab-dc-h-vm09), against the current working tree
python3 scripts/manual_reconcile.py --dry-run
```

`manual_reconcile.py` resolves `intent/class_state.yaml` and the relevant profile against
`infra/inventory.yaml`, probes device reachability, and prints what would be done without
applying any changes. Useful for sanity-checking a class_state change before pushing.

**The systemd unit:**

```bash
sudo systemctl status  network-reconciler
sudo systemctl restart network-reconciler
sudo journalctl -u network-reconciler -f
```

The unit lives in `reconciler/systemd/network-reconciler.service` and runs the
reconciler on a 60-second loop.

**Where reports land:**

```
/var/lib/network-automation/reports/
├── latest.json                              # symlink to most recent run
└── reconcile-<YYYYMMDDTHHMMSSZ>.json        # per-run reports
```

Each report has the same status counters as the CLI `report.json` plus per-device
detail and a `git` block recording the head SHA that produced the run. For a one-line
health check after a push:

```bash
sudo cat /var/lib/network-automation/reports/latest.json | jq '{success, already_correct, skipped, failed}'
```

---

## 7. References

- [RFC 8040: RESTCONF Protocol](https://datatracker.ietf.org/doc/html/rfc8040)
- [RFC 6241: NETCONF Protocol](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- [ncclient documentation](https://ncclient.readthedocs.io)
- [Cisco IOS XE Zero Touch Provisioning Guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/173/b_173_programmability_cg/zero_touch_provisioning.html)
- [Project repository](https://github.com/TimurKhakimovPXL/network-automation-ra09)
- LAB 7.1 Python Network Automation Infrastructure: PXL DEVNET (Wim Leppens, 2024)
