# Architecture: GitOps for the Network Lab

This document describes how the controller, intent files, profiles, and device
handlers fit together.

---

## 1. Problem Statement

The PXL DEVNET lab consists of 10 racks, each with 2 Cisco IOS XE routers (ISR4200), totalling 20 devices. Today, these devices are configured manually before each class. Students wipe and power off devices at end of class, and the lab supervisor frequently has to chase students who forget. Class times are unpredictable: multiple teachers share the lab, sometimes back-to-back, sometimes not.

The manual process causes three recurring problems:

1. Configuring 20 devices two or three times a week takes too much staff time.
2. Device state varies between classes because manual work and student cleanup
   are inconsistent.
3. There is no versioned record of the configuration expected for a class.

The supervisor now selects the required state in one file. The reconciler reads
that file and brings the available hardware into line with it.

---

## 2. Architectural Principles

### 2.1 Authoritative State

Git holds the intended state. `intent/class_state.yaml` selects the state for the
next class, `infra/inventory.yaml` lists the hardware, and
`intent/profiles/<name>.yaml` contains reusable device configurations. Runtime
reports and wipe progress live on the controller, but they are not configuration
inputs.

### 2.2 Declarative Intent

The supervisor chooses the desired result rather than writing a sequence of CLI
commands. The reconciler compares that result with the device and applies the
required changes.

### 2.3 Continuous Reconciliation

The reconciler runs as a long-lived service, not as scheduled jobs. It pulls Git, observes device state, computes deltas, and applies changes in a loop. This handles three scenarios uniformly:

- Supervisor commits a change: next loop iteration picks it up
- Device boots after being off: next loop iteration sees it reachable and converges it
- Device drift detected: next loop iteration corrects it

The service is not tied to the timetable. It keeps polling, which also covers
devices that are powered on after a class has started.

### 2.4 Out-of-Band Management

SSH, NETCONF, RESTCONF, and ZTP use the OOB management network through each
device's `GigabitEthernet0` port. Profiles may configure data-plane interfaces,
but the controller does not depend on them for access. Student changes to those
interfaces therefore do not cut off the management path.

### 2.5 Reactive Services Downstream

DHCP assigns addresses, TFTP serves `ztp.py`, and blank devices start ZTP when
they boot. The controller does not schedule those steps; it begins reconciling a
device once the device is reachable.

---

## 3. The Layered Model

The system has four layers, each with a single concern:

```
                    ┌─────────────────────────────┐
                    │  Layer 4: INTENT             │
                    │  intent/class_state.yaml     │  ← Supervisor edits this
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │  Layer 3: PROFILES           │
                    │  intent/profiles/*.yaml      │  ← Reusable device states
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │  Layer 2: INVENTORY          │
                    │  infra/inventory.yaml        │  ← What hardware exists
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │  Layer 1: HANDLERS           │
                    │  labs/network-automation/    │  ← How to converge a device
                    └─────────────────────────────┘
```

The layers separate the operator's intent from the code that talks to a device.

### 3.1 Layer 1: Handlers (the engine)

11 domain handlers (interface_description, interface_ip, interface_switchport, interface_state, ospf, static_route, vlan, etherchannel, dhcp_server, dhcp_relay, hsrp). Each implements a `handle(device_params, device_name, change) -> dict` interface. Each performs the read-compare-write-verify cycle for its YANG domain.

To add a domain, write a handler and register it in the root `dispatch.py`.
Both the reconciler and `automate.py` use that registry.

### 3.2 Layer 2: Inventory

`infra/inventory.yaml` is the single source of "what hardware exists." Each device has:

- `name`: hostname for reporting
- `rack`: rack number (1-10)
- `side`: c01 or c02
- `mgmt_ip`: OOB management IP
- `wan_octet`: WAN-side host octet for addressing
- `mac`: MAC of `GigabitEthernet0` for DHCP reservation
- `platform`: hardware model
- `ios_version`: IOS XE version (affects YANG model selection)

