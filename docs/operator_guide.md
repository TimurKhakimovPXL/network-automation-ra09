# Operator Guide: Network Lab GitOps

This guide covers the routine changes an operator makes before and after a lab
session.

---

## The one-minute summary

Edit `intent/class_state.yaml`, commit it, and push. The controller checks for a
new commit within 60 seconds and starts reconciling reachable devices.

---

## How to do common things

### Make all devices blank for the next class

```bash
git pull
# edit intent/class_state.yaml: set:
#   session.pre_class.mode: blank
#   session.pre_class.profile: null
git commit -am "blank lab for tomorrow's class"
git push
```

Within 60 seconds, all reachable devices are reset to the ZTP-bootstrap minimum. Powered-off devices will be brought to that state when they next boot.

### Pre-configure all devices with OSPF for the next class

```bash
git pull
# edit intent/class_state.yaml: set:
#   session.pre_class.mode: preconfigured
#   session.pre_class.profile: ospf-baseline
git commit -am "OSPF baseline for routing class"
git push
```

Within 60 seconds, all reachable devices have OSPF area 0 configured on their WAN uplink.

### Pre-configure most racks but leave one blank

```yaml
session:
  pre_class:
    mode: preconfigured
    profile: ospf-baseline

overrides:
  racks:
    RA09:
      mode: blank
      profile: null
```

RA09 stays blank; all other racks get the OSPF baseline.

### Per-device overrides

When a rack holds devices of different types (e.g. a router and a switch in
the same rack), a rack-wide override may apply the wrong profile to one of
them. Use `overrides.devices` to target a single device by inventory name.

Precedence (most specific wins):

1. `overrides.devices[<device-name>]`: single device
2. `overrides.racks[<RAxx>]`: whole rack
3. `session.pre_class`: default for everything not overridden

This example assigns a separate profile to each of the three test platforms.
The default remains `blank` for other inventory entries.

```yaml
session:
  pre_class:
    mode: blank
    profile: null

overrides:
  devices:
    lab-dc-h-vm10:
      mode: preconfigured
      profile: csr1000v-test
    lab-dc-h-sw01:
      mode: preconfigured
      profile: c9200l-demo
    LAB-R11-C01-R01:
      mode: preconfigured
      profile: isr4221-physical-test
```

The CSR1000v gets `csr1000v-test`, the C9200L switch gets the minimal
`c9200l-demo` (single interface description on an unused port), and
the ISR4221 gets the full seven-task `isr4221-physical-test`. Every
other inventory device falls through to `session.pre_class` (blank).

Mixing rack and device scopes works the same way: device entries override
rack entries override the session default:

```yaml
overrides:
  racks:
    RA09:
      mode: preconfigured
      profile: ospf-baseline
  devices:
    lab-dc-h-newhw01:     # hypothetical: new device class in rack 9
                          # whose convergence is not yet validated
      mode: observe
      profile: null
```

> **Legacy syntax.** Earlier versions accepted rack keys directly under
> `overrides:` (`overrides.RA09: { ... }`) instead of nested under
> `overrides.racks:`. The flat form is still honoured for backward
> compatibility but is deprecated: use `overrides.racks` and
> `overrides.devices` in new configurations.

### Observation-only mode

`mode: observe` tells the reconciler to probe reachability on every loop
and report it, but never to write configuration and never to wipe. The
device appears in reports with `status: observed_reachable` or
`status: observed_unreachable`, and is excluded from blanket
`maintenance.wipe_now: true` runs.

Use this mode for a platform that has been added to inventory but does not yet
have tested handlers and a suitable profile. It keeps the device visible in
reports without risking a write or wipe.

```yaml
overrides:
  devices:
    lab-dc-h-newhw01:
      mode: observe
      profile: null         # ignored in observe mode; keep null for clarity
```

Once the platform has a tested profile, change the override to
`preconfigured`, or remove it so the device follows the session default.

### Wipe everything immediately

```yaml
maintenance:
  wipe_now: true
```

Commit and push. The reconciler wipes each reachable, non-observe device and
records progress per device. Failed or unreachable devices are retried on the
next loop. Set `wipe_now` back to `false` in the following commit.

### See what just happened

On the controller:

```bash
sudo cat /var/lib/network-automation/reports/latest.json | less
```

The report contains each device status, applied changes, and errors. It is JSON,
so it can also be searched directly:

```bash
sudo grep -A 3 '"status": "unreachable"' /var/lib/network-automation/reports/latest.json
```

### See what the reconciler is doing in real time

```bash
sudo journalctl -u network-reconciler -f
```

Press `Ctrl+C` to stop following the log.

---

## When something goes wrong

### "I committed a change and nothing happened"

Check the following:

1. Did the push succeed? `git status` on your local clone.
2. Wait 60 seconds: that's the loop interval.
3. Check the reconciler is running: `sudo systemctl status network-reconciler`
4. Check it pulled your commit: `journalctl -u network-reconciler --since "5 minutes ago"`
5. If it pulled but didn't act, check the latest report for `errors`.

