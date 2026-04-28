# network-automation-ra09

> **GitOps for the network lab.** Edit one file, commit, and the lab converges.
> Continuous reconciliation of Cisco IOS XE devices via NETCONF, RESTCONF, and ZTP — PXL DEVNET / RA09.

No CLI. No manual steps. Devices bootstrap themselves via ZTP, then receive full desired-state configuration over NETCONF and RESTCONF from a central Ubuntu automation controller running a continuous reconciliation loop.

---

## Architectural Principles

1. **Git is the single source of truth.** All state lives in YAML, version-controlled.
2. **Declarative intent.** The supervisor declares what the lab should be, not how to make it that way.
3. **Continuous reconciliation.** A reconciler service always runs, always converging.
4. **Out-of-band management.** The pipeline reaches devices only via the OOB network (`GigabitEthernet0`).
5. **Reactive infrastructure.** DHCP and TFTP serve booting devices reactively; the controller handles post-bootstrap convergence.

See [docs/architecture.md](docs/architecture.md) for the full discussion.

---

## Repository Structure

```
network-automation-ra09/
├── README.md                              # This file
├── .env.example                           # Credential template — copy to .env
│
├── intent/                                # Layer 4: the control surface
│   ├── class_state.yaml                   # ← Supervisor edits this
│   └── profiles/                          # Reusable device-state declarations
│       ├── blank.yaml
│       ├── ospf-baseline.yaml
│       └── routing-and-vlans.yaml
│
├── infra/                                 # Layer 2: hardware as code
│   ├── inventory.yaml                     # What devices exist (single source)
│   └── dhcp_reservations.yaml             # MAC → IP bindings
│
├── reconciler/                            # The continuous loop
│   ├── reconciler.py                      # Main entry point (systemd service)
│   ├── state_resolver.py                  # intent + inventory → target state
│   ├── git_watcher.py                     # Git pull and SHA tracking
│   ├── requirements.txt
│   └── systemd/
│       └── network-reconciler.service
│
├── labs/
│   ├── ra09-interface-description/        # Day-N: original single-domain lab (hardware-tested)
│   ├── network-automation/                # Day-N: flexible multi-domain engine (the engine)
│   └── ztp/                               # Day-0: Zero Touch Provisioning bootstrap
│       ├── ztp.py
│       └── deploy_to_tftp.sh              # Pushes ztp.py to TFTP server
│
├── scripts/
│   ├── apply_dhcp_reservations.py         # Renders DHCP config from inventory (Windows DHCP)
│   └── manual_reconcile.py                # One-shot reconcile (for debugging, --dry-run mode)
│
└── docs/
    ├── architecture.md                     # GitOps system design
    ├── oob_network_design.md               # OOB network specification
    ├── operator_guide.md                   # Day-to-day usage
    └── network_automation_documentation.md # Engine internals, handler authoring
```

### Read order for documentation

1. **[docs/architecture.md](docs/architecture.md)** — system design, four-layer model
2. **[docs/oob_network_design.md](docs/oob_network_design.md)** — OOB network the system depends on
3. **[docs/operator_guide.md](docs/operator_guide.md)** — day-to-day usage
4. **[docs/network_automation_documentation.md](docs/network_automation_documentation.md)** — engine internals and handler authoring

---

## Labs

### ra09-interface-description
Original single-domain automation lab. Tested against real hardware on rack RA09.
Manages interface descriptions via RESTCONF (read) and NETCONF (write). Fully idempotent.

```bash
cd labs/ra09-interface-description
python3 automate_interface_desc.py
```

### network-automation
Flexible multi-domain engine built on the same pattern as the original lab. A single dispatcher routes each change to the correct handler based on the change type. Supports 11 configuration domains — interfaces, routing, switching, DHCP, and gateway redundancy. The script never changes — only the YAML does.

This engine is now invoked **by the reconciler** rather than directly by humans. It still has a CLI entry point for debugging and one-shot runs.

Supported change types: `interface_description`, `interface_ip`, `interface_switchport`, `interface_state`, `ospf`, `static_route`, `vlan`, `etherchannel`, `dhcp_server`, `dhcp_relay`, `hsrp`

### ztp
Day-0 bootstrap script that runs automatically on a wiped IOS XE device via DHCP option 67.
Identifies the device from its DHCP-assigned IP, pushes hostname, credentials, SSH, NETCONF, and RESTCONF.
No console access required. Not yet hardware tested.

---

## Quick start

For the supervisor (operating the system):

