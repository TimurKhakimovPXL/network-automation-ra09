# network-automation-ra09

This repository contains the automation used for the PXL DEVNET RA09 lab. Cisco
IOS XE devices bootstrap through ZTP, then a service on the Ubuntu controller
applies the state declared in Git over NETCONF and RESTCONF.

---

## Design

1. Lab state is stored in version-controlled YAML.
2. The supervisor selects the required state; the reconciler works out the changes.
3. A systemd service checks Git and reconciles the devices every 60 seconds.
4. Management traffic uses the out-of-band network on `GigabitEthernet0`.
5. DHCP and TFTP handle bootstrapping. The controller takes over after ZTP.

See [docs/architecture.md](docs/architecture.md) for the full discussion.

---

## Repository Structure

```
network-automation-ra09/
├── README.md                              # This file
├── .env.example                           # Credential template: copy to .env
├── requirements.txt                       # Unified runtime dependency pins
├── requirements-dev.txt                   # Runtime + test tooling
├── dispatch.py                            # Single registration site for HANDLERS (shared by reconciler + automate.py)
│
├── intent/                                # Layer 4: the control surface
│   ├── class_state.yaml                   # ← Supervisor edits this
│   └── profiles/                          # Reusable device-state declarations
│       ├── blank.yaml                     # ─┐
│       ├── ospf-baseline.yaml             #  ├─ reusable templates
│       ├── routing-and-vlans.yaml         # ─┘
│       ├── c9200l-demo.yaml               # ─┐
│       ├── isr4221-demo.yaml              #  ├─ device-targeted profiles
│       └── isr4221-physical-test.yaml     # ─┘   in class_state.yaml)
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

1. **[docs/architecture.md](docs/architecture.md)**: system design, four-layer model
2. **[docs/oob_network_design.md](docs/oob_network_design.md)**: OOB network the system depends on
3. **[docs/operator_guide.md](docs/operator_guide.md)**: day-to-day usage
4. **[docs/network_automation_documentation.md](docs/network_automation_documentation.md)**: engine internals and handler authoring
5. **[docs/troubleshooting/restconf-keypath-debugging.md](docs/troubleshooting/restconf-keypath-debugging.md)**: diagnostic technique for YANG augmenting modules

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
The multi-domain engine grew out of the original interface-description lab. A
dispatcher sends each YAML change to one of 11 handlers covering interfaces,
routing, switching, DHCP, and gateway redundancy.

This engine is invoked by the reconciler in production and by `automate.py` for single-device CLI debugging. Both entry points import the same `HANDLERS` dict from `dispatch.py` at the repo root, so registering a new handler is a single edit.

Supported change types: `interface_description`, `interface_ip`, `interface_switchport`, `interface_state`, `ospf`, `static_route`, `vlan`, `etherchannel`, `dhcp_server`, `dhcp_relay`, `hsrp`

### ztp
This is the Day-0 script delivered to a wiped IOS XE device through DHCP option
67. It identifies the device from its assigned address and configures its
hostname, credentials, SSH, NETCONF, and RESTCONF. It has not yet been tested on
the rack hardware.

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
| YANG Suite | 10.125.100.231:8443 | YANG model browser and NETCONF testing (also installed locally: see docs) |
| ESXi | 10.199.64.37 | Ubuntu automation controller VM |

Rack addressing (X = rack number): C01 mgmt `172.17.X.2/28`, C02 mgmt `172.17.X.66/28` *(provisional: see [docs/oob_network_design.md](docs/oob_network_design.md) §4 for the open OOB subnet decision)*

The three currently-validated devices live outside the rack scheme on existing lab subnets: `lab-dc-h-vm10` on `10.199.64.91`, `lab-dc-h-sw01` on `172.19.11.5`, and `LAB-R11-C01-R01` on `172.19.11.2`. The rack scheme above applies once OOB is built out.

---

## Credentials

Copy `.env.example` to `.env` in the repo root and fill in your values:

```bash
cp .env.example .env
```

`.env` is gitignored and never committed.

---

## Current Status: 2026-05-18

| Item | Status |
|---|---|
| `ra09-interface-description` | Tested against real hardware RA09 |
| `network-automation` (flexible engine) | Validated against real hardware: ISR4221 17.3.4a, CSR1000v 16.9.5, C9200L 17.6.3 (2026-05-18) |
| `ztp` | Written, not yet hardware tested |
| **Reconciler (continuous loop)** | **Live on controller (lab-dc-h-vm09); writes the ISR4221 and C9200L targets and observes the CSR1000v without writes** |
| **Profiles (`intent/profiles/`)** | **Six profiles: three reusable templates plus three device-targeted profiles (isr4221-demo, isr4221-physical-test, c9200l-demo)** |
| **Inventory (`infra/inventory.yaml`)** | **22 devices catalogued (19 rack ISR4200s plus three test devices: CSR1000v `lab-dc-h-vm10` occupies the slot that would otherwise be LAB-RA09-C01-R01, plus ISR4221 and C9200L). MACs still pending for the rack fleet.** |
| **OOB network** | **Designed, not yet built (see [docs/oob_network_design.md](docs/oob_network_design.md))** |
| Ubuntu automation controller | Confirmed available on ESXi: setup with Leppens pending |
| DHCP reservations (MAC → IP) | Generator script written, awaiting MAC collection |
| YANG Suite (local) | Running at `https://localhost:8443` via Podman |

