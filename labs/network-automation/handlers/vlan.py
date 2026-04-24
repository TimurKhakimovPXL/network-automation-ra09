"""
handlers/vlan.py

Domain: VLAN definitions on IOS XE switches
YANG model: Cisco-IOS-XE-native (vlan/vlan-list)
Read:  RESTCONF GET  → native/vlan/vlan-list={vlan_id}
Write: NETCONF edit-config → <vlan><vlan-list> subtree

Change schema in changes.yaml:
    - type: vlan
      vlans:
        - id: 91
          name: Management
        - id: 92
          name: Data_Users
        - id: 99
          name: Native
"""

import urllib3
import requests
from ncclient import manager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/vlan/vlan-list={vlan_id}"
RESTCONF_ALL  = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/vlan"


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get_all(device_params: dict) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    return requests.get(
        RESTCONF_ALL.format(host=host),
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_vlans(response: requests.Response) -> dict[int, str]:
    """
    Returns {vlan_id: vlan_name} for all VLANs currently on the device.
    """
    data  = response.json()
    vlan_data = data.get("Cisco-IOS-XE-native:vlan", {})
    vlan_list = vlan_data.get("vlan-list", [])

    return {
        int(v["id"]): v.get("name", "")
        for v in vlan_list
        if "id" in v
    }


def _desired_vlans(change: dict) -> dict[int, str]:
    return {
        int(v["id"]): v.get("name", "")
        for v in change.get("vlans", [])
    }


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_vlan_xml(vlans: dict[int, str]) -> str:
    lines = []
    for vlan_id, name in vlans.items():
        name_xml = f"<name>{name}</name>" if name else ""
        lines.append(f"""
          <vlan-list>
            <id>{vlan_id}</id>
            {name_xml}
          </vlan-list>""")
    return "".join(lines)


def _netconf_edit(device_params: dict, vlans: dict[int, str]) -> None:
    vlan_xml = _build_vlan_xml(vlans)

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <vlan>
          {vlan_xml}
        </vlan>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    result = {
        "device_name":   device_name,
        "type":          "vlan",
        "vlans_desired": len(change.get("vlans", [])),
        "changed":       False,
        "verified":      False,
        "status":        None,
    }

    # 1. Read
    try:
        response = _restconf_get_all(device_params)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    if not response.ok and response.status_code != 404:
        result["status"] = "read_failed"
        result["error"]  = f"HTTP {response.status_code}"
        return result

    # 2. Compare
    try:
        current_vlans = _extract_vlans(response) if response.ok else {}
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    desired_vlans = _desired_vlans(change)

    # Find VLANs that are missing or have wrong names
    delta = {
        vid: name
        for vid, name in desired_vlans.items()
        if current_vlans.get(vid) != name
    }

    if not delta:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    result["delta"] = [{"id": vid, "name": name} for vid, name in delta.items()]

    # 3. Write — only push the delta
    try:
        _netconf_edit(device_params, delta)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify
    try:
        verify_response = _restconf_get_all(device_params)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified_vlans   = _extract_vlans(verify_response)
        still_wrong = {
            vid: name
            for vid, name in desired_vlans.items()
            if verified_vlans.get(vid) != name
        }

        if not still_wrong:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"{len(still_wrong)} VLAN(s) still incorrect after write"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
