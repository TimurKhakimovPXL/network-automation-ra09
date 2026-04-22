# Network Automation Labs

Python-based network automation labs from the **NetAcad DEVASC** curriculum, targeting Cisco IOS XE using model-driven programmability (NETCONF / RESTCONF / YANG).

## Labs

| Lab | Description | Protocols |
|-----|-------------|-----------|
| [ra09-interface-description](labs/ra09-interface-description/README.md) | Idempotent interface description automation | RESTCONF · NETCONF |

## Stack

- Python 3.8+
- NETCONF (RFC 6241) via `ncclient`
- RESTCONF (RFC 8040) via `requests`
- YANG model: `Cisco-IOS-XE-native`
- Desired state declared in YAML

## Repository Structure

```
labs/
└── ra09-interface-description/   # RA09 – interface description automation
```

## Prerequisites

Each lab has its own `requirements.txt` and `README.md`. See the individual lab directory for setup and usage instructions.

All labs assume a Cisco IOS XE device with NETCONF and RESTCONF enabled:

```
netconf-yang
restconf
```
