# Architecture — GitOps for the Network Lab

> Design specification for the network automation pipeline.
> Read this before reading any code.

---

## 1. Problem Statement

The PXL DEVNET lab consists of 10 racks, each with 2 Cisco IOS XE routers (ISR4200), totalling 20 devices. Today, these devices are configured manually before each class. Students wipe and power off devices at end of class, and the lab supervisor frequently has to chase students who forget. Class times are unpredictable — multiple teachers share the lab, sometimes back-to-back, sometimes not.

The current state has three pain points:

1. **Manual configuration** of 20 devices, 2-3 times per week, is unsustainable labour
2. **Inconsistent state** between classes because manual configuration drifts and student wipe behaviour varies
3. **No declarative record** of what the lab "should" look like at any point in time

This system replaces all three with a single declarative model: the lab supervisor edits one file declaring what state the lab should be in, and a continuous reconciliation loop converges the physical hardware to match.

---

## 2. Architectural Principles

### 2.1 Single Source of Truth

Git is the only authoritative declaration of state. `intent/class_state.yaml` declares what the lab should be. `infra/inventory.yaml` declares what hardware exists. `intent/profiles/<name>.yaml` declares reusable device states. No state lives outside Git. No manual edits to devices persist; the next reconciliation overwrites them.

### 2.2 Declarative Intent

The supervisor declares **what** the lab should look like, never **how** to make it that way. The reconciler computes the delta between declared state and observed state, then issues the minimum changes to converge. The supervisor never writes procedural commands.

### 2.3 Continuous Reconciliation

The reconciler runs as a long-lived service, not as scheduled jobs. It pulls Git, observes device state, computes deltas, and applies changes in a loop. This handles three scenarios uniformly:

- Supervisor commits a change: next loop iteration picks it up
- Device boots after being off: next loop iteration sees it reachable and converges it
- Device drift detected: next loop iteration corrects it

There is no scheduler, no cron, no manual trigger. Time-based scheduling is not used because class schedules are unpredictable and continuous reconciliation handles all timing concerns implicitly.

### 2.4 Out-of-Band Management

All control plane traffic — SSH, NETCONF, RESTCONF, ZTP — flows over the OOB management network via each device's `GigabitEthernet0` port. Data-plane interfaces are managed *by* the pipeline (declared in profiles) but never used *to reach* the device. This guarantees the pipeline can recover any device regardless of what students did to data-plane configuration.

### 2.5 Reactive Services Downstream

DHCP, TFTP, and the device themselves are reactive services downstream of Git. The DHCP server hands out leases when devices broadcast. TFTP serves `ztp.py` when devices fetch it. Devices enter ZTP when they boot blank. None of these are orchestrated by the controller — they react to system state. The controller's only role is post-bootstrap convergence.

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

Each layer depends only on the layer below it. Changes at one layer don't ripple upward.

### 3.1 Layer 1 — Handlers (the engine)

11 domain handlers (interface_description, interface_ip, interface_switchport, interface_state, ospf, static_route, vlan, etherchannel, dhcp_server, dhcp_relay, hsrp). Each implements a `handle(device_params, device_name, change) -> dict` interface. Each performs the read-compare-write-verify cycle for its YANG domain.

This layer is unchanged from the existing flexible engine. Adding a new domain is still: write a handler, register it.

### 3.2 Layer 2 — Inventory

`infra/inventory.yaml` is the single source of "what hardware exists." Each device has:

- `name` — hostname for reporting
- `rack` — rack number (1-10)
- `side` — c01 or c02
- `mgmt_ip` — OOB management IP
- `wan_octet` — WAN-side host octet for addressing
- `mac` — MAC of `GigabitEthernet0` for DHCP reservation
- `platform` — hardware model
- `ios_version` — IOS XE version (affects YANG model selection)

When a new rack comes online, you add entries here. Three downstream consumers update automatically: DHCP reservations, reconciler's device list, profile resolution.

### 3.3 Layer 3 — Profiles

`intent/profiles/<name>.yaml` declares a reusable lab state by name. A profile is a Jinja2-templated list of changes that, when resolved against an inventory entry, produces a concrete change list for one device.

