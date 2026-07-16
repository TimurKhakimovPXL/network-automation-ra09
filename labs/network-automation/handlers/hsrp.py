"""
handlers/hsrp.py

Domain: HSRP (Hot Standby Router Protocol) — gateway redundancy
YANG model: Cisco-IOS-XE-interfaces (submodule of Cisco-IOS-XE-native)
            standby container is in native namespace — no xmlns override needed
Read:  RESTCONF GET  → native/interface/{type}={name}
Write: NETCONF edit-config → <standby> subtree (native namespace)

Change schema in changes.yaml:
    - type: hsrp
      interface_type: GigabitEthernet
      interface_name: "0/0/0"
      group: 1
      version: 2              # optional, default 2
      priority: 110           # optional, default 100 (higher = preferred active)
      preempt: true           # optional, default true
      virtual_ip: 172.17.9.1
"""

import urllib.parse
import urllib3
import requests
from ncclient import manager

from . import _normalize as norm
from . import _debug
from . import _xml as xml

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


def _extract_hsrp(response: requests.Response, iface_type: str, group: int) -> dict | None:
    """Returns normalised HSRP state for the specified group, or None if not configured."""
    data  = response.json()
    key   = f"Cisco-IOS-XE-native:{iface_type}"
    iface = data.get(key, {})

    standby_list = norm.as_list(iface.get("standby", {}).get("standby-list"))

    for entry in standby_list:
        if norm.normalize_int(entry.get("group-number")) == group:
            return {
                "group":      group,
                "priority":   norm.normalize_int(entry.get("priority")) or 100,
                "virtual_ip": norm.normalize_ipv4(entry.get("ip", {}).get("address")),
                "preempt":    "preempt" in entry,
            }
    return None


def _states_match(current: dict, desired: dict) -> bool:
    return (
        current["virtual_ip"] == desired["virtual_ip"] and
        current["priority"]   == desired["priority"] and
        current["preempt"]    == desired["preempt"]
    )


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _netconf_edit(device_params: dict, iface_type: str, iface_name: str,
                  change: dict) -> None:
    iface_tag = xml.interface_tag(iface_type)

    group      = change["group"]
    version    = change.get("version", 2)
    priority   = change.get("priority", 100)
    virtual_ip = change["virtual_ip"]
    preempt    = change.get("preempt", True)

    preempt_xml  = "<preempt/>" if preempt else ""
    priority_xml = f"<priority>{xml.text(priority)}</priority>"

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <{iface_tag}>
            <name>{xml.text(iface_name)}</name>
            <standby>
              <version>{xml.text(version)}</version>
              <standby-list>
                <group-number>{xml.text(group)}</group-number>
                <ip>
                  <address>{xml.text(virtual_ip)}</address>
                </ip>
                {priority_xml}
                {preempt_xml}
              </standby-list>
            </standby>
          </{iface_tag}>
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
    group      = norm.normalize_int(change["group"])

    result = {
        "device_name":    device_name,
        "type":           "hsrp",
        "interface_type": iface_type,
        "interface_name": iface_name,
        "group":          group,
        "virtual_ip":     change.get("virtual_ip"),
        "changed":        False,
        "verified":       False,
        "status":         None,
    }

    if group is None:
        result["status"] = "invalid_input"
        result["error"]  = f"group must be an integer, got {change.get('group')!r}"
        return result

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
        current = _extract_hsrp(response, iface_type, group)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    desired = {
        "group":      group,
        "priority":   norm.normalize_int(change.get("priority", 100)) or 100,
        "virtual_ip": norm.normalize_ipv4(change["virtual_ip"]),
        "preempt":    norm.normalize_bool(change.get("preempt", True)) if change.get("preempt") is not None else True,
    }

    if current and _states_match(current, desired):
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

        verified = _extract_hsrp(verify_response, iface_type, group)

        if verified and _states_match(verified, desired):
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = "HSRP state after write does not match desired"
            _debug.capture(device_name, "hsrp", "verify",
                           verify_response, change=change, force=True)

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
