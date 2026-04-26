"""
handlers/dhcp_relay.py

Domain: DHCP relay (ip helper-address) on SVI or routed interfaces
YANG model: Cisco-IOS-XE-native (interface/{type}/ip/helper-address)
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <ip><helper-address> subtree

Use this when the router is NOT the DHCP server but forwards client
broadcasts to a central DHCP server (e.g. 10.199.64.66 in this lab).

SEMANTICS — ADDITIVE (not converging):
    This handler adds any helper addresses declared in changes.yaml that
    are not already present. It does NOT remove helper addresses that exist
    on the device but are absent from changes.yaml.

    Rationale: helper-address entries are safe to accumulate and removing
    an unexpected entry could silently break DHCP for clients on that
    interface. The safe operation is add-only.

    If you need to remove a helper address, do it via CLI and re-run
    the automation to verify the desired entries are present.

Change schema in changes.yaml:
    - type: dhcp_relay
      interface_type: Vlan
      interface_name: "92"
      helper_addresses:
        - 10.199.64.66
"""

import urllib.parse
import urllib3
import requests
from ncclient import manager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/interface/{iface_type}={iface_name}"


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get(device_params: dict, iface_type: str, iface_name: str) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    encoded_name = urllib.parse.quote(iface_name, safe="")
    url = RESTCONF_BASE.format(host=host, iface_type=iface_type, iface_name=encoded_name)

    return requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_helpers(response: requests.Response, iface_type: str) -> set[str]:
    """Returns set of helper-address IPs currently configured on the interface."""
    data    = response.json()
    key     = f"Cisco-IOS-XE-native:{iface_type}"
    iface   = data.get(key, {})
    helpers = iface.get("ip", {}).get("helper-address", [])

    # helper-address can be a list of dicts or a single dict
    if isinstance(helpers, dict):
        helpers = [helpers]

    return {h.get("address") for h in helpers if h.get("address")}


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _netconf_edit(device_params: dict, iface_type: str, iface_name: str,
                  helper_addresses: list[str]) -> None:

    helpers_xml = "".join(
        f"<helper-address><address>{addr}</address></helper-address>"
        for addr in helper_addresses
    )

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{iface_type}>
            <name>{iface_name}</name>
            <ip>
              {helpers_xml}
            </ip>
          </{iface_type}>
        </interface>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    iface_type      = change["interface_type"]
    iface_name      = change["interface_name"]
    desired_helpers = set(change.get("helper_addresses", []))

    result = {
        "device_name":    device_name,
        "type":           "dhcp_relay",
        "interface_type": iface_type,
        "interface_name": iface_name,
        "changed":        False,
        "verified":       False,
        "status":         None,
    }

    # 1. Read
    try:
        response = _restconf_get(device_params, iface_type, iface_name)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    if response.status_code == 404:
        result["status"] = "interface_not_found"
        result["error"]  = f"HTTP 404 — {iface_type}{iface_name} not found"
        return result

    if not response.ok:
        result["status"] = "read_failed"
        result["error"]  = f"HTTP {response.status_code}"
        return result

    # 2. Compare — additive semantics: only add missing helpers, never remove
    # existing ones not in changes.yaml (removal must be done via CLI)
    try:
        current_helpers = _extract_helpers(response, iface_type)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    missing = desired_helpers - current_helpers

    if not missing:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 3. Write — only the missing helpers
    try:
        _netconf_edit(device_params, iface_type, iface_name, list(missing))
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify
    try:
        verify_response = _restconf_get(device_params, iface_type, iface_name)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified_helpers = _extract_helpers(verify_response, iface_type)
        still_missing    = desired_helpers - verified_helpers

        if not still_missing:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"Helper addresses still missing: {still_missing}"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
