---
title: Network Automation Project — Technical Documentation
author: Timur Khakimov
supervisor: Wim Leppens
institution: PXL University of Applied Sciences
group: DEVNET
date: 2026-04-26
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

# Network Automation Project — Technical Documentation

## Table of Contents

- [[#1. Project Overview]]
- [[#2. Lab Infrastructure]]
- [[#3. Current Work — The Automation Engine]]
- [[#3.5 Known Issues Fixed — 2026-04-26]]
- [[#3.6 YANG Suite — Local Installation]]
- [[#3.7 YANG Model Audit — Handler Verification]]
- [[#4. Full Architecture]]
- [[#5. Infrastructure Confirmation]]
- [[#6. Installation and Usage]]
- [[#7. References]]

---

## 1. Project Overview

### 1.1 Goal

The goal of this project is to design and implement a fully automated network device configuration pipeline for the PXL lab environment. The solution eliminates manual CLI interaction by using model-driven programmability: devices are bootstrapped automatically from a blank state and configured using Python scripts that communicate over NETCONF and RESTCONF.

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
| ESXi Host | `10.199.64.37` | Virtualisation host — Ubuntu automation controller VM |

Domain: `data.labnet.local`

### 2.3 Per-Rack Addressing Scheme

> [!INFO] Variable Notation
> Throughout this document, **X** refers to the student rack number (1–10). Each rack has its own isolated addressing space. This documentation uses **rack RA09 (X=9)** as the worked example — the student in the adjacent rack (RA08, X=8) has an identical structure with `X=8` substituted throughout.

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

**RA09 example:** management subnet `172.17.9.0/28`, router management IP `172.17.9.2` — confirmed working against real hardware.

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
| .21–.30 | DHCP pool — Data_Users (RA0X-L only) |
| .85–.94 | DHCP pool — Data_Users (RA0X-R only) |

### 2.5 Physical Patching

| Device | Router Port | Switch Port |
|---|---|---|
| LAB-RA09-C01-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/17 |
| LAB-RA09-C02-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/18 |

---

## 3. Current Work — The Automation Engine

### 3.1 Repository Structure

```
network-automation-ra09/
├── README.md                          # Repository index
└── labs/
    ├── ra09-interface-description/    # Day-N: interface description automation (tested)
    │   ├── automate_interface_desc.py
    │   ├── changes.yaml
    │   ├── report.json
    │   ├── requirements.txt
    │   └── README.md
    ├── network-automation/            # Day-N: flexible multi-domain engine (feature branch)
    │   ├── automate.py
    │   ├── changes.yaml
    │   ├── report.json
    │   ├── requirements.txt
    │   ├── README.md
    │   └── handlers/
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

### 3.2 Lab: ra09-interface-description (Day-N, tested)

This lab implements the core automation pattern for the project: a YAML-driven, idempotent configuration push using RESTCONF for reads and NETCONF for writes. Developed and tested against real Cisco IOS XE hardware in the RA09 rack. This lab is the direct origin of the flexible engine in section 3.3 — the pattern is identical, the scope is extended.

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

> [!NOTE] Credentials
> Credentials are not stored in `changes.yaml`. They are loaded from `.env` via `python-dotenv` using `LAB_USER` and `LAB_PASS`. The `.env` file is gitignored and never committed. Copy `.env.example` to `.env` and fill in your values before running.

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

`ztp.py` is a Zero Touch Provisioning bootstrap script that runs automatically on a wiped IOS XE device the first time it boots. It requires no console access and no manual intervention.

#### 3.3.1 How It Works

When a wiped device boots, IOS XE has no startup config. It enters ZTP mode, sends a DHCP discover, and receives an IP address. If the DHCP server includes option 67 pointing to `tftp://10.199.64.134/ztp.py`, the device downloads and executes that script inside Guest Shell.

#### 3.3.2 Device Identification — No Manual MAC Inventory

The script identifies the device automatically from the DHCP-assigned IP using the PXL addressing scheme. No MAC address list is required in the script itself — MACs only need to be configured in the DHCP server for static reservations.

```
DHCP IP 172.17.X.2  → LAB-RA0X-C01-R01  (rack X, left side)
DHCP IP 172.17.X.66 → LAB-RA0X-C02-R01  (rack X, right side)
```

The rack number `X` is the third octet of the IP. This means the same single `ztp.py` file on the TFTP server handles all 20 routers across all 10 racks with zero per-device customisation.

#### 3.3.3 What the Script Configures

- Hostname derived from rack and side
- Enable secret and local admin credentials
- Management interface IP (static, matching DHCP assignment)
- Default route via rack gateway
- `ip domain-name data.labnet.local`
- RSA 2048-bit key (with IOS XE 16.8 compatibility check)
- `ip ssh version 2`
- VTY lines — SSH only
- `no ip http server` — disables plaintext HTTP on port 80
- `ip http secure-server` — enables HTTPS on port 443 (required for RESTCONF)
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

The script handles the 16.8 RSA issue by checking whether a key already exists before attempting generation. If generation fails, it logs a warning and continues — NETCONF and RESTCONF work without SSH being fully configured.

#### 3.3.5 Log File

Every step is logged to `bootflash:ztp.log` with timestamps. This file persists across reboots. If ZTP fails, connect via console and run:

```
more bootflash:ztp.log
```

### 3.4 Lab: network-automation (Day-N, feature branch)

Built directly on the pattern established in `ra09-interface-description`, the flexible engine generalises the single-domain approach into a universal dispatcher. The script itself never changes — only `changes.yaml` is edited to declare desired state for the day.

#### 3.4.1 Architecture

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

#### 3.4.2 Handler Registry

Each domain is a self-contained module in `handlers/`. Adding a new domain requires two steps: create the handler file, register it in `HANDLERS` in `automate.py`. No other files change.

| Handler | Domain | YANG path |
|---|---|---|
| `interface_description` | Interface descriptions | `native/interface/{type}={name}` |
| `interface_ip` | IPv4 address assignment | `native/interface/{type}={name}/ip/address` |
| `interface_switchport` | Access / trunk mode and VLANs | `native/interface/{type}={name}/switchport` |
| `interface_state` | Shutdown / no shutdown | `native/interface/{type}={name}/shutdown` |
| `ospf` | OSPF process, router-id, networks | `native/router/ospf={process_id}` |
| `static_route` | IPv4 static routes | `native/ip/route` |
| `vlan` | VLAN definitions on switches | `native/vlan/vlan-list` |
| `etherchannel` | Port-channel and member interfaces | `native/interface/Port-channel={id}` |
| `dhcp_server` | DHCP pools, exclusions, DNS, gateway | `native/ip/dhcp/pool={name}` |
| `dhcp_relay` | ip helper-address on SVIs | `native/interface/{type}={name}/ip/helper-address` |
| `hsrp` | Gateway redundancy | `native/interface/{type}={name}/standby` |

#### 3.4.3 Idempotency and Error Handling

Every handler follows the same four-step cycle: read current state via RESTCONF, compare against desired state, write only if a delta exists via NETCONF, verify via a second RESTCONF read. A failure in one task is recorded in `report.json` and the run continues — no single device failure aborts the rest.

#### 3.4.4 Status Values

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | Desired state already present, no change made |
| `interface_not_found` | RESTCONF returned 404 |
| `read_failed` | RESTCONF GET failed |
| `edit_failed` | NETCONF edit-config failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value |
| `unknown_type` | No handler registered for that change type |
| `missing_type` | Change entry has no type field |

### 3.5 Known Issues Fixed — 2026-04-26

The following bugs were identified by code review prior to hardware validation and corrected on `feature/flexible-automation-engine`.

#### 3.5.1 NETCONF Key Element: `<n>` vs `<name>`

**Affected files:** all handlers in `handlers/` except `ospf.py`, `static_routes.py`, `vlan.py`, `dhcp_server.py`

The YANG model `Cisco-IOS-XE-native` uses `<name>` as the list key element for interface identification in NETCONF payloads. The flexible engine handlers were incorrectly using `<n>`. IOS XE may silently accept XML with unrecognised elements and return `<ok/>` without writing anything to the running config — meaning the script would report `success` for a change that never applied.

Corrected payload (all interface handlers):

```xml
<GigabitEthernet>
  <n>0/0/0</n>
  <description>RA09-L management interface</description>
</GigabitEthernet>
```

This is consistent with the reference implementation in `ra09-interface-description/automate_interface_desc.py`, which was tested against real hardware.

#### 3.5.2 ncclient Device Handler: `iosxe` vs `csr`

**Affected file:** `automate.py`

ncclient uses a `device_params` dict to select an internal handler class that applies device-specific NETCONF framing workarounds. The dispatcher was using `{"name": "iosxe"}`. The correct value for Cisco IOS XE is `{"name": "csr"}` — named after the CSR1000v, the original IOS XE platform in ncclient's codebase.

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

m = manager.connect(
    host="172.17.9.2", port=830,
    username="cisco", password="cisco",
    hostkey_verify=False,
    device_params={"name": "csr"},
    allow_agent=False, look_for_keys=False,
)
for cap in m.server_capabilities:
    print(cap)
m.close_session()
```

**2. Check for candidate datastore support:**

Look for `urn:ietf:params:netconf:capability:candidate:1.0` in the output. If present on all devices, candidate datastore is a viable future enhancement. If absent on any device, design around running as the write target.

**3. OSPF RESTCONF key — resolved, no manual check needed:**

The correct key is `Cisco-IOS-XE-ospf:ospf` on all IOS XE versions from 16.8 through 17.x. This was confirmed by inspecting the `Cisco-IOS-XE-ospf.yang` module directly from the YangModels GitHub repository across versions `1681`, `1693`, `1711`, `1731`, and `1751`. The namespace `http://cisco.com/ns/yang/Cisco-IOS-XE-ospf` has never changed. The fix is documented in section 3.5.6 below.


#### 3.5.6 OSPF RESTCONF JSON Key

**Affected file:** `handlers/ospf.py`

The `_extract_ospf_state()` function was reading the RESTCONF response using the wrong JSON key:

```python
# Wrong — always returned empty dict
ospf = data.get("Cisco-IOS-XE-native:ospf", {})

# Correct — matches the OSPF module namespace
ospf = data.get("Cisco-IOS-XE-ospf:ospf", {})
```

**Root cause:** OSPF configuration in IOS XE is defined in the augmenting module `Cisco-IOS-XE-ospf` with namespace `http://cisco.com/ns/yang/Cisco-IOS-XE-ospf`. When RESTCONF returns data from a path that resolves into an augmenting module, the JSON key uses that module's namespace — not the native namespace. The RESTCONF GET path `native/router/ospf={id}` resolves into the OSPF augmentation, so the top-level key is `Cisco-IOS-XE-ospf:ospf`.

This was confirmed by inspecting `Cisco-IOS-XE-ospf.yang` directly from the YangModels GitHub repository across IOS XE versions `1681` (16.8.1), `1693` (16.9.3), `1711` (17.1.1), `1731` (17.3.1), and `1751` (17.5.1). The namespace is identical across all versions — no version branching is needed.

**Impact without fix:** `_extract_ospf_state()` always returned an empty dict. The handler always concluded OSPF was not configured and pushed a write on every run, making OSPF non-idempotent. The write itself was correct (NETCONF namespace was already right) but the read-compare phase was broken.

The docstring was also corrected from `Cisco-IOS-XE-ospf-oper / Cisco-IOS-XE-native` to `Cisco-IOS-XE-ospf` with the explicit namespace URI.


### 3.6 YANG Suite — Local Installation

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
DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_PASSWORD=admin123
DJANGO_SUPERUSER_EMAIL=admin@localhost.com
DJANGO_SETTINGS_MODULE=yangsuite.settings.production
SECRET_KEY=yangsuite-secret-key-change-in-production
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
   python3 manage.py createsuperuser --username admin --email admin@localhost.com"
```

#### 3.6.2 Access

```
URL:      https://localhost:8443
Username: admin
Password: admin123
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


### 3.7 YANG Model Audit — Handler Verification

All 11 handlers were verified against the actual YANG model source files from the YangModels GitHub repository for both IOS XE 16.8.1 (`1681`) and 17.3.1 (`1731`). YANG files downloaded and inspected: `Cisco-IOS-XE-native`, `Cisco-IOS-XE-interfaces`, `Cisco-IOS-XE-ip`, `Cisco-IOS-XE-ospf`, `Cisco-IOS-XE-dhcp`, `Cisco-IOS-XE-ethernet`, `Cisco-IOS-XE-vlan`.

#### 3.7.1 Audit Results

| Handler | Status | Notes |
|---|---|---|
| `interface_description` | ✅ Clean | Native submodule, `<name>` key, `<description>` — correct |
| `interface_ip` | ✅ Clean | Native submodule, `<ip><address><primary>` — correct |
| `interface_state` | ✅ Clean | `<shutdown>` presence leaf — correct |
| `interface_switchport` | ✅ Clean | `<switchport><mode>` — correct |
| `dhcp_relay` | ✅ Clean | `<ip><helper-address>` — correct |
| `etherchannel` | ✅ Clean | `channel-group` is in `Cisco-IOS-XE-ethernet` augmenting module — `xmlns` on `<channel-group>` is correct |
| `vlan` | ✅ Clean | `vlan-list` key `id`, leaf `name` — identical on both versions |
| `static_routes` | ✅ Clean | `ip-route-interface-forwarding-list`, `fwd-list`, `<name>` for description — confirmed from `Cisco-IOS-XE-ip` submodule |
| `ospf` | ⚠️ Fixed | Version-aware branching added — see 3.7.2 |
| `dhcp_server` | ⚠️ Fixed | Version-aware branching added — see 3.7.3 |
| `hsrp` | ⚠️ Fixed | Wrong namespace removed — see 3.7.4 |

Two additional items flagged for hardware validation:
- `vlan.py` read key `Cisco-IOS-XE-native:vlan` — vlan content comes from augmenting module, response key may differ
- `dhcp_server.py` read key `Cisco-IOS-XE-native:pool` — same concern

#### 3.7.2 OSPF — Version-Aware Network Element

**Affected file:** `handlers/ospf.py`

The OSPF network list key and wildcard element name changed between IOS XE versions:

| Version | YANG key | XML element |
|---|---|---|
| 16.x | `key "ip mask"` | `<mask>` |
| 17.x | `key "ip wildcard"` | `<wildcard>` |

The handler now detects the IOS XE version at runtime from NETCONF capabilities and branches both the read extraction and the NETCONF write accordingly:

```python
wildcard_key  = "mask" if pre_17 else "wildcard"   # for _extract_ospf_state
wildcard_elem = "mask" if pre_17 else "wildcard"   # for _build_network_xml
```

The version is recorded in `report.json` as `ios_xe_pre_17` for each OSPF task.

#### 3.7.3 DHCP Server — Version-Aware Pool Structure

**Affected file:** `handlers/dhcp_server.py`

Three structural changes between IOS XE versions affect the DHCP pool NETCONF payload:

| Field | 16.x structure | 17.x structure |
|---|---|---|
| `default-router` | `leaf-list default-router` | `container default-router { leaf-list default-router-list }` |
| `dns-server` | `leaf-list dns-server` | `container dns-server { leaf-list dns-server-list }` |
| `lease` | `list lease { key "Days"; leaf Days }` | `container lease { choice { container lease-value { leaf days } } }` |

**16.x XML:**
```xml
<default-router>172.17.9.17</default-router>
<dns-server>10.199.64.66</dns-server>
<lease><Days>1</Days></lease>
```

**17.x XML:**
```xml
<default-router>
  <default-router-list>172.17.9.17</default-router-list>
</default-router>
<dns-server>
  <dns-server-list>10.199.64.66</dns-server-list>
</dns-server>
<lease><lease-value><days>1</days></lease-value></lease>
```

Both `_extract_pool` (read) and `_build_pool_xml` (write) branch on the detected version. The version is recorded in `report.json` as `ios_xe_pre_17`.

#### 3.7.4 HSRP — Wrong Namespace on `<standby>`

**Affected file:** `handlers/hsrp.py`

The NETCONF payload contained `xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-hsrp"` on the `<standby>` element. This namespace does not exist. The `standby` container is defined in `Cisco-IOS-XE-interfaces`, which is a **submodule** of `Cisco-IOS-XE-native`. Submodules inherit their parent module's namespace — `http://cisco.com/ns/yang/Cisco-IOS-XE-native`.

Confirmed from YANG Suite node properties:
```
module:    Cisco-IOS-XE-native
namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-native
xpath:     /native/interface/GigabitEthernet/standby
```

This is identical on both 16.8 and 17.3 — no version branching needed. Fix: removed the `xmlns` attribute from `<standby>` entirely.

#### 3.7.5 YANG Suite Usage for Verification

YANG Suite was used to visually confirm the `standby` container namespace. The workflow:

1. **Setup → YANG files and repositories → Git tab** — import `vendor/cisco/xe/1681` from `https://github.com/YangModels/yang`
2. **Setup → YANG module sets** — create set with `Cisco-IOS-XE-native`, run **Locate and add missing dependencies**
3. **Explore → YANG module explorer** — select the module set, load `Cisco-IOS-XE-native`
4. Navigate to `interface/GigabitEthernet/standby` — Node Properties panel shows module and namespace

Note: YANG Suite containers do not persist across WSL2 restarts. Restart with:
```bash
cd ~/YANG-suite/yangsuite/docker
podman-compose up -d --no-build
```


---

## 4. Full Architecture

### 4.1 Overview

The complete solution is divided into three phases. The flexible automation engine in `network-automation` is the implementation of Phase 3 — built and ready for hardware validation on the feature branch.

### 4.2 Phase 1 — Day-0: Zero Touch Provisioning

> [!SUCCESS] Confirmed
> DHCP option 67 is supported on the school DHCP server. ZTP is the confirmed Day-0 path. Ubuntu server is available on ESXi. Both will be configured together with the lab supervisor.

Boot sequence for a wiped device:

```
Device wiped — no config
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

### 4.3 Phase 2 — Inventory Management

Static DHCP reservations map each device's MAC address to a fixed IP. This ensures the same IP survives every wipe and reprovision. `changes.yaml` remains stable and requires no updates after the initial setup.

MACs are recorded once during physical setup and entered into the school DHCP server. The `ztp.py` script itself requires no MAC list.

### 4.4 Phase 3 — Day-N: Configuration Push

The flexible engine in `labs/network-automation/` handles full desired-state push across all devices. `changes.yaml` declares the target state for all 10+ devices. `automate.py` runs from the Ubuntu automation controller and pushes the full desired state via 11 domain handlers:

- Interface configuration (IP addresses, descriptions, shutdown state, switchport mode)
- VLAN definitions on switches
- Routing (OSPF, static routes, default route)
- EtherChannel port aggregation
- DHCP server pools and relay
- HSRP gateway redundancy

Each handler uses a different YANG model but the same read-compare-write-verify pattern. The engine is on `feature/flexible-automation-engine` and pending hardware validation before merge to main.

### 4.5 Optional Extension — Firmware Version Enforcement

The `Cisco-IOS-XE-install-oper` YANG model exposes software image management via NETCONF. If a device's IOS XE version does not match the target version, the script triggers an install RPC, pulls the image from TFTP at `10.199.64.134`, installs it, and reboots. Day-N config push proceeds after the device comes back online. Executed only when a version mismatch is detected.

### 4.6 Full Pipeline

```
DEVICE WIPED
     |
     v
[PHASE 1 — DAY-0: ZTP]
  DHCP -> option 67 -> TFTP fetch ztp.py
  Guest Shell executes ztp.py
  Device gets hostname, IP, SSH, NETCONF, RESTCONF
     |
     v
[OPTIONAL — FIRMWARE CHECK]
  RESTCONF: read IOS XE version
  Mismatch: NETCONF install RPC -> TFTP image -> reboot
  RESTCONF: verify version
     |
     v
[PHASE 3 — DAY-N: CONFIG PUSH]
  For each device in changes.yaml:
    RESTCONF GET  -> read current state
    Compare       -> detect delta
    NETCONF write -> apply changes
    RESTCONF GET  -> verify
    Write to report.json
     |
     v
ALL DEVICES CONFIGURED — report.json generated
```

### 4.7 Ubuntu Automation Controller

The Ubuntu server (VM on `LAB-DC-H-ESXi02`, confirmed available) acts as the central automation controller. It hosts the automation scripts, `changes.yaml`, and generated reports. The Day-N script is run on demand or on a schedule. No tooling beyond `requirements.txt` is required.

---

## 5. Infrastructure Confirmation

> [!SUCCESS] All infrastructure questions resolved — confirmed by Wim Leppens

| # | Question | Answer |
|---|---|---|
| 1 | Can DHCP option 67 be set on the school DHCP server? | **Yes** — will be configured together next week |
| 2 | Is an Ubuntu server VM available on the ESXi host? | **Yes** — will be set up together next week |
| 3 | Console access if ZTP not possible? | **Not needed** — option 67 confirmed |

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

# Flexible engine (feature branch)
git checkout feature/flexible-automation-engine
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

**Flexible engine:**
```bash
cd labs/network-automation
python3 automate.py
```

Both read `changes.yaml` from the working directory and write `report.json` on completion.

### 6.5 Verifying Results

Inspect `report.json`. The `total_tasks`, `success`, `already_correct`, and `failed` fields give an immediate overview. The `results` array contains per-task detail including old/new values and any error messages.

---

## 7. References

- [RFC 8040 — RESTCONF Protocol](https://datatracker.ietf.org/doc/html/rfc8040)
- [RFC 6241 — NETCONF Protocol](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- [ncclient documentation](https://ncclient.readthedocs.io)
- [Cisco IOS XE Zero Touch Provisioning Guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/173/b_173_programmability_cg/zero_touch_provisioning.html)
- [Project repository](https://github.com/TimurKhakimovPXL/network-automation-ra09)
- LAB 7.1 Python Network Automation Infrastructure — PXL DEVNET (Wim Leppens, 2024)