Profiles are reusable: `ospf-baseline` can be applied to a class on Tuesday, then `routing-and-vlans` on Thursday. Profiles compose: a profile can declare common base changes plus per-side overrides.

### 3.4 Layer 4 — Intent

`intent/class_state.yaml` declares what state the lab should be in *right now*. It selects a profile, optionally overrides per-rack, and provides a maintenance-mode flag for explicit wipes. This is the only file the supervisor edits day-to-day.

---

## 4. The Reconciliation Loop

The reconciler runs continuously. Each iteration executes the following:

```
1. PULL    — git pull origin main
2. PARSE   — load intent/class_state.yaml + infra/inventory.yaml + selected profile
3. RESOLVE — render profile against inventory → per-device target state
4. OBSERVE — for each device, probe reachability; if reachable, read current state via RESTCONF
5. DIFF    — compare target state vs observed state → per-device delta list
6. CONVERGE— for each device with a non-empty delta:
                if reachable: invoke automate.apply(device, delta)
                if unreachable: log "pending" and skip
7. WIPE    — if maintenance.wipe_now is true and not yet acted on (timestamp check):
                wipe all reachable devices via NETCONF "write erase"
                record completion timestamp in /var/lib/network-automation/wipe-state.json
8. REPORT  — write report.json to /var/lib/network-automation/reports/
9. SLEEP   — wait 60 seconds, then loop
```

### 4.1 Failure Modes

The reconciler must continue running through all of these:

- **Git pull fails** (network blip, GitHub outage): use last successfully-pulled state
- **YAML invalid** (supervisor typo): log error, do nothing, retry next iteration
- **Single device unreachable**: skip, log, continue with other devices
- **Single handler fails**: record failure in report, move to next change
- **Single device's full convergence fails**: continue with remaining devices

Loud failure on validation errors is the policy. The reconciler does not attempt rollback to previous Git state. The supervisor sees the error, fixes the YAML, recommits.

### 4.2 The `wipe_now` Mechanism

Setting `maintenance.wipe_now: true` and committing triggers a one-shot wipe. To prevent re-wiping on every loop iteration, the reconciler stores the commit SHA of the last completed wipe in `/var/lib/network-automation/wipe-state.json`. On each iteration:

```
if wipe_now is true AND current_commit_sha != last_completed_wipe_sha:
    perform wipe
    record current_commit_sha as last_completed_wipe_sha
else:
    skip — already acted on this commit's wipe directive
```

The supervisor manually sets `wipe_now: false` on the next commit to keep the file clean. The mechanism is idempotent — leaving `wipe_now: true` permanently does nothing after the first action. This avoids the controller needing Git write access.

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

Tue 09:30 lab proceeds —         (none)                          observes drift on student-
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
| Git repo (GitHub) | external | Single source of truth |
| Reconciler | Ubuntu controller | Continuous reconciliation loop |
| Engine (handlers) | Ubuntu controller | YANG-modelled state convergence |
| ZTP script | TFTP server | Bootstrap blank devices to reachable |
| DHCP server | school infra | Hand out leases + option 67 to booting devices |
| TFTP server | school infra | Serve `ztp.py` to devices that fetch via option 67 |
| Devices | racks | Drive their own bootstrap via IOS XE ZTP mode |

The Ubuntu controller is the *automation* server. It is not an *infrastructure* server — it does not run DHCP or TFTP. Those responsibilities stay with school IT.

---

## 7. Out-of-Scope (Deliberately)

The following are not part of this design and should not be added without explicit reconsideration:

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

---

## 9. Future Extensions

These are explicitly future work, not part of the initial implementation:

- **Webhooks** to reduce reconciliation latency from 60s to seconds (replace polling)
- **Web UI** showing reconciliation status, drift report, recent commits
- **Profile composition** (one profile extends another) for finer reuse
- **Rollback** to a known-good Git state on validation failure
- **Multi-controller HA** for redundancy
- **Candidate datastore** support once IOS XE 17.6+ is the uniform baseline
