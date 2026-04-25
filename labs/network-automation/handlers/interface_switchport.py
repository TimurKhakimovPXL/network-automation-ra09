"""
handlers/interface_switchport.py

Domain: Switchport mode and VLAN assignment on switch interfaces
YANG model: Cisco-IOS-XE-native (interface/{type}/switchport)
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <switchport> subtree

Change schema in changes.yaml:

    Access port:
    - type: interface_switchport
      interface_type: GigabitEthernet
      interface_name: "1/0/1"
      mode: access
      access_vlan: 92

    Trunk port:
    - type: interface_switchport
      interface_type: GigabitEthernet
      interface_name: "1/0/24"
      mode: trunk
      native_vlan: 99
      allowed_vlans: "91-98"    # IOS XE range string format
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


def _extract_switchport(response: requests.Response, iface_type: str) -> dict:
    """Returns normalised switchport state dict from RESTCONF response."""
    data  = response.json()
    key   = f"Cisco-IOS-XE-native:{iface_type}"
    iface = data.get(key, {})
    sw    = iface.get("switchport", {})

    mode_data   = sw.get("mode", {})
    access_data = sw.get("access", {})
    trunk_data  = sw.get("trunk", {})

    mode = None
    if "access" in mode_data:
        mode = "access"
    elif "trunk" in mode_data:
        mode = "trunk"

    return {
        "mode":          mode,
        "access_vlan":   str(access_data.get("vlan", {}).get("vlan", "")) if mode == "access" else None,
        "native_vlan":   str(trunk_data.get("native", {}).get("vlan", {}).get("vlan-id", "")) if mode == "trunk" else None,
        "allowed_vlans": trunk_data.get("allowed", {}).get("vlan", {}).get("vlans", "") if mode == "trunk" else None,
    }


def _desired_state(change: dict) -> dict:
    mode = change["mode"]
    return {
        "mode":          mode,
        "access_vlan":   str(change.get("access_vlan", "")) if mode == "access" else None,
        "native_vlan":   str(change.get("native_vlan", "")) if mode == "trunk" else None,
        "allowed_vlans": str(change.get("allowed_vlans", "")) if mode == "trunk" else None,
    }


def _states_match(current: dict, desired: dict) -> bool:
    return (
        current["mode"]          == desired["mode"] and
        current["access_vlan"]   == desired["access_vlan"] and
        current["native_vlan"]   == desired["native_vlan"] and
        current["allowed_vlans"] == desired["allowed_vlans"]
    )


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_switchport_xml(change: dict) -> str:
    mode = change["mode"]

    if mode == "access":
        vlan = change.get("access_vlan", "")
        return f"""
          <switchport>
            <mode>
              <access/>
            </mode>
            <access>
              <vlan>
                <vlan>{vlan}</vlan>
              </vlan>
            </access>
          </switchport>"""

    elif mode == "trunk":
        native_vlan   = change.get("native_vlan", "")
        allowed_vlans = change.get("allowed_vlans", "")

        native_xml  = f"<native><vlan><vlan-id>{native_vlan}</vlan-id></vlan></native>" if native_vlan else ""
        allowed_xml = f"<allowed><vlan><vlans>{allowed_vlans}</vlans></vlan></allowed>" if allowed_vlans else ""

        return f"""
          <switchport>
            <mode>
              <trunk/>
            </mode>
            <trunk>
              {native_xml}
              {allowed_xml}
            </trunk>
          </switchport>"""

    else:
        raise ValueError(f"Unsupported switchport mode: {mode}. Use 'access' or 'trunk'.")


def _netconf_edit(device_params: dict, iface_type: str, iface_name: str, change: dict) -> None:
    switchport_xml = _build_switchport_xml(change)

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{iface_type}>
            <name>{iface_name}</name>
            {switchport_xml}
          </{iface_type}>
        </interface>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    iface_type = change["interface_type"]
    iface_name = change["interface_name"]

    result = {
        "device_name":    device_name,
        "type":           "interface_switchport",
        "interface_type": iface_type,
        "interface_name": iface_name,
        "mode":           change.get("mode"),
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
        current = _extract_switchport(response, iface_type)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    desired = _desired_state(change)

    if _states_match(current, desired):
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 3. Write
    try:
        _netconf_edit(device_params, iface_type, iface_name, change)
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

        verified = _extract_switchport(verify_response, iface_type)

        if _states_match(verified, desired):
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"Switchport state after write does not match desired"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
