# NETCONF connectivity troubleshooting

When NETCONF to a device fails or behaves oddly, walk down a fixed three-layer
chain before changing project code. Each layer eliminates a category of cause,
so by the time you reach the layer that fails you already know which team
owns the fix.

## The three layers

### Layer 1 — TCP reachability

```
nc -vz <ip> 830
```

A successful connect proves that routing, VRF binding, firewall policy, and
ACLs all permit the controller to reach port 830 on the device. If this fails,
the problem is in the network path — not on the device's NETCONF stack and
not in the project. Hand it to the network track.

### Layer 2 — SSH subsystem (manual NETCONF hello)

```
ssh -p 830 <user>@<ip> -s netconf
```

This opens the `netconf` SSH subsystem directly, bypassing every client
library. On success the device responds with a `<hello>` envelope listing its
NETCONF capabilities. That proves:

- `ncsshd` is running and listening on 830,
- AAA accepts the user for the `netconf` subsystem,
- the subsystem is registered in `ip ssh` config,
- the device can advertise capabilities.

If Layer 1 passes but Layer 2 fails, the device's NETCONF/SSH config is the
problem. Config track.

### Layer 3 — ncclient with the right device handler

```python
from ncclient import manager

with manager.connect(
    host="<ip>",
    port=830,
    username="<user>",
    password="<pass>",
    hostkey_verify=False,
    device_params={"name": "iosxe"},   # or "csr" for CSR1000v
) as m:
    print(m.server_capabilities)
```

If Layers 1 and 2 pass but Layer 3 fails, the failure is client-side: the
ncclient device handler, missing capabilities expected by the handler,
project-level config, or credentials passed by the project. Project track.

## Decision tree

| Layer 1 (`nc`) | Layer 2 (`ssh -s netconf`) | Layer 3 (`ncclient`) | Owner          |
| -------------- | -------------------------- | -------------------- | -------------- |
| fail           | —                          | —                    | network track  |
| pass           | fail                       | —                    | config track   |
| pass           | pass                       | fail                 | project track  |
| pass           | pass                       | pass                 | not a NETCONF problem — look elsewhere |

## Worked example: lab-dc-h-sw01

Reported symptom: NETCONF to the Catalyst C9200L-24T-4G test switch failed
from the reconciler. Initial hypothesis was that the C9200L on IOS XE 17.6.3
had a Mgmt-vrf binding limitation that prevented `netconf-yang` from
listening on the management interface — i.e. a device-side platform issue
that would need a Cisco workaround.

Running the chain from the Ubuntu controller against `172.19.11.5`:

- **Layer 1** — `nc -vz 172.19.11.5 830` — connected immediately. TCP path
  is clean, so Mgmt-vrf is not blocking the listener.
- **Layer 2** — `ssh -p 830 $LAB_USER@172.19.11.5 -s netconf` — returned
  the `<hello>` envelope with full capability list. NETCONF subsystem on
  the device is healthy.
- **Layer 3** — ncclient with `device_params={"name": "iosxe"}` connected
  and returned capabilities. ncclient with `device_params={"name": "csr"}`
  did not negotiate cleanly.

The Mgmt-vrf hypothesis was wrong. Actual root cause was project-side:
ncclient's device handler defaulted to `csr`, which is tuned for the
CSR1000v and does not negotiate cleanly against Catalyst 9000. Fixed in
commit 6444906 by making `ncclient_device_type` a per-device field in
`infra/inventory.yaml` so the switch entry can declare `iosxe` while the
CSR entry keeps `csr`.

Lesson: never patch the project against a device-side hypothesis until
Layer 1 and Layer 2 have been run from the same host the project runs on.
The chain takes two minutes and either confirms or kills the theory before
anyone touches code.

## Common gotchas

- **`.env` is not sourced in interactive shells.** When running `curl`,
  `ssh`, or an ncclient one-liner by hand, `$LAB_USER` / `$LAB_PASS` will be
  empty even though the reconciler sees them fine. Source explicitly:

  ```
  set -a; source .env; set +a
  ```

  If `.env` is mode 600 owned by `netauto` (which it should be), run the
  command as that user instead:

  ```
  sudo -u netauto -H bash -c 'set -a; source .env; set +a; <command>'
  ```

- **Hostname disambiguation.** Two Catalyst 9000s at factory defaults both
  report hostname `Switch`, so a `show running-config | include hostname`
  check is not enough to confirm you are talking to the device you think
  you are. Pull the chassis serial via NETCONF using
  `Cisco-IOS-XE-device-hardware-oper` and compare against the rack
  inventory. In the lab-dc-h-sw01 investigation this confirmed that
  172.19.11.5 (`FOC26425LPE`) and 172.19.11.6 (`FOC26425N9Y`) were two
  distinct chassis — only `.5` is in scope.