```bash
git pull
$EDITOR intent/class_state.yaml
git commit -am "configure for tomorrow's class"
git push
# ... wait 60 seconds, lab converges to declared state
```

For checking what just happened, on the controller:

```bash
sudo cat /var/lib/network-automation/reports/latest.json
sudo journalctl -u network-reconciler -f
```

---

## Infrastructure

| Service | IP | Role |
|---|---|---|
| DHCP / DNS / NTP | 10.199.64.66 | IP assignment, name resolution, time sync |
| TFTP | 10.199.64.134 | ZTP script delivery |
| YANG Suite | 10.125.100.231:8443 | YANG model browser and NETCONF testing (also installed locally — see docs) |
| ESXi | 10.199.64.37 | Ubuntu automation controller VM |

Rack addressing (X = rack number): C01 mgmt `172.17.X.2/28`, C02 mgmt `172.17.X.66/28` *(provisional — see [docs/oob_network_design.md](docs/oob_network_design.md) §4 for the open OOB subnet decision)*

---

## Credentials

Copy `.env.example` to `.env` in the repo root and fill in your values:

```bash
cp .env.example .env
```

`.env` is gitignored and never committed.

---

## Current Status — 2026-04-28

| Item | Status |
|---|---|
| `ra09-interface-description` | Tested against real hardware RA09 |
| `network-automation` (flexible engine) | Built, bugs fixed, pending hardware validation |
| `ztp` | Written, not yet hardware tested |
| **Reconciler (continuous loop)** | **Built, pending hardware validation** |
| **Profiles (`intent/profiles/`)** | **Three template profiles in place (blank, ospf-baseline, routing-and-vlans)** |
| **Inventory (`infra/inventory.yaml`)** | **All 20 devices catalogued, MACs/IOS versions pending collection** |
| **OOB network** | **Designed, not yet built (see [docs/oob_network_design.md](docs/oob_network_design.md))** |
| Ubuntu automation controller | Confirmed available on ESXi — setup with Leppens pending |
| DHCP reservations (MAC → IP) | Generator script written, awaiting MAC collection |
| YANG Suite (local) | Running at `https://localhost:8443` via Podman |

### Bugs Fixed (2026-04-26)
All fixes are on `feature/flexible-automation-engine` and committed to the remote.

**Round 1 — Pre-hardware fixes:**
- `automate.py` — ncclient device handler corrected from `"iosxe"` to `"csr"`
- `automate.py` — `load_dotenv()` path made explicit and relative to script file
- All interface handlers — NETCONF key element corrected from `<n>` to `<name>`
- `handlers/hsrp.py` — HSRP priority comparison made type-safe with `int()` cast
- `handlers/ospf.py` — RESTCONF read key corrected to `Cisco-IOS-XE-ospf:ospf`

**Round 2 — YANG model audit (16.8 and 17.3 verified from YangModels repo):**
- `handlers/hsrp.py` — Removed wrong `xmlns` from `<standby>` (native submodule, not standalone module)
- `handlers/ospf.py` — Version-aware branching: `<mask>` on 16.x, `<wildcard>` on 17.x
- `handlers/dhcp_server.py` — Version-aware branching for default-router, dns-server, lease (all changed structure between 16.x and 17.x)
- 7 other handlers confirmed correct against YANG source files

### Architecture Refactor (2026-04-28)
Major architectural shift from one-shot scripts to continuous reconciliation. Same engine (handlers unchanged), new control plane on top.

- Added `intent/` layer — `class_state.yaml` as the single control surface for the supervisor
- Added `infra/inventory.yaml` — single source of truth for hardware catalog
- Added `intent/profiles/` — reusable Jinja2-templated device-state declarations
- Added `reconciler/` — continuous reconciliation loop (60s interval, GitOps-style)
- Added `scripts/apply_dhcp_reservations.py` — renders Windows DHCP PowerShell from inventory
- Added `docs/architecture.md`, `docs/oob_network_design.md`, `docs/operator_guide.md`
- All 11 existing handlers unchanged — they remain the data plane

---

## Technologies

- [RESTCONF (RFC 8040)](https://datatracker.ietf.org/doc/html/rfc8040)
- [NETCONF (RFC 6241)](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- [Cisco IOS XE Zero Touch Provisioning](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/173/b_173_programmability_cg/zero_touch_provisioning.html)
- Jinja2 templating for profiles (new)
- systemd service for reconciler (new)

---

## Course Context

NetAcad DEVASC (DevNet Associate) — PXL University / DEVNET / RA09
