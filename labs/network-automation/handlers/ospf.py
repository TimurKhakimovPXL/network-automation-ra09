"""
handlers/ospf.py

Domain: OSPF process configuration
YANG model: Cisco-IOS-XE-ospf-oper / Cisco-IOS-XE-native (router ospf)
Read:  RESTCONF GET  → native/router/ospf={process_id}
Write: NETCONF edit-config → <router><ospf> subtree

Change schema in changes.yaml:
    - type: ospf
      process_id: 1
      router_id: 172.17.9.2
      networks:
        - prefix: 172.17.9.0
          wildcard: 0.0.0.15
          area: 0
"""

import urllib3
import requests
from ncclient import manager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/router/ospf={process_id}"


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


def _extract_ospf_state(response: requests.Response, process_id: int) -> dict | None:
    """
    Returns a normalised dict of the current OSPF state for comparison,
    or None if the process does not exist.
    """
    data  = response.json()
    ospf  = data.get("Cisco-IOS-XE-native:ospf", {})

    router_id = ospf.get("router-id")
    networks  = [
        {
            "prefix":   n.get("ip"),
            "wildcard": n.get("mask"),
            "area":     str(n.get("area", "")),
        }
        for n in ospf.get("network", [])
    ]

    return {"router_id": router_id, "networks": networks}


def _desired_state(change: dict) -> dict:
    return {
        "router_id": change.get("router_id"),
        "networks": [
            {
                "prefix":   n["prefix"],
                "wildcard": n["wildcard"],
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

def _build_network_xml(networks: list[dict]) -> str:
    lines = []
    for n in networks:
        lines.append(f"""
          <network>
            <ip>{n['prefix']}</ip>
            <mask>{n['wildcard']}</mask>
            <area>{n['area']}</area>
          </network>""")
    return "".join(lines)


def _netconf_edit(device_params: dict, change: dict) -> None:
    process_id  = change["process_id"]
    router_id   = change.get("router_id", "")
    networks    = change.get("networks", [])

    network_xml = _build_network_xml(networks)
    router_id_xml = f"<router-id>{router_id}</router-id>" if router_id else ""

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <router>
          <ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">
            <id>{process_id}</id>
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
            current = _extract_ospf_state(response, process_id)
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
        _netconf_edit(device_params, change)
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

        verified = _extract_ospf_state(verify_response, process_id)

        if _states_match(verified, desired):
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = "Post-write state does not match desired"

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
