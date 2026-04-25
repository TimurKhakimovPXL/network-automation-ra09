"""
handlers/interface_description.py

Domain: interface descriptions
YANG model: Cisco-IOS-XE-native
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <description> element

Change schema in changes.yaml:
    - type: interface_description
      interface_type: GigabitEthernet
      interface_name: "0/0/0"
      description: RA09-L management interface
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

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/interface"


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get(device_params: dict, interface_type: str, interface_name: str) -> dict:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    encoded_name = urllib.parse.quote(interface_name, safe="")
    url = f"{RESTCONF_BASE.format(host=host)}/{interface_type}={encoded_name}"

    response = requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )
    return response


def _extract_description(response: requests.Response, interface_type: str) -> str | None:
    data = response.json()
    key  = f"Cisco-IOS-XE-native:{interface_type}"
    iface = data.get(key, {})
    return iface.get("description")


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _netconf_edit(device_params: dict, interface_type: str, interface_name: str, description: str) -> None:
    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{interface_type}>
            <name>{interface_name}</name>
            <description>{description}</description>
          </{interface_type}>
        </interface>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    """
    Read-compare-write-verify for a single interface description.
    Returns a result dict for inclusion in report.json.
    """
    iface_type  = change["interface_type"]
    iface_name  = change["interface_name"]
    desired_desc = change["description"]

    result = {
        "device_name":        device_name,
        "type":               "interface_description",
        "interface_type":     iface_type,
        "interface_name":     iface_name,
        "desired_description": desired_desc,
        "old_description":    None,
        "new_description":    None,
        "changed":            False,
        "verified":           False,
        "status":             None,
    }

    # 1. Read current state
    try:
        response = _restconf_get(device_params, iface_type, iface_name)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    if response.status_code == 404:
        result["status"] = "interface_not_found"
        result["error"]  = f"HTTP 404 — interface {iface_type}{iface_name} not found on device"
        return result

    if not response.ok:
        result["status"] = "read_failed"
        result["error"]  = f"HTTP {response.status_code}"
        return result

    # 2. Compare
    try:
        current_desc = _extract_description(response, iface_type)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    result["old_description"] = current_desc

    if current_desc == desired_desc:
        result["status"]       = "already_correct"
        result["new_description"] = current_desc
        result["verified"]     = True
        return result

    # 3. Write
    try:
        _netconf_edit(device_params, iface_type, iface_name, desired_desc)
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

        verified_desc = _extract_description(verify_response, iface_type)
        result["new_description"] = verified_desc

        if verified_desc == desired_desc:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"Expected '{desired_desc}', got '{verified_desc}'"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
