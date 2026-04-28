# OOB Management Network — Design Specification

> Physical and logical design for the out-of-band management network that the automation pipeline depends on.
> This is the network that does not exist yet and must be built before the pipeline can be deployed at scale.

---

## 1. Why OOB

Every networking lab faces the same hazard: students misconfigure data-plane interfaces during exercises (loops, wrong IPs, OSPF storms, dropped routes), and those misconfigurations break management connectivity to the device. When the management path runs over the same physical/logical network as the data path, fixing a broken device requires console access — which means walking to the rack with a laptop and a console cable.

OOB management eliminates this. Each Cisco ISR4200 has a dedicated `GigabitEthernet0` port — separate silicon from the data-plane gigs (`gi0/0/0`–`gi0/0/4`), in its own VRF (`Mgmt-intf` by default), electrically isolated from anything students touch. By cabling all 20 devices' `GigabitEthernet0` ports to a dedicated management switch, the automation controller retains reachability *no matter what students do to the data plane*.

This is the architectural property that makes "wipe everything in one command" survivable. Without OOB, a single misconfigured student device can take its own management plane offline. With OOB, the management plane is never on the table.

---

## 2. Current State

**The OOB ports are not in use.** The ISR4200's `GigabitEthernet0` port on every device in the lab is currently unconnected. Devices are managed today via data-plane interfaces, which means:

- Lab management is fragile — students breaking data-plane breaks management
- Recovery requires physical console access via the upper RJ-45 port on the back panel
- The automation pipeline cannot be reliably operated without resolving this

Building the OOB network is the prerequisite for production deployment of the automation pipeline.

---

## 3. Target Topology

```
                     ┌───────────────────────────┐
                     │   School backbone          │
                     │   10.199.64.0/24            │
                     └───┬─────────┬─────────┬────┘
                         │         │         │
                  ┌──────▼──┐ ┌────▼────┐ ┌──▼─────┐
                  │  DHCP   │ │  TFTP   │ │ Ubuntu │
                  │ Windows │ │ server  │ │ Control│
                  │ Server  │ │         │ │  VM    │
                  │.64.66   │ │.64.134  │ │  TBD   │
                  └────┬────┘ └────┬────┘ └────┬───┘
                       │           │           │
                       └─────┬─────┴───────────┘
                             │ routed
                     ┌───────▼─────────────┐
                     │  OOB Management VLAN │
                     │  Subnet: TBD         │  ← see §4
                     └───────┬─────────────┘
                             │
                     ┌───────▼─────────┐
                     │ Mgmt L2 Switch   │
                     │ (≥24 ports)      │
                     └───────┬─────────┘
                             │
              ┌──────────────┼──────────────────┐
              │              │                  │
         ┌────▼────┐    ┌────▼────┐        ┌────▼────┐
         │ RA01-   │    │ RA01-   │  ...   │ RA10-   │
         │ C01-R01 │    │ C02-R01 │        │ C02-R01 │
         │  gi0    │    │  gi0    │        │  gi0    │
         └─────────┘    └─────────┘        └─────────┘
              ↑              ↑                  ↑
              │              │                  │
            data-plane interfaces (gi0/0/0 etc.) ──── students touch these
                            (separate physical network)
```

Critical properties:

- **Physical separation** — every device's `GigabitEthernet0` runs to the mgmt switch over its own cable, separate from data-plane cabling
- **Logical separation** — mgmt subnet is its own VLAN, optionally its own L3 subnet (recommended), with ACLs restricting who can talk to it
- **Reachability** — the DHCP, TFTP, and controller must be able to reach the OOB subnet; no other hosts should
- **No data-plane crossover** — devices use `Mgmt-intf` VRF for OOB traffic, so misconfigured data-plane routing cannot redirect mgmt traffic

---

## 4. Addressing — Open Decision

The existing codebase uses `172.17.X.0/28` per rack for management addressing:

- C01 router: `172.17.X.2/28` (subnet `172.17.X.0/28`, gw `172.17.X.1`)
- C02 router: `172.17.X.66/28` (subnet `172.17.X.64/28`, gw `172.17.X.65`)

This was inherited from the original `ra09-interface-description` lab and predates the OOB design. **Two options for the new OOB network:**

### Option A — Keep the existing scheme

Pros: zero code changes; addressing already documented in handlers, `ztp.py`, and inventory.
Cons: `172.17.0.0/16` is private space not necessarily aligned with the school's IP plan; the per-rack `/28` subdivision is unusual for a flat mgmt network.

### Option B — Single flat /24 for OOB

Allocate `10.199.X.0/24` (X to be assigned by school IT) as the OOB subnet. All 20 devices live in this single subnet:

- RA01-C01 → `10.199.X.11`
- RA01-C02 → `10.199.X.12`
- RA02-C01 → `10.199.X.13`
- ...
- RA10-C02 → `10.199.X.30`

Pros: one subnet, one DHCP scope, simpler routing, fits standard school IP plans.
Cons: requires updating `inventory.yaml`, the IP-derivation logic in `ztp.py`, and any addressing documentation.

**Decision pending:** confirm with Wim Leppens which approach he wants. If he doesn't have a preference, recommend Option B for cleanliness — the cost of updating addressing once is small, and the result is a more standard layout.