A YAML syntax error is the usual cause. The reconciler logs the parser message
and waits for a corrected commit.

### "A specific device isn't being configured"

```bash
sudo cat /var/lib/network-automation/reports/latest.json | jq '.devices["LAB-RA09-C01-R01"]'
```

(Replace the device name.) Possible statuses:

- `unreachable`: device is off, or OOB cabling broken, or DHCP didn't give it a lease
- `observed_reachable` / `observed_unreachable`: `mode: observe`, probe-only result
- `blank_confirmed`: `mode: blank`, device probed and found to already carry no managed config (no action taken)
- `wiped_for_blank_convergence`: `mode: blank`, device had managed config and was wiped this iteration; check `wipe_result`
- `converged`: every change in `change_results` returned `success` or `already_correct`
- `converged_with_failures`: at least one change failed (e.g. `edit_failed`, `verify_mismatch`); inspect `change_results`
- `converged_with_skips`: no failures, but at least one change was skipped via `depends_on` (status `skipped_due_to_dependency`); inspect `change_results` for the unmet prerequisite
- `convergence_exception`: handler threw; check `traceback` field

### "The reconciler crashed"

The systemd unit uses `Restart=always`. If the process is not running, inspect
its status and recent log output:

```bash
sudo systemctl status network-reconciler
sudo journalctl -u network-reconciler -n 100
```

The traceback will be in the journal. Include it when reporting an unhandled
reconciler error.

### "I want to test a profile change without affecting the lab"

```bash
# On the controller (as netauto):
cd /opt/network-automation-ra09
sudo -u netauto venv/bin/python scripts/manual_reconcile.py --dry-run
```

Resolves the target state and probes reachability without applying anything. Use this before committing risky profile changes.

---

## Adding a new profile

1. Create `intent/profiles/<your-name>.yaml`: see existing profiles for examples
2. Test it dry-run: `python scripts/manual_reconcile.py --dry-run` (after editing class_state.yaml to point at it)
3. Commit, push, wait 60s
4. Verify in the latest report

Profiles use Jinja2 templating. The variables available per device are everything in that device's `infra/inventory.yaml` entry: `name`, `rack`, `side`, `mgmt_ip`, `wan_octet`, `mac`, `platform`, `ios_version`.

For per-side conditionals (C01 vs C02 routers in the same rack):

```yaml
{% if side == 'c01' %}
  # C01-specific changes
{% else %}
  # C02-specific changes
{% endif %}
```

For rack-aware addressing:

```yaml
address: "192.168.{{ rack }}.1"
```

---

## Adding a new domain (interface protocol, routing protocol, etc.)

See `docs/network_automation_documentation.md` section 3.4.2 for the handler
interface. After writing a handler:

1. Register it in the root `dispatch.py`
2. Use the new `type:` value in any profile
3. Reconciler picks it up on the next loop

The reconciler uses the same registry, so no separate reconciler change is
needed.

---

## Adding a new device or rack

1. Edit `infra/inventory.yaml`: add the new device entry, fill in the MAC if you have it
2. Edit `infra/dhcp_reservations.yaml` if needed (subnet expansion, scope changes)
3. Run `python scripts/apply_dhcp_reservations.py` to regenerate the PowerShell
4. Hand the resulting `dhcp_reservations.ps1` to whoever runs the Windows DHCP server
5. Commit and push
6. Cable the new device's `GigabitEthernet0` to the management switch
7. Power it on: ZTP handles the rest

---

## Maintenance windows

Stop the reconciler:

```bash
sudo systemctl stop network-reconciler
```

While stopped, you can manually configure devices, run experimental scripts, etc. Restart with:

```bash
sudo systemctl start network-reconciler
```

After restart, the first iteration checks for drift and reapplies the declared
state. Manual changes to managed paths may therefore be overwritten.

---

## What to commit, what not to commit

**Commit:**
- Anything in `intent/`: it's the control surface
- Anything in `infra/`: it is the hardware inventory used by the reconciler
- Anything in `docs/`: design and operational docs
- Code in `reconciler/`, `labs/`, `scripts/`

**Do not commit:**
- `.env` (it's in `.gitignore` for a reason: credentials)
- Generated PowerShell from `apply_dhcp_reservations.py`: that's a render target, not source
- Reports from `/var/lib/network-automation/`: they're observational data, not source
- The `venv/` directory

---

## Where things live on the controller

```
/opt/network-automation-ra09/   ← repo clone, owned by netauto:netauto
├── .env                         ← credentials, chmod 600
├── intent/                      ← what you edit
├── infra/                       ← hardware truth
├── reconciler/                  ← the loop
├── labs/                        ← engine and handlers
└── venv/                        ← Python virtualenv

/var/lib/network-automation/
├── wipe-state.json              ← per-device wipe progress for the active commit
└── reports/
    ├── reconcile-20260428T143500Z.json  ← timestamped reports
    ├── reconcile-20260428T143600Z.json
    └── latest.json              ← symlink to most recent

/etc/systemd/system/
└── network-reconciler.service   ← service unit

journalctl -u network-reconciler  ← logs
```
