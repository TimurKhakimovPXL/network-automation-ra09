# RA09 – Interface Description Automation

> YAML-driven, idempotent interface description management on Cisco IOS XE using RESTCONF and NETCONF.

---

## Overview

This lab automates interface description configuration on a Cisco IOS XE router without touching the CLI.
Desired state is declared in a YAML file. The script reads current state via **RESTCONF**, compares it against
the desired state, and applies any delta via **NETCONF**. A final RESTCONF read verifies the change.
Results are written to a structured JSON report.

The workflow is fully **idempotent**: running it twice produces the same outcome — the second run detects
the description is already correct and skips the change without touching the device.

---

## Workflow

```
changes.yaml (desired state)
        │
        ▼
 RESTCONF GET ──► compare actual vs desired
        │
        ├── already correct → skip, mark verified
        │
        └── delta found
                │
                ▼
         NETCONF edit-config (running)
                │
                ▼
         RESTCONF GET (verify)
                │
                ▼
           report.json
```

---

## Repository Structure

```
labs/ra09-interface-description/
├── automate_interface_desc.py   # Main automation script
├── changes.yaml                 # Desired state input (devices + interface descriptions)
├── report.json                  # Run output (auto-generated, do not edit)
├── requirements.txt             # Python dependencies
└── README.md
```

---

## Prerequisites

**Router requirements:**

- Cisco IOS XE with NETCONF and RESTCONF enabled
- NETCONF runs on TCP/830, RESTCONF on HTTPS/443

Enable on the device if not already active:

```
netconf-yang
restconf
```

**Host requirements:**

- Python 3.8 or later
- Network reachability to the router management IP on ports 830 and 443

---

## Installation

```bash
# Clone the repository
git clone https://github.com/TimurKhakimovPXL/network-automation-ra09.git
cd network-automation-ra09/labs/ra09-interface-description

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Environment Variables

Credentials are loaded from a `.env` file in the repository root. Copy the example file and fill in your values:

```bash
cp ../../.env.example ../../.env
```

Then edit `.env`:

```env
LAB_USER=your_username
LAB_PASS=your_password
```

> **Note:** `.env` is listed in `.gitignore` and will never be committed. Never share or commit this file.

---

## Configuration

Edit `changes.yaml` to declare your desired state. Each device entry supports multiple interface changes.
Credentials are loaded from `.env` — do not add them here.

```yaml
devices:
  - name: LAB-RA09-C01-R01
    host: 172.17.9.2
    changes:
      - interface_type: GigabitEthernet
        interface_name: "0/0/0"
        description: RA09-L management interface
```

---

## Usage

```bash
python3 automate_interface_desc.py
```

The script reads `changes.yaml` from the working directory and writes `report.json` on completion.

---

## Output

**Console (first run — change applied):**

```
=== Processing LAB-RA09-C01-R01 GigabitEthernet0/0/0 ===
[INFO] Current description: None
[INFO] NETCONF edit applied.
[SUCCESS] Verified description: 'RA09-L management interface'

[INFO] Report written to report.json
```

**Console (second run — idempotent):**

```
=== Processing LAB-RA09-C01-R01 GigabitEthernet0/0/0 ===
[INFO] Current description: 'RA09-L management interface'
[SKIP] Desired description already present. No change needed.

[INFO] Report written to report.json
```

**`report.json` structure:**

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
      "old_description": "RA09-L management interface",
      "new_description": "RA09-L management interface",
      "changed": false,
      "verified": true,
      "status": "already_correct"
    }
  ]
}
```

### Status values

| Status | Meaning |
|---|---|
| `success` | Change applied and verified |
| `already_correct` | No change needed; desired state already present |
| `interface_not_found` | Interface does not exist on the device (HTTP 404) |
| `read_failed` | RESTCONF GET failed |
| `edit_failed` | NETCONF edit-config failed |
| `verify_failed` | Post-change RESTCONF GET failed |
| `verify_mismatch` | Change applied but verification returned unexpected value |

---

## Dependencies

| Package | Purpose |
|---|---|
| `ncclient` | NETCONF client (edit-config) |
| `requests` | RESTCONF HTTP client |
| `PyYAML` | Desired state file parsing |
| `urllib3` | TLS warning suppression for self-signed certs |
| `python-dotenv` | Load credentials from `.env` file |

---

## Technologies

- [RESTCONF (RFC 8040)](https://datatracker.ietf.org/doc/html/rfc8040)
- [NETCONF (RFC 6241)](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- YANG model used: `Cisco-IOS-XE-native`

---

## Course Context

This lab is part of the **NetAcad DEVASC** (DevNet Associate) curriculum — RA09.
