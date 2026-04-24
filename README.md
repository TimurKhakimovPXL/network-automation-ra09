# Network Automation Labs

Python-based network automation labs from the **NetAcad DEVASC** curriculum, targeting Cisco IOS XE using model-driven programmability (NETCONF / RESTCONF / YANG).

## Labs

| Lab | Description | Protocols |
|-----|-------------|-----------|
| [ra09-interface-description](labs/ra09-interface-description/README.md) | Idempotent interface description automation | RESTCONF · NETCONF |
| [ztp](labs/ztp/README.md) | Zero Touch Provisioning — Day-0 bootstrap for wiped IOS XE devices | DHCP · TFTP · Guest Shell |

## Stack

- Python 3.8+
- NETCONF (RFC 6241) via `ncclient`
- RESTCONF (RFC 8040) via `requests`
- YANG model: `Cisco-IOS-XE-native`
- Desired state declared in YAML
- Zero Touch Provisioning via IOS XE Guest Shell + DHCP option 67

## Repository Structure

```
labs/
├── ra09-interface-description/   # RA09 – interface description automation (Day-N)
└── ztp/                          # Zero Touch Provisioning bootstrap script (Day-0)
```

## Prerequisites

Each lab has its own `requirements.txt` and `README.md`. See the individual lab directory for setup and usage instructions.

All labs assume a Cisco IOS XE device (16.8+) with NETCONF and RESTCONF enabled:

```
netconf-yang
restconf
```

For ZTP labs, a DHCP server with option 67 support and a TFTP server are also required.
