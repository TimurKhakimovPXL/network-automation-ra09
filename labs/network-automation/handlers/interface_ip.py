"""
handlers/interface_ip.py

Domain: IPv4 address assignment on routed interfaces
YANG model: Cisco-IOS-XE-native (interface/{type}/ip/address)
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <ip><address> subtree

Change schema in changes.yaml:
    - type: interface_ip
      interface_type: GigabitEthernet
      interface_name: "0/0/1"
      ip: 10.199.65.17
      mask: 255.255.255.224
      secondary: false          # optional, default false
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


def _extract_ip(response: requests.Response, iface_type: str) -> tuple[str | None, str | None]:
    """Returns (ip, mask) of the primary address, or (None, None) if unset."""
    data  = response.json()
    key   = f"Cisco-IOS-XE-native:{iface_type}"
    iface = data.get(key, {})
    addr  = iface.get("ip", {}).get("address", {}).get("primary", {})
    return addr.get("address"), addr.get("mask")


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _netconf_edit(device_params: dict, iface_type: str, iface_name: str,
                  ip: str, mask: str, secondary: bool) -> None:

    if secondary:
        addr_xml = f"""
          <address>
            <secondary>
              <address>{ip}</address>
              <mask>{mask}</mask>
            </secondary>
          </address>"""
    else:
        addr_xml = f"""
          <address>
            <primary>
              <address>{ip}</address>
              <mask>{mask}</mask>
            </primary>
          </address>"""

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{iface_type}>
            <n>{iface_name}</n>
            <ip>
              {addr_xml}
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
    iface_type = change["interface_type"]
    iface_name = change["interface_name"]
    desired_ip = change["ip"]
    desired_mask = change["mask"]
    secondary  = change.get("secondary", False)

    result = {
        "device_name":    device_name,
        "type":           "interface_ip",
        "interface_type": iface_type,
        "interface_name": iface_name,
        "desired_ip":     desired_ip,
        "desired_mask":   desired_mask,
        "old_ip":         None,
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
        current_ip, current_mask = _extract_ip(response, iface_type)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    result["old_ip"] = current_ip

    if current_ip == desired_ip and current_mask == desired_mask:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 3. Write
    try:
        _netconf_edit(device_params, iface_type, iface_name, desired_ip, desired_mask, secondary)
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

        verified_ip, verified_mask = _extract_ip(verify_response, iface_type)

        if verified_ip == desired_ip and verified_mask == desired_mask:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"Expected {desired_ip} {desired_mask}, got {verified_ip} {verified_mask}"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
