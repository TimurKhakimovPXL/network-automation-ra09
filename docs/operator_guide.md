# Operator Guide — Network Lab GitOps

> Day-to-day usage. Everything you need to control the lab.
> If you find yourself SSH'ing to a device, you're doing it wrong — come back here.

---

## The one-minute summary

You edit one file: `intent/class_state.yaml`. You commit it to Git. Within 60 seconds, the lab matches what you declared.

That's it.

---

## How to do common things

### Make all devices blank for the next class

```bash
git pull
# edit intent/class_state.yaml — set:
#   session.pre_class.mode: blank
#   session.pre_class.profile: null
git commit -am "blank lab for tomorrow's class"
git push
```

Within 60 seconds, all reachable devices are reset to the ZTP-bootstrap minimum. Powered-off devices will be brought to that state when they next boot.

### Pre-configure all devices with OSPF for the next class

```bash
git pull
# edit intent/class_state.yaml — set:
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
  RA09:
    mode: blank
    profile: null
```

RA09 stays blank; all other racks get the OSPF baseline.

### Wipe everything immediately

```yaml
maintenance:
  wipe_now: true
```

Commit and push. Within 60 seconds, all reachable devices are wiped. The reconciler tracks which commit triggered the wipe — re-running with the same commit does nothing. For your next change, set `wipe_now: false` again to keep the file clean.

### See what just happened

On the controller:

```bash
sudo cat /var/lib/network-automation/reports/latest.json | less
```

This is the most recent reconciliation report. Status per device, applied changes, errors. JSON, so you can grep:

```bash
sudo grep -A 3 '"status": "unreachable"' /var/lib/network-automation/reports/latest.json
```

### See what the reconciler is doing in real time

```bash
sudo journalctl -u network-reconciler -f
```

Live log stream. `Ctrl+C` to exit.

---

## When something goes wrong

### "I committed a change and nothing happened"

In order:

1. Did the push succeed? `git status` on your local clone.
2. Wait 60 seconds — that's the loop interval.
3. Check the reconciler is running: `sudo systemctl status network-reconciler`
4. Check it pulled your commit: `journalctl -u network-reconciler --since "5 minutes ago"`
5. If it pulled but didn't act, check the latest report for `errors`.

The most common cause: YAML syntax error in your edit. The reconciler logs the error and refuses to act until you fix it. Look at the journald output to see the parse error.

### "A specific device isn't being configured"

```bash
sudo cat /var/lib/network-automation/reports/latest.json | jq '.devices["LAB-RA09-C01-R01"]'
```

(Replace the device name.) Possible statuses:

- `unreachable` — device is off, or OOB cabling broken, or DHCP didn't give it a lease
- `blank_no_changes` — it's correctly blank, no action needed (this is the expected state when mode=blank)
- `converged` — it had changes applied; check `change_results` for per-change status
- `convergence_exception` — handler threw; check `traceback` field

### "The reconciler crashed"

It shouldn't crash — `Restart=always` is set in the systemd unit. If it did:

```bash
sudo systemctl status network-reconciler
sudo journalctl -u network-reconciler -n 100
```

The traceback will be in the journal. If it's an unhandled exception in the reconciler itself, that's a bug — open an issue with the traceback.

### "I want to test a profile change without affecting the lab"

```bash
# On the controller (as netauto):
cd /opt/network-automation-ra09
sudo -u netauto venv/bin/python scripts/manual_reconcile.py --dry-run
```

Resolves the target state and probes reachability without applying anything. Use this before committing risky profile changes.

---

## Adding a new profile

1. Create `intent/profiles/<your-name>.yaml` — see existing profiles for examples
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

Out of scope for this guide — see `docs/network_automation_documentation_1.md` section 3.4.2 for handler authoring. Once you write a handler:

1. It's auto-registered in the engine
2. Use the new `type:` value in any profile
3. Reconciler picks it up on the next loop

No reconciler changes needed.

---

## Adding a new device or rack

1. Edit `infra/inventory.yaml` — add the new device entry, fill in the MAC if you have it
2. Edit `infra/dhcp_reservations.yaml` if needed (subnet expansion, scope changes)
3. Run `python scripts/apply_dhcp_reservations.py` to regenerate the PowerShell
4. Hand the resulting `dhcp_reservations.ps1` to whoever runs the Windows DHCP server
5. Commit and push
6. Cable the new device's `GigabitEthernet0` to the management switch
7. Power it on — ZTP handles the rest

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

The reconciler will detect drift on its first iteration after restart and converge devices back to declared state. Anything you changed manually will be overwritten unless it matches the current declared state — that's the whole point of the architecture.

---

## What to commit, what not to commit

**Commit:**
- Anything in `intent/` — it's the control surface
- Anything in `infra/` — it's the source of truth for hardware
- Anything in `docs/` — design and operational docs
- Code in `reconciler/`, `labs/`, `scripts/`

**Never commit:**
- `.env` (it's in `.gitignore` for a reason — credentials)
- Generated PowerShell from `apply_dhcp_reservations.py` — that's a render target, not source
- Reports from `/var/lib/network-automation/` — they're observational data, not source
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
├── wipe-state.json              ← tracks which commit triggered last wipe
└── reports/
    ├── reconcile-20260428T143500Z.json  ← timestamped reports
    ├── reconcile-20260428T143600Z.json
    └── latest.json              ← symlink to most recent

/etc/systemd/system/
└── network-reconciler.service   ← service unit

journalctl -u network-reconciler  ← logs
```
