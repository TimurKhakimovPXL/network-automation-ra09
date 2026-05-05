"""
handlers/ospf.py

Domain: OSPF process configuration
YANG model: Cisco-IOS-XE-ospf (namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-ospf)
Read:  RESTCONF GET  → native/router/ospf={process_id}
Write: NETCONF edit-config → <router><ospf> subtree

YANG structure differs between IOS XE versions:
  16.x: network list key "ip mask"     → XML element <mask>
  17.x: network list key "ip wildcard" → XML element <wildcard>
Version is detected from NETCONF capabilities at runtime.

Change schema in changes.yaml:
    - type: ospf
      process_id: 1
      router_id: 172.17.9.2
      networks:
        - prefix: 172.17.9.0
          wildcard: 0.0.0.15
          area: 0
"""

import re
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

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/router/ospf={process_id}"


# ── Version detection ──────────────────────────────────────────────────────────

def _get_ios_xe_version(device_params: dict) -> float:
    """
    Connect to device and extract the IOS XE major.minor version from NETCONF
    capabilities. Returns a float e.g. 16.8, 17.3.
    Returns 17.0 as safe default if version cannot be determined
    (17.x behaviour is the current standard going forward).
    """
    try:
        with manager.connect(**device_params) as m:
            for cap in m.server_capabilities:
                match = re.search(r'ios-xe[_-](\d+)[._](\d+)', cap, re.IGNORECASE)
                if match:
                    return float(f"{match.group(1)}.{match.group(2)}")
    except Exception:
        pass
    return 17.0


def _is_pre_17(device_params: dict) -> bool:
    """Returns True if device is running IOS XE 16.x."""
    return _get_ios_xe_version(device_params) < 17.0


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get(device_params: dict, process_id: int) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    url = RESTCONF_BASE.format(host=host, process_id=process_id)

    return requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_ospf_state(response: requests.Response, pre_17: bool) -> dict | None:
    """
    Returns a normalised dict of the current OSPF state for comparison.
    pre_17=True:  network list uses 'mask' element (16.x YANG)
    pre_17=False: network list uses 'wildcard' element (17.x YANG)
    """
    data         = response.json()
    ospf         = data.get("Cisco-IOS-XE-ospf:ospf", {})
    router_id    = ospf.get("router-id")
    wildcard_key = "mask" if pre_17 else "wildcard"

    networks = [
        {
            "prefix":   norm.normalize_ipv4(n.get("ip")),
            "wildcard": norm.normalize_ipv4(n.get(wildcard_key)),
            "area":     str(n.get("area", "")),
        }
        for n in norm.as_list(ospf.get("network"))
    ]

    return {"router_id": norm.normalize_ipv4(router_id), "networks": networks}


def _desired_state(change: dict) -> dict:
    return {
        "router_id": norm.normalize_ipv4(change.get("router_id")),
        "networks": [
            {
                "prefix":   norm.normalize_ipv4(n["prefix"]),
                "wildcard": norm.normalize_ipv4(n["wildcard"]),
                "area":     str(n["area"]),
            }
            for n in change.get("networks", [])
        ],
    }


def _states_match(current: dict, desired: dict) -> bool:
    if current["router_id"] != desired["router_id"]:
        return False
    # Compare as sets — order in YANG response is not guaranteed
    current_nets = {(n["prefix"], n["wildcard"], n["area"]) for n in current["networks"]}
    desired_nets = {(n["prefix"], n["wildcard"], n["area"]) for n in desired["networks"]}
    return current_nets == desired_nets


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_network_xml(networks: list[dict], pre_17: bool) -> str:
    """
    16.x: <mask> element (YANG key "ip mask")
    17.x: <wildcard> element (YANG key "ip wildcard")
    """
    wildcard_elem = "mask" if pre_17 else "wildcard"
    lines = []
    for n in networks:
        lines.append(f"""
          <network>
            <ip>{xml.text(n['prefix'])}</ip>
            <{wildcard_elem}>{xml.text(n['wildcard'])}</{wildcard_elem}>
            <area>{xml.text(n['area'])}</area>
          </network>""")
    return "".join(lines)


def _netconf_edit(device_params: dict, change: dict, pre_17: bool) -> None:
    process_id    = change["process_id"]
    router_id     = change.get("router_id", "")
    networks      = change.get("networks", [])

    network_xml   = _build_network_xml(networks, pre_17)
    router_id_xml = f"<router-id>{xml.text(router_id)}</router-id>" if router_id else ""

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <router>
          <ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">
            <id>{xml.text(process_id)}</id>
            {router_id_xml}
            {network_xml}
          </ospf>
        </router>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    process_id = change["process_id"]

    result = {
        "device_name": device_name,
        "type":        "ospf",
        "process_id":  process_id,
        "changed":     False,
        "verified":    False,
        "status":      None,
    }

    # Detect IOS XE version once — determines YANG element names for this device
    pre_17 = _is_pre_17(device_params)
    result["ios_xe_pre_17"] = pre_17

    # 1. Read
    try:
        response = _restconf_get(device_params, process_id)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    # 2. Compare
    if response.status_code == 404:
        current = None
    elif response.ok:
        try:
            current = _extract_ospf_state(response, pre_17)
        except Exception as e:
            result["status"] = "read_failed"
            result["error"]  = f"Failed to parse RESTCONF response: {e}"
            return result
    else:
        result["status"] = "read_failed"
        result["error"]  = f"HTTP {response.status_code}"
        return result

    desired = _desired_state(change)

    if current and _states_match(current, desired):
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 3. Write
    try:
        _netconf_edit(device_params, change, pre_17)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify
    try:
        verify_response = _restconf_get(device_params, process_id)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified = _extract_ospf_state(verify_response, pre_17)

        if _states_match(verified, desired):
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = "Post-write state does not match desired"
            _debug.capture(device_name, "ospf", "verify",
                           verify_response, change=change, force=True)

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