Adding a device here makes it available to profile resolution, reconciliation,
and the DHCP reservation generator.

### 3.3 Layer 3: Profiles

`intent/profiles/<name>.yaml` declares a reusable lab state by name. A profile is a Jinja2-templated list of changes that, when resolved against an inventory entry, produces a concrete change list for one device.

Profiles are reusable: `ospf-baseline` can be applied to a class on Tuesday, then `routing-and-vlans` on Thursday. Profiles compose: a profile can declare common base changes plus per-side overrides.

### 3.4 Layer 4: Intent

`intent/class_state.yaml` declares what state the lab should be in *right now*. It selects a profile, optionally overrides per-rack, and provides a maintenance-mode flag for explicit wipes. This is the only file the supervisor edits day-to-day.

Overrides support two granularities: `overrides.racks[<RAxx>]` applies to every device in a rack, and `overrides.devices[<device-name>]` applies to a single device. Device-level overrides take precedence over rack-level overrides, which take precedence over `session.pre_class`. This matters when a rack contains heterogeneous hardware (e.g. a router and a switch sharing a rack): a per-device override pins each device to a profile it actually supports without forcing the supervisor to split the rack into a separate session. Legacy flat rack keys (`overrides.RA09: { ... }`) remain honoured for backward compatibility.

Three modes are supported. `blank` removes managed configuration,
`preconfigured` applies a named profile, and `observe` reports reachability
without writing or wiping. Observe mode is useful when a device is already in
inventory but its platform is not supported yet. Those devices are also omitted
from `maintenance.wipe_now` runs.

---

## 4. The Reconciliation Loop

The reconciler runs continuously. Each iteration executes the following:

```
1. PULL   : git pull --ff-only from the branch's configured upstream
2. PARSE  : load intent/class_state.yaml + infra/inventory.yaml + selected profile
3. RESOLVE: render profile against inventory → per-device target state
4. OBSERVE: for each device, probe reachability; if reachable, read current state via RESTCONF
5. DIFF   : compare target state vs observed state → per-device delta list
6. CONVERGE: for each device with a non-empty delta:
                if reachable: invoke automate.apply(device, delta)
                if unreachable: log "pending" and skip
7. WIPE   : if maintenance.wipe_now is true:
                wipe each not-yet-completed reachable device via SSH
                retain per-device progress and retry failures/unreachable devices
8. REPORT : write report.json to /var/lib/network-automation/reports/
9. SLEEP  : wait 60 seconds, then loop
```

### 4.1 Failure Modes

The reconciler must continue running through all of these:

- **Git pull fails** (network blip, GitHub outage): use last successfully-pulled state
- **YAML invalid** (supervisor typo): log error, do nothing, retry next iteration
- **Single device unreachable**: skip, log, continue with other devices
- **Single handler fails**: record failure in report, move to next change
- **Single device's full convergence fails**: continue with remaining devices

Validation errors stop that iteration and are written to the log. The reconciler
does not roll Git back; the operator fixes the YAML and commits the correction.

### 4.2 The `wipe_now` Mechanism

Setting `maintenance.wipe_now: true` and committing triggers a one-shot wipe. The reconciler stores the commit SHA plus the names of devices successfully wiped in `/var/lib/network-automation/wipe-state.json`. On each iteration:

```
if wipe_now is true:
    eligible = all non-observe devices
    remaining = eligible - devices_completed_for_current_sha
    wipe remaining reachable devices
    record each successful device
    retry failed or unreachable devices next iteration
```

The supervisor manually sets `wipe_now: false` on the next commit to keep the file clean. Once every eligible device is recorded, leaving `wipe_now: true` does nothing for that commit. A new commit creates a new wipe identity. This avoids both repeated successful wipes and the old failure mode where one successful device caused all unreachable devices to be skipped permanently.

---

## 5. Class State Lifecycle

A typical class session flows through these states:

