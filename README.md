# Network Automation – RA09

## Overview
This project automates interface description configuration on a Cisco IOS XE router using a model-driven approach.

## Key Concepts
- RESTCONF (read state)
- NETCONF (apply configuration)
- YAML (desired state)
- Idempotent automation

## Project Structure
```text
labs/ra09-interface-description/
Lab Description

This lab demonstrates:

reading interface configuration via RESTCONF
comparing with desired state
applying changes via NETCONF
verifying results automatically
Result

The script ensures that the interface description is:

applied if missing
skipped if already correct
Technologies Used
Python
ncclient
requests
RESTCONF / NETCONF
Cisco IOS XE

---

## 2. Folder structure → good

```text
labs/ra09-interface-description
