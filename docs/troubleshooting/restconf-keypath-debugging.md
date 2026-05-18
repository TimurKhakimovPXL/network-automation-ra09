# RESTCONF keypath debugging

When a RESTCONF GET against a fully-qualified, keyed path returns
`{"errors": [{"error-message": "uri keypath not found"}]}`, it almost
never means the device has no configuration. It means the *path
itself* does not exist in the device's YANG model — typically because
the data lives inside an augmenting module's container that the path
skipped over.

## Diagnostic chain

### Step 1 — Confirm the symptom

    sudo -u netauto -H bash -c '
      set -a; source /opt/network-automation-ra09/.env; set +a
      curl -sk -u "$LAB_USER:$LAB_PASS" \
        -H "Accept: application/yang-data+json" \
        "https://<host>/restconf/data/<your suspected path>" \
      | python3 -m json.tool
    '

If the response is the `uri keypath not found` JSON error, do NOT
conclude the feature is unconfigured. Proceed.

### Step 2 — Walk up to the broadest parent

Remove the keyed predicate and any optional segments and GET the
broadest reasonable parent. For OSPF on IOS XE this was
`…/Cisco-IOS-XE-native:native/router`. For interface oper data it
might be `…/Cisco-IOS-XE-interfaces-oper:interfaces`. For VLAN it
could be `…/Cisco-IOS-XE-native:native/vlan`.

The response tells you where the data actually lives. Look for keys
that are module-prefixed (e.g. `Cisco-IOS-XE-ospf:router-ospf`) —
those are augmenting modules adding structure under the native model.

### Step 3 — Reconstruct the correct path

Build the path down the actual hierarchy the response revealed, using
module-prefixed keys at every augmentation boundary. Verify with one
final keyed GET.

## Worked example — OSPF on LAB-R11-C01-R01

Initial path tried (handler default):

    GET /restconf/data/Cisco-IOS-XE-native:native/router/ospf=1

Response: `uri keypath not found`.

Walk up to `…/router`:

    {
      "Cisco-IOS-XE-native:router": {
        "Cisco-IOS-XE-ospf:router-ospf": {
          "ospf": {
            "process-id": [
              { "id": 1, "router-id": "192.168.11.1" }
            ]
          }
        }
      }
    }

Correct path:

    GET /restconf/data/Cisco-IOS-XE-native:native/router/Cisco-IOS-XE-ospf:router-ospf/ospf/process-id=1

Returns the expected single entry. Read path and write payload now
agree.

## Why this matters for write paths too

The same hierarchy applies to NETCONF edit-config payloads. A write
that targets the flat path may succeed in returning `<ok/>` because
the device's CLI translation layer reconstructs scalar leaves from
arbitrary XML structure. But structured list elements (networks,
address-families, redistribute lists) silently fall on the floor when
the surrounding container is wrong. The handler's verify cycle is
only meaningful if the read path knows where to look — which means
both paths must be derived from the same diagnostic.

## When to suspect this issue

- Handler returns `read_failed` with a JSON-decode error on what
  should be an existing configuration.
- Handler returns `verify_mismatch` immediately after a write that
  reported success. Especially if the rpc-reply was bare `<ok/>` with
  no warnings.
- Working with a YANG module ending in `-ospf`, `-bgp`, `-eigrp`,
  `-hsrp`, `-ethernet`, `-vlan`, `-ip` — anything that augments the
  native model rather than living directly inside it.

## Reference

The OSPF schema discovery session that produced this SOP is captured
in `technical-notes.md` §11 and
`docs/network_automation_documentation.md` §3.5.9.