```
Time     Lab State                Supervisor Action               Reconciler Action
─────────────────────────────────────────────────────────────────────────────────────
Mon 18:00 devices powered off    commits class_state with        sees devices unreachable,
          (last class ended)     pre_class.profile = blank        marks pending convergence

Tue 09:00 students arrive,       (none)                          observes devices booting,
          power on devices                                        ZTP completes (devices blank
                                                                  via OOB), no profile to apply
                                                                  (mode = blank)

Tue 09:30 lab proceeds:         (none)                          observes drift on student-
          students configure                                      configured changes, but
                                                                  ignores them (only enforces
                                                                  what's declared in profile)

Tue 12:30 class ends, students   (none)                          devices remain reachable
          leave, may or may                                       (some powered off, some not)
          not power off

Tue 14:00 next teacher arrives,  commits class_state with        within 60s, applies
          wants OSPF baseline    pre_class.profile = ospf-       ospf-baseline to all reachable
                                 baseline                        devices; logs pending for any
                                                                  powered-off ones

Tue 14:05 next teacher needs     (none)                          observes the still-powered-off
          devices on; students                                    devices boot, ZTP fires,
          power them on                                           reconciler applies ospf-baseline
                                                                  within 60s

Fri 18:00 end of week,           commits class_state with        within 60s, wipes all
          supervisor wants       maintenance.wipe_now = true      reachable devices; records
          full wipe                                               commit SHA as completed
```

---

## 6. Component Responsibilities

| Component | Lives On | Responsibility |
|---|---|---|
| Git repo (GitHub) | external | Authoritative configuration |
| Reconciler | Ubuntu controller | Continuous reconciliation loop |
| Engine (handlers) | Ubuntu controller | YANG-modelled state convergence |
| ZTP script | TFTP server | Bootstrap blank devices to reachable |
| DHCP server | school infra | Hand out leases + option 67 to booting devices |
| TFTP server | school infra | Serve `ztp.py` to devices that fetch via option 67 |
| Devices | racks | Drive their own bootstrap via IOS XE ZTP mode |

The Ubuntu controller is the *automation* server. It is not an *infrastructure* server: it does not run DHCP or TFTP. Those responsibilities stay with school IT.

---

## 7. Not in scope

The current design does not cover the following:

- **Power management.** Devices are powered on/off by humans. The pipeline does not control PDUs.
- **Student credentials provisioning.** Devices are configured for class; per-student access lives elsewhere if needed at all.
- **Data-plane traffic monitoring.** The pipeline manages config; observability of running traffic is a separate concern.
- **Multi-tenancy.** One reconciler, one Git repo, one supervisor. If multiple supervisors need conflicting states simultaneously, a more complex design is required.
- **Rollback to previous Git states.** Forward-only convergence. To revert a change, commit a new state describing the prior intent.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| **Intent** | What the lab should look like, declared in `class_state.yaml` |
| **Profile** | Reusable named device-state declaration in `intent/profiles/` |
| **Inventory** | Authoritative list of physical devices in `infra/inventory.yaml` |
| **Reconciler** | The continuous loop that converges devices to declared state |
| **Engine** | The 11 handlers + dispatcher that perform actual NETCONF/RESTCONF operations |
| **OOB** | Out-of-band management network, accessed via each device's `GigabitEthernet0` |
| **Drift** | Divergence between declared state and observed state |
| **Convergence** | Bringing observed state into alignment with declared state |
| **Pending** | A device that has a declared state to apply but is currently unreachable |
| **Observe mode** | A device mode (`mode: observe` in `class_state.yaml`) where the reconciler probes reachability and reports it but never writes or wipes. Used for devices the engine cannot yet safely manage. |

---

## 9. Future Extensions

Possible follow-up work includes:

- **Webhooks** to reduce reconciliation latency from 60s to seconds (replace polling)
- **Web UI** showing reconciliation status, drift report, recent commits
- **Profile composition** (one profile extends another) for finer reuse
- **Rollback** to a known-good Git state on validation failure
- **Multi-controller HA** for redundancy
