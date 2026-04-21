# RA09 Interface Description Automation

## Goal
Automate interface description configuration on a Cisco IOS XE router using a YAML-driven workflow.

## How it works
1. Read desired state from `changes.yaml`
2. Read current state via RESTCONF
3. Compare actual vs desired
4. Apply change via NETCONF if needed
5. Verify result via RESTCONF
6. Output results to `report.json`

## Requirements
- Python 3
- ncclient
- requests
- PyYAML
- urllib3
- Router with NETCONF and RESTCONF enabled

## Install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt


## Run
python3 automate_interface_desc.py

Result
First run → applies configuration if needed
Second run → returns already_correct (idempotent behavior)
Example

Interface:

GigabitEthernet0/0/0

Description applied:

RA09-L management interface