### Development and validation history

The notes below record the hardware findings that shaped the handlers. All of
these changes are now part of `main`.

**Initial code review**
- `automate.py`: ncclient device handler corrected from `"iosxe"` to `"csr"`
- `automate.py`: `load_dotenv()` path made explicit and relative to script file
- All interface handlers: NETCONF key element corrected from `<n>` to `<name>`
- `handlers/hsrp.py`: HSRP priority comparison made type-safe with `int()` cast
- `handlers/ospf.py`: RESTCONF read key corrected to `Cisco-IOS-XE-ospf:ospf`

**YANG model audit for IOS XE 16.8 and 17.3**
- `handlers/hsrp.py`: Removed wrong `xmlns` from `<standby>` (native submodule, not standalone module)
- `handlers/ospf.py`: Version-aware branching: `<mask>` on 16.x, `<wildcard>` on 17.x
- `handlers/dhcp_server.py`: Version-aware branching for default-router, dns-server, lease (all changed structure between 16.x and 17.x)
- 7 other handlers confirmed correct against YANG source files

### Round 3 (2026-05-18): OSPF schema discovery on real hardware

The first routing test used LAB-R11-C01-R01, an ISR4221 running IOS XE
17.3.4a. It uncovered three OSPF issues:

- `56a0ba7`: select the schema from the Cisco-IOS-XE-ospf YANG revision
  (queried from NETCONF capabilities at runtime), not on IOS XE release
  number. Release number is not a reliable proxy for schema revision.

- `974e38c`: use the augmenting
  router-ospf container layout, not the flat router/ospf path. RESTCONF
  read URL and NETCONF write payload both updated; <network> list now
  lands correctly under the wrapped process-id list.

- `c69e7a7`: use <wildcard> in the wrapped schema.
  Previous mask-vs-wildcard branching reflected flat-schema CLI
  translation behaviour, not the augmenting module's actual schema.
  A later update replaced that compatibility hook with separate legacy and
  wrapped schema builders selected from the advertised revision.

Idempotency proven: post-fix ospf task reports `status: success` on
first run, `status: already_correct` on subsequent runs. Same pattern
as every other tested handler.

See `docs/network_automation_documentation.md` section 3.5 and
`docs/troubleshooting/restconf-keypath-debugging.md` for the investigation.

### Round 4 (2026-05-20): Dependency cascade unification

Commit `7c54ba3` fixed four related dependency issues. Profiles refer to
task `id`, but `reconciler/reconciler.py` implemented it by change
type by tracking a `failed_types` set. As a result, failed dependencies did not
block later tasks. Fixes:

- Dependency model is ID-only across both entry points (reconciler
  was the broken one; `automate.py` was already ID-based).
- Status name unified to `skipped_due_to_dependency`: the reconciler
  had been emitting the divergent `skipped_depends_on`.
