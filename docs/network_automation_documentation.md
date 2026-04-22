---
title: Network Automation Project — Technical Documentation
author: Timur Khakimov
supervisor: Wim Leppens
institution: PXL University of Applied Sciences
group: DEVNET
date: 2026-04-22
status: in-progress
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
- [[#4. Proposed Solution — Full Architecture]]
- [[#5. Open Questions]]
- [[#6. Installation and Usage]]
- [[#7. References]]

---

## 1. Project Overview

### 1.1 Goal

The goal of this project is to design and implement a fully automated network device configuration pipeline for the PXL lab environment. The solution eliminates manual CLI interaction by using model-driven programmability: devices are bootstrapped automatically from a blank state and configured using Python scripts that communicate over NETCONF and RESTCONF.

The teacher currently configures all lab devices manually after each wipe. This project replaces that process with a system where devices configure themselves after boot, and ongoing changes are pushed programmatically from a central Ubuntu automation controller.

### 1.2 Scope

- **Day-0 bootstrap:** bring a wiped, unconfigured Cisco IOS XE device to a reachable, NETCONF/RESTCONF-enabled state automatically
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

---

## 2. Lab Infrastructure

### 2.1 Network Topology

The PXL lab is structured around student racks **RA01 through RA10**, connected to a central backbone. Each student rack contains two routers (`C01-R01` and `C02-R01`) and two access switches (`A01-SW01` and `A02-SW01`). All routers uplink via GigabitEthernet 0/0/1 to the backbone switch `LAB-BR-A-SW08`, which connects upstream to `LAB-BR-C-R03` at `10.199.65.100`.

A Data Center segment hosts shared infrastructure services used by all racks.

### 2.2 Data Center Services

| Service | IP Address | Role |
|---|---|---|
| DHCP / DNS / NTP | `10.199.64.66` | IP assignment, name resolution, time sync |
| TFTP Server | `10.199.64.134` | Bootstrap script delivery for ZTP |
| YANG Suite | `10.125.100.231:8443` | YANG model browser and NETCONF testing |
| ESXi Host | `10.199.64.37` | Virtualisation host (Ubuntu automation controller VM) |

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

**RA09 example:** management subnet `172.17.9.0/28`, router management IP `172.17.9.2` — this is the address used in `changes.yaml` and confirmed working with the automation script.

### 2.4 VLAN and Subnet Scheme

Each rack gets its own VLAN and subnet block based on rack number X. The left half of the rack (L) and right half (R) each get four VLANs.

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

Within each subnet, addresses are allocated as follows:

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

Each router's Gig 0/0/1 uplink is patched through to `LAB-BR-A-SW08` in the central rack via the patch panel:

| Device | Router Port | Switch Port |
|---|---|---|
| LAB-RA09-C01-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/17 |
| LAB-RA09-C02-R01 | Gig 0/0/1 | LAB-BR-A-SW08 Fa 0/18 |

> [!TIP] Console Access
> Console ports on the back of each device are the out-of-band access path. Whether these are patched through to a console server is a pending open question — see [[#5. Open Questions]].

---

## 3. Current Work — The Automation Engine

### 3.1 Repository

```
network-automation-ra09/
├── README.md                          # Repository index
└── labs/
    └── ra09-interface-description/
        ├── automate_interface_desc.py # Main automation script
        ├── changes.yaml               # Desired state input
        ├── report.json                # Run output (auto-generated)
        ├── requirements.txt           # Python dependencies
        └── README.md                  # Lab documentation
```

The repository follows a multi-lab structure where each lab lives in its own subdirectory under `labs/`. This allows the project to grow as additional labs are added without cluttering the root.

### 3.2 Lab: ra09-interface-description

This lab implements the core automation pattern for the project: a YAML-driven, idempotent configuration push using RESTCONF for reads and NETCONF for writes. It was developed and tested successfully against real Cisco IOS XE hardware in the RA09 rack.

#### 3.2.1 Automation Workflow

The script executes the following steps for each device and interface change defined in `changes.yaml`:

1. Read current interface state from the device via RESTCONF GET
2. Compare the actual description against the desired description from YAML
3. Skip the change if the device is already in the desired state (idempotent)
4. Apply the change via NETCONF `edit-config` targeting the running configuration
5. Verify the change by performing a second RESTCONF GET
6. Record the outcome (`success`, `already_correct`, or error) in `report.json`

#### 3.2.2 File: `automate_interface_desc.py`

The script is structured into clearly separated functions, each with a single responsibility:

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

The script targets the `Cisco-IOS-XE-native` YANG model. The RESTCONF URL is constructed per interface:

```
GET https://{host}/restconf/data/
    Cisco-IOS-XE-native:native/interface/
    {interface_type}={url_encoded_interface_name}
```

The interface name is URL-encoded using `urllib.parse.quote()` to handle forward slashes in interface identifiers such as `0/0/0`.

#### 3.2.4 NETCONF Payload

Configuration is applied using a NETCONF `edit-config` RPC targeting the running datastore:

```xml
<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
    <interface>
      <GigabitEthernet>
        <n>0/0/0</n>
        <description>RA09-L management interface</description>
      </GigabitEthernet>
    </interface>
  </native>
</config>
```

#### 3.2.5 Error Handling

The script handles errors per task without aborting the entire run. Each result carries a `status` field and an `error` field for diagnostics:

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

The desired state is declared in a YAML file. Each device entry supports multiple interface changes. The script iterates over all devices and all changes within each device.

```yaml
devices:
  - name: LAB-RA09-C01-R01
    host: 172.17.9.2
    username: cisco
    password: cisco
    changes:
      - interface_type: GigabitEthernet
        interface_name: "0/0/0"
        description: RA09-L management interface
```

> [!WARNING] Credentials
> Credentials are stored in plaintext for lab purposes only. In production, use environment variables or a secrets manager such as HashiCorp Vault or Ansible Vault.

#### 3.2.7 File: `report.json`

Every run produces a `report.json` file with a summary and per-task results. This file is machine-readable and can be consumed by a dashboard or alerting system in a future extension.

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

#### 3.2.8 File: `requirements.txt`

```
ncclient
requests
PyYAML
urllib3
```

---

## 4. Proposed Solution — Full Architecture

### 4.1 Overview

The complete solution is divided into three phases that build on each other. The automation engine built in `ra09-interface-description` forms the foundation of Phase 3 and requires no redesign — only extension.

### 4.2 Phase 1 — Day-0: Zero Touch Provisioning

When a device is wiped it has no IP address, no SSH, no NETCONF. It cannot be reached over the network. The Day-0 phase solves this using Cisco IOS XE's built-in Zero Touch Provisioning (ZTP) capability.

ZTP works as follows: on first boot, IOS XE automatically sends a DHCP request. If the DHCP server responds with option 67 (bootfile-name), the device fetches that file from the TFTP server and executes it as a Python script inside the IOS XE Guest Shell environment.

The ZTP Python script pushes a minimal bootstrap configuration to the device:

- Hostname
- Enable secret and local user credentials
- IP address on the management interface
- SSH version 2 with RSA key generation
- `netconf-yang` enabled
- `restconf` enabled
- VTY lines configured for SSH

After the ZTP script completes, the device is reachable over the network and ready for Day-N configuration. No console access is required.

> [!WARNING] Prerequisite
> The school DHCP server must support adding option 67 pointing to the TFTP server at `10.199.64.134`. This is the primary open question pending confirmation from the lab supervisor — see [[#5. Open Questions]].

### 4.3 Phase 2 — Inventory Management

After ZTP, each device has an IP address. For the Day-N automation to work reliably across 10+ devices, each device must have a stable, known IP. This is achieved using static DHCP reservations: each device's MAC address is mapped to a fixed IP in the DHCP server. The `changes.yaml` file is then stable across reboots and wipes.

The MAC addresses are recorded once during the initial physical setup and entered into the DHCP server as reservations. From that point forward, the device always receives the same IP regardless of how many times it is wiped and reprovisioned.

### 4.4 Phase 3 — Day-N: Configuration Push

This phase is implemented by the existing automation engine. With the inventory in place, `changes.yaml` is expanded to include all 10+ devices. The script is run from the Ubuntu automation controller and pushes the full desired state to every device.

The Day-N scope extends beyond interface descriptions to cover the full device desired state:

- Interface configuration (IP addresses, descriptions, shutdown state)
- VLAN definitions on switches
- Routing configuration (OSPF, static routes, default route)
- AAA and authentication
- VRRP/HSRP gateway redundancy

Each configuration domain maps to a different YANG model and NETCONF payload, but the read-compare-write-verify pattern remains identical to what is already implemented.

### 4.5 Optional Extension — Firmware Version Enforcement

If devices in the lab run different IOS XE versions, the automation can enforce a target version as a pre-step before Day-N configuration. The IOS XE NETCONF interface exposes YANG models for software image management (`Cisco-IOS-XE-install-oper`). The script checks the current version via RESTCONF, and if it does not match the target version, triggers an install RPC that pulls the image from the TFTP server at `10.199.64.134`, installs it, and reboots the device. The Day-N push then proceeds after the device comes back online.

This step is conditionally executed only when a version mismatch is detected.

### 4.6 Full Pipeline

```
DEVICE WIPED
     |
     v
[PHASE 1 — DAY-0: ZTP]
  DHCP request -> option 67 -> TFTP fetch bootstrap.py
  Bootstrap script runs in Guest Shell
  Device gets IP, SSH, NETCONF, RESTCONF
     |
     v
[OPTIONAL — FIRMWARE CHECK]
  RESTCONF: read current IOS XE version
  If mismatch: NETCONF install RPC -> TFTP image -> reboot
  RESTCONF: verify version
     |
     v
[PHASE 3 — DAY-N: CONFIG PUSH]
  For each device in changes.yaml:
    RESTCONF GET  -> read current state
    Compare       -> detect delta
    NETCONF write -> apply changes
    RESTCONF GET  -> verify
    Write result to report.json
     |
     v
ALL DEVICES CONFIGURED — report.json generated
```

### 4.7 Ubuntu Automation Controller

The Ubuntu server (VM on `LAB-DC-H-ESXi02`) acts as the central automation controller. It stores the automation scripts, the `changes.yaml` inventory, and the generated reports. The teacher or a student runs the Day-N script on demand or on a schedule. No tooling beyond the Python packages in `requirements.txt` is required.

---

## 5. Open Questions

> [!TODO] Pending — awaiting response from supervisor

The following items are pending confirmation from the lab supervisor before the full architecture can be finalised:

| # | Question | Impact |
|---|---|---|
| 1 | Can DHCP option 67 be set on the school DHCP server? | Determines whether ZTP is possible. If not, OOB console access is required for Day-0. |
| 2 | Is an Ubuntu server VM available on the ESXi host and is access granted? | Required to host the automation controller and run the Day-N scripts centrally. |
| 3 | If ZTP is not possible, is console access available via a console server in the rack? | Fallback Day-0 path using `pyserial` or Netmiko over OOB console. |

---

## 6. Installation and Usage

### 6.1 Prerequisites

- Python 3.8 or later installed on the automation controller
- Network reachability to device management IPs on TCP/830 (NETCONF) and TCP/443 (RESTCONF)
- Cisco IOS XE 16.6+ on all target devices
- The following enabled on each target device:
  - `netconf-yang`
  - `restconf`
  - `ip ssh version 2`

### 6.2 Setup

```bash
git clone https://github.com/TimurKhakimovPXL/network-automation-ra09.git
cd network-automation-ra09/labs/ra09-interface-description
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 6.3 Configuration

Edit `changes.yaml` to define the desired state for each device and interface. Add additional device entries for each router in scope. Ensure static DHCP reservations are in place so device IPs remain stable across reboots.

### 6.4 Running the Script

```bash
python3 automate_interface_desc.py
```

The script reads `changes.yaml` from the working directory and writes `report.json` on completion. Console output provides real-time status per device and interface.

### 6.5 Verifying Results

Inspect `report.json` for a full summary of the run. The `total_tasks`, `success`, `already_correct`, and `failed` fields provide an immediate overview. The `results` array contains per-task detail including the old description, new description, and any error messages.

---

## 7. References

- [RFC 8040 — RESTCONF Protocol](https://datatracker.ietf.org/doc/html/rfc8040)
- [RFC 6241 — NETCONF Protocol](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- [ncclient documentation](https://ncclient.readthedocs.io)
- [Cisco IOS XE Zero Touch Provisioning Guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/173/b_173_programmability_cg/zero_touch_provisioning.html)
- [Project repository](https://github.com/TimurKhakimovPXL/network-automation-ra09)
- LAB 7.1 Python Network Automation Infrastructure — PXL DEVNET (Wim Leppens, 2024)
