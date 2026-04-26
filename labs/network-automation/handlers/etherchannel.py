"""
handlers/etherchannel.py

Domain: EtherChannel (port-channel) configuration
YANG model: Cisco-IOS-XE-native (interface/Port-channel + member interfaces)
Read:  RESTCONF GET  → native/interface/Port-channel={channel_id}
Write: NETCONF edit-config → <Port-channel> + member <GigabitEthernet> subtrees

Change schema in changes.yaml:
    - type: etherchannel
      channel_id: 1
      mode: active          # active | passive | on | desirable | auto
      protocol: lacp        # lacp | pagp | none
      description: Uplink to distribution
      members:
        - interface_type: GigabitEthernet
          interface_name: "0/1"
        - interface_type: GigabitEthernet
          interface_name: "0/2"
"""

import urllib.parse
import urllib3
import requests
from ncclient import manager

from . import _normalize as norm
from . import _debug

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/interface/Port-channel={channel_id}"


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get(device_params: dict, channel_id: int) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    return requests.get(
        RESTCONF_BASE.format(host=host, channel_id=channel_id),
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_port_channel(response: requests.Response) -> dict | None:
    data = response.json()
    pc   = data.get("Cisco-IOS-XE-native:Port-channel", {})
    if not pc:
        return None
    return {
        "description": norm.normalize_str(pc.get("description")),
    }


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_member_xml(member: dict, channel_id: int, mode: str, protocol: str) -> str:
    iface_type = member["interface_type"]
    iface_name = member["interface_name"]

    if protocol == "lacp":
        channel_xml = f"""
          <channel-group xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet">
            <number>{channel_id}</number>
            <mode>{mode}</mode>
          </channel-group>"""
    elif protocol == "pagp":
        channel_xml = f"""
          <channel-group xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet">
            <number>{channel_id}</number>
            <mode>{mode}</mode>
          </channel-group>"""
    else:
        channel_xml = f"""
          <channel-group xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet">
            <number>{channel_id}</number>
            <mode>on</mode>
          </channel-group>"""

    return f"""
        <{iface_type}>
          <name>{iface_name}</name>
          {channel_xml}
        </{iface_type}>"""


def _netconf_edit(device_params: dict, change: dict) -> None:
    channel_id  = change["channel_id"]
    mode        = change.get("mode", "active")
    protocol    = change.get("protocol", "lacp")
    description = change.get("description", "")
    members     = change.get("members", [])

    desc_xml    = f"<description>{description}</description>" if description else ""

    member_xml = "".join(
        _build_member_xml(m, channel_id, mode, protocol)
        for m in members
    )

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <Port-channel>
            <name>{channel_id}</name>
            {desc_xml}
          </Port-channel>
          {member_xml}
        </interface>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    channel_id = norm.normalize_int(change["channel_id"])

    result = {
        "device_name": device_name,
        "type":        "etherchannel",
        "channel_id":  channel_id,
        "changed":     False,
        "verified":    False,
        "status":      None,
    }

    if channel_id is None:
        result["status"] = "invalid_input"
        result["error"]  = f"channel_id must be an integer, got {change.get('channel_id')!r}"
        return result

    # 1. Read
    try:
        response = _restconf_get(device_params, channel_id)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    # 2. Compare — check if port-channel already exists with correct description
    if response.status_code == 404:
        current = None
    elif response.ok:
        try:
            current = _extract_port_channel(response)
        except Exception as e:
            result["status"] = "read_failed"
            result["error"]  = f"Failed to parse RESTCONF response: {e}"
            return result
    else:
        result["status"] = "read_failed"
        result["error"]  = f"HTTP {response.status_code}"
        return result

    desired_desc = norm.normalize_str(change.get("description", "")) or ""
    current_desc = current.get("description") if current else None

    if current and current_desc == desired_desc:
        # Port-channel exists with correct description — consider correct.
        # Full member verification would require per-interface RESTCONF reads.
        # Extend this comparison if stricter idempotency is needed.
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
        verify_response = _restconf_get(device_params, channel_id)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified = _extract_port_channel(verify_response)

        if verified and verified.get("description") == desired_desc:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = "Port-channel not found or description mismatch after write"
            _debug.capture(device_name, "etherchannel", "verify",
                           verify_response, change=change, force=True)

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