- Shared helpers (`SUCCESS_STATUSES`, `SKIPPED_STATUS`,
  `check_dependencies`, `record_outcome`) centralised in `dispatch.py`,
  the file both entry points already imported `HANDLERS` from.
- Reconciler docstring rewritten to match the actual (ID-based) model.

Verified with a synthetic cascade test (fail → skip cascade,
`already_correct` counts as success, single-string and list forms of
`depends_on` both resolve correctly). Live verification was deferred because
all current production tasks were already idempotent.

### Round 5 (2026-05-20): DHCP server YANG shape, EtherChannel protocol

Commit `e348176` contains three handler fixes checked against the vendored
Cisco IOS XE YANG
modules (`yang/ios-xe-1731/`, `yang/ios-xe-1681/`):

- `handlers/dhcp_server.py`: 17.x `<network>` now wraps `<number>` and
  `<mask>` in `<primary-network>` (was being emitted flat, matching the
  16.x shape). 17.x `<excluded-address>` ranges now wrap entries in
  `<low-high-address-list>`. Both `<pool>` and `<excluded-address>` now
  declare `xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp"`: the
  augmenting module requires it. RESTCONF parser updated to read the
  17.x `network.primary-network.{number,mask}` shape. `_validate_change()`
  rejects malformed IPv4/mask/excluded ranges before any I/O.
- `handlers/etherchannel.py`: `protocol: lacp/pagp` was being accepted
  in profiles but never written. Handler now emits
  `<channel-protocol xmlns="...ethernet">` next to `<channel-group>`,
  validates mode/protocol consistency (`lacp ↔ {active, passive}`,
  etc.), and verification now reads every member interface to check
  channel-group number, mode, and protocol as well as the Port-channel
  description.
- `handlers/ospf.py`: docstring header brought into line with the
  augmented router-ospf path the code has used since Round 3. No
  behaviour change.

The first test suite contained 31 tests. It covered the DHCP 17.x and 16.x
XML and parser shapes, DHCP input validation, EtherChannel XML and validation,
and EtherChannel member RESTCONF parsing. The suite has since grown to 46
tests, as described below.

Live verification of DHCP and EtherChannel still deferred: no current
profile exercises either handler.

### Main branch update (2026-07-17)

- Merged the flexible engine into `main` and retired the old feature branch.
- Added a shared NETCONF transaction layer for writable-running and candidate
  datastores, including validate/commit and discard-on-failure handling.
- Added legacy flat and modern wrapped OSPF schema support based on the
  advertised `Cisco-IOS-XE-ospf` revision.
- Changed maintenance wipe state from one global completed SHA to per-device
  progress, so unreachable and failed devices are retried.
- Unified runtime dependencies, added GitHub Actions CI, and expanded the suite
  to 46 unit tests.
- Added `--changes` and `--report` to the CLI debug runner so local device work
  does not require modifying tracked examples.

### Reconciler introduction (2026-04-28)

This update put a continuous reconciliation service around the existing
one-shot engine.

- Added `intent/` layer: `class_state.yaml` as the single control surface for the supervisor
- Added `infra/inventory.yaml` as the hardware inventory
- Added `intent/profiles/`: reusable Jinja2-templated device-state declarations
- Added `reconciler/`: continuous reconciliation loop (60s interval, GitOps-style)
- Added `scripts/apply_dhcp_reservations.py`: renders Windows DHCP PowerShell from inventory
- Added `docs/architecture.md`, `docs/oob_network_design.md`, `docs/operator_guide.md`
- The 11 existing handlers remained responsible for device configuration.

---

## Technologies

- [RESTCONF (RFC 8040)](https://datatracker.ietf.org/doc/html/rfc8040)
- [NETCONF (RFC 6241)](https://datatracker.ietf.org/doc/html/rfc6241)
- [Cisco IOS XE YANG Models](https://github.com/YangModels/yang/tree/main/vendor/cisco/xe)
- [Cisco IOS XE Zero Touch Provisioning](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/173/b_173_programmability_cg/zero_touch_provisioning.html)
- Jinja2 profile templates
- systemd reconciler service

---

## Course Context

NetAcad DEVASC (DevNet Associate): PXL University / DEVNET / RA09
