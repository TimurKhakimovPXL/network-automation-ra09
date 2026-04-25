"""
handlers/interface_state.py

Domain: Interface admin state (shutdown / no shutdown)
YANG model: Cisco-IOS-XE-native (interface/{type}/shutdown)
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <shutdown> element presence/absence

IOS XE YANG represents shutdown state as element presence:
  <shutdown/> present = interface is administratively down
  <shutdown/> absent  = interface is up

Change schema in changes.yaml:
    - type: interface_state
      interface_type: GigabitEthernet
      interface_name: "0/0/1"
      state: up           # up | down
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


def _extract_state(response: requests.Response, iface_type: str) -> str:
    """Returns 'up' or 'down' based on presence of <shutdown> in YANG response."""
    data  = response.json()
    key   = f"Cisco-IOS-XE-native:{iface_type}"
    iface = data.get(key, {})
    # shutdown key present in response means the interface is admin down
    return "down" if "shutdown" in iface else "up"


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _netconf_edit(device_params: dict, iface_type: str, iface_name: str, state: str) -> None:
    if state == "down":
        # Add shutdown element
        shutdown_xml = "<shutdown/>"
        op = ""
    else:
        # Remove shutdown element using NETCONF delete operation
        shutdown_xml = '<shutdown xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="remove"/>'
        op = ""

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{iface_type}>
            <name>{iface_name}</name>
            {shutdown_xml}
          </{iface_type}>
        </interface>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    iface_type    = change["interface_type"]
    iface_name    = change["interface_name"]
    desired_state = change["state"].lower()

    if desired_state not in ("up", "down"):
        return {
            "device_name": device_name,
            "type":        "interface_state",
            "status":      "invalid_state",
            "error":       f"state must be 'up' or 'down', got '{desired_state}'",
        }

    result = {
        "device_name":    device_name,
        "type":           "interface_state",
        "interface_type": iface_type,
        "interface_name": iface_name,
        "desired_state":  desired_state,
        "current_state":  None,
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

    # 2. Compare
    try:
        current_state = _extract_state(response, iface_type)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    result["current_state"] = current_state

    if current_state == desired_state:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 3. Write
    try:
        _netconf_edit(device_params, iface_type, iface_name, desired_state)
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

        verified_state = _extract_state(verify_response, iface_type)

        if verified_state == desired_state:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"Expected '{desired_state}', got '{verified_state}'"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