For now, this design assumes addressing is **provisional** and will be confirmed in a separate decision. All references to specific IPs in this document and in `inventory.yaml` should be treated as placeholders.

---

## 5. Hardware Requirements

Minimum hardware to build the OOB network:

| Item | Quantity | Notes |
|---|---|---|
| Cat6 patch cables, ~1m | 20 | One per device, from `gi0` to mgmt switch |
| Patch cables, sufficient length | as needed | From mgmt switch to school backbone |
| Managed L2 switch | 1 | At least 24 ports (20 devices + uplinks + spare) |
| VLAN configuration on existing infra | — | Trunk OOB VLAN to wherever DHCP/TFTP/controller live |

The mgmt switch must be **managed** (not unmanaged) to support VLAN tagging and port isolation if needed. A basic Cisco Catalyst 2960 or equivalent is sufficient — this is not a high-throughput link; mgmt traffic is small.

---

## 6. DHCP Configuration

The DHCP server (Windows Server, per current infrastructure) needs an additional scope serving the OOB subnet:

```
Scope:        OOB-Management
Subnet:       (per §4 decision)
Range:        (full subnet minus reservations)
Lease:        24 hours
Options:
  003 Router:    (default gateway for OOB subnet)
  006 DNS:       (existing DNS servers)
  066 Boot Server: 10.199.64.134
  067 Bootfile: /ztp.py
  042 NTP:       (existing NTP server)
Reservations:
  20 entries, one per device
  MAC → IP, mapped per infra/inventory.yaml
```

**Option 67 is critical.** This is what tells a wiped device in ZTP mode where to fetch `ztp.py`. Without it, ZTP fires but the device has no script to execute.

The reservations are deterministic per-device. They must match exactly what `inventory.yaml` declares — the renderer in `scripts/apply_dhcp_reservations.py` generates the reservation list from inventory and produces a PowerShell script that the supervisor runs on the Windows DHCP server.

---

## 7. Firewall and ACL Policy

The OOB subnet should be **strictly firewalled**. Only three sources should originate traffic to OOB:

| Source | Protocols | Purpose |
|---|---|---|
| Ubuntu controller | TCP/22, TCP/443, TCP/830 | SSH, RESTCONF, NETCONF |
| DHCP server | UDP/67 (responses) | Lease replies |
| TFTP server | UDP/69 | ZTP file serves |

All other inbound traffic to OOB should be blocked. **No student device, no classroom workstation, no general lab equipment should have a route to the OOB subnet.** This is the property that gives OOB its security value.

Outbound from OOB should also be restricted: devices on OOB only need to reach DHCP, TFTP, NTP, and (optionally) the school DNS. They should not have a default route to the internet.

---

## 8. Migration Plan

Bringing the OOB network online without disrupting current lab operations:

### Phase 1 — Build (no impact on current lab)

1. School IT allocates the OOB subnet (per §4 decision)
2. Procure cables and mgmt switch (§5)
3. Install mgmt switch in lab, uplink to school backbone, configure VLAN
4. Configure firewall rules per §7 — verify OOB subnet is reachable from controller, DHCP, TFTP, but not from elsewhere
5. Configure DHCP scope per §6, including option 67 — but do not yet add reservations

### Phase 2 — Patch one rack (RA09)

6. Connect RA09 C01-R01 `gi0` and C02-R01 `gi0` to the mgmt switch with patch cables
7. Add DHCP reservations for RA09's two devices
8. Console into one device, verify `gi0` is in `Mgmt-intf` VRF, configure `ip address dhcp` on it
9. Confirm the device gets a DHCP lease on the OOB subnet
10. From the controller, verify reachability over OOB
11. Run a no-op pipeline pass against RA09 over OOB; confirm convergence works

### Phase 3 — Full rollout

12. Repeat steps 6-10 for the remaining 9 racks, one at a time
13. Once all 20 devices are on OOB, deprecate any data-plane mgmt access
14. Update lab documentation to reflect OOB as the management path

### Phase 4 — ZTP validation

15. With OOB live, wipe one device on RA09 and confirm ZTP fires over OOB and brings the device back to a managed state
16. Document the boot-to-converged time as a baseline

---

## 9. Operational Verification

The OOB network is "production-ready" when all of the following are true:

- All 20 devices reachable on `GigabitEthernet0` from the Ubuntu controller via the OOB subnet
- Reachability is independent of data-plane configuration on each device (verified by deliberately misconfiguring a data-plane interface and confirming OOB still works)
- DHCP option 67 delivery confirmed via packet capture on a wiped device boot
- ZTP completes successfully end-to-end on a wiped device using the OOB path only
- Firewall rules verified — no unintended hosts can reach OOB

These checks should be repeated quarterly once in production, and after any school-network changes that could affect OOB routing or firewall posture.

---

## 10. Open Questions for Lab Supervisor

1. Which subnet should OOB use? (See §4 — confirm Option A or Option B, or propose a third)
2. Is there a school IT contact for VLAN/subnet allocation, or does Wim allocate directly?
3. Existing managed switch available, or new procurement needed for §5?
4. Existing firewall capable of enforcing §7, or do we need to add rules to school IT's firewall?
5. Confirmed ports for ZTP options to be 66 (boot server) and 67 (bootfile name)?
