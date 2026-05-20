"""
handlers/etherchannel.py

Domain: EtherChannel (port-channel) configuration
YANG model:
  - Cisco-IOS-XE-native: native/interface/Port-channel (Port-channel container)
  - Cisco-IOS-XE-ethernet (namespace http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet)
    augments physical interfaces with sibling leaves <channel-group> and
    <channel-protocol>. Both must carry the ethernet namespace in NETCONF XML.

Read:  RESTCONF GET  →
       native/interface/Port-channel={channel_id}        (port-channel presence + description)
       native/interface/{type}={name}/Cisco-IOS-XE-ethernet:channel-group   (per member)
       native/interface/{type}={name}/Cisco-IOS-XE-ethernet:channel-protocol (per member, optional)
Write: NETCONF edit-config →
       <Port-channel> for the bundle itself, plus per-member
       <channel-group xmlns="…ethernet"> and <channel-protocol xmlns="…ethernet">.

mode / protocol matrix:
    mode=active  | passive            → protocol must be lacp (or omitted)
    mode=desirable | auto             → protocol must be pagp (or omitted)
    mode=on                           → protocol must be none (or omitted)
The handler enforces this combination in _validate_change so an
inconsistent profile fails early with invalid_input.

Change schema in changes.yaml:
    - type: etherchannel
      channel_id: 1
      mode: active          # active | passive | on | desirable | auto
      protocol: lacp        # lacp | pagp | none (none = static "on" channel)
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
from . import _xml as xml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/interface/Port-channel={channel_id}"
RESTCONF_MEMBER = (
    "https://{host}/restconf/data/Cisco-IOS-XE-native:native/interface/"
    "{iface_type}={iface_name}"
)

ETHERNET_NS = "http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet"

VALID_MODES     = {"active", "passive", "on", "desirable", "auto"}
VALID_PROTOCOLS = {"lacp", "pagp", "none"}
# Modes implied by each protocol; the empty set means "any mode" — used to
# allow a stricter check than just "is the mode/protocol pair listed?"
LACP_MODES = {"active", "passive"}
PAGP_MODES = {"desirable", "auto"}


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


def _restconf_get_member(device_params: dict, iface_type: str, iface_name: str) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    encoded = urllib.parse.quote(iface_name, safe="")
    url = RESTCONF_MEMBER.format(host=host, iface_type=iface_type, iface_name=encoded)

    return requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_member_channel(response: requests.Response, iface_type: str) -> dict:
    """
    Pull channel-group {number, mode} and channel-protocol from a member
    interface RESTCONF response. Augmenting-module nodes appear under their
    module-qualified key, e.g. 'Cisco-IOS-XE-ethernet:channel-group'.
    Returns {'number': int|None, 'mode': str|None, 'protocol': str|None}.
    """
    data = response.json()
    iface = data.get(f"Cisco-IOS-XE-native:{iface_type}", {}) or {}
    cg    = iface.get("Cisco-IOS-XE-ethernet:channel-group") or iface.get("channel-group") or {}
    proto = iface.get("Cisco-IOS-XE-ethernet:channel-protocol") or iface.get("channel-protocol")
    return {
        "number":   norm.normalize_int(cg.get("number")) if isinstance(cg, dict) else None,
        "mode":     norm.normalize_str(cg.get("mode")) if isinstance(cg, dict) else None,
        "protocol": norm.normalize_str(proto) if proto is not None else None,
    }


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_member_xml(member: dict, channel_id: int, mode: str, protocol: str) -> str:
    """
    Per Cisco-IOS-XE-ethernet: <channel-group> and <channel-protocol> are
    sibling leaves added by config-interface-ethernet-grouping under each
    physical interface. Both live in the Cisco-IOS-XE-ethernet namespace.

    mode=on is a static channel — no protocol is configured on the wire,
    so <channel-protocol> is omitted. For lacp/pagp we emit it explicitly
    so the device matches the declared protocol rather than relying on the
    mode→protocol implication.
    """
    iface_type = xml.interface_tag(member["interface_type"])
    iface_name = member["interface_name"]

    effective_mode = mode if protocol != "none" else "on"
    channel_group = f"""
          <channel-group xmlns="{ETHERNET_NS}">
            <number>{xml.text(channel_id)}</number>
            <mode>{xml.text(effective_mode)}</mode>
          </channel-group>"""

    if protocol in ("lacp", "pagp"):
        channel_protocol = (
            f'\n          <channel-protocol xmlns="{ETHERNET_NS}">'
            f'{xml.text(protocol)}</channel-protocol>'
        )
    else:
        channel_protocol = ""

    return f"""
        <{iface_type}>
          <name>{xml.text(iface_name)}</name>{channel_group}{channel_protocol}
        </{iface_type}>"""


def _netconf_edit(device_params: dict, change: dict) -> None:
    channel_id  = change["channel_id"]
    mode        = change.get("mode", "active")
    protocol    = change.get("protocol", "lacp")
    description = change.get("description", "")
    members     = change.get("members", [])

    desc_xml    = f"<description>{xml.text(description)}</description>" if description else ""

    member_xml = "".join(
        _build_member_xml(m, channel_id, mode, protocol)
        for m in members
    )

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface>
          <Port-channel>
            <name>{xml.text(channel_id)}</name>
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

def _validate_change(change: dict) -> str | None:
    """Reject malformed inputs before the device is touched."""
    if norm.normalize_int(change.get("channel_id")) is None:
        return f"channel_id must be an integer, got {change.get('channel_id')!r}"

    mode     = (change.get("mode") or "active")
    protocol = (change.get("protocol") or "lacp")

    if mode not in VALID_MODES:
        return f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}"
    if protocol not in VALID_PROTOCOLS:
        return f"protocol must be one of {sorted(VALID_PROTOCOLS)}, got {protocol!r}"

    # mode / protocol consistency
    if protocol == "lacp" and mode not in LACP_MODES:
        return f"protocol=lacp requires mode in {sorted(LACP_MODES)}, got mode={mode!r}"
    if protocol == "pagp" and mode not in PAGP_MODES:
        return f"protocol=pagp requires mode in {sorted(PAGP_MODES)}, got mode={mode!r}"
    if protocol == "none" and mode != "on":
        return f"protocol=none requires mode=on, got mode={mode!r}"

    members = change.get("members", []) or []
    if not members:
        return "etherchannel change has no member interfaces"
    for m in members:
        try:
            xml.interface_tag(m.get("interface_type", ""))
        except ValueError as e:
            return f"invalid member interface: {e}"
        if not m.get("interface_name"):
            return f"member missing interface_name: {m!r}"

    return None


def _verify_members(
    device_params: dict,
    members: list[dict],
    channel_id: int,
    mode: str,
    protocol: str,
) -> tuple[bool, list[str]]:
    """
    Verify that each member interface advertises the expected channel-group
    number, mode, and (when applicable) channel-protocol via RESTCONF.

    Returns (all_ok, list_of_mismatch_descriptions). A 404 on a member is
    treated as a mismatch — the augment didn't land.
    """
    effective_mode     = mode if protocol != "none" else "on"
    expected_protocol  = protocol if protocol in ("lacp", "pagp") else None
    mismatches: list[str] = []

    for m in members:
        iface_type = m["interface_type"]
        iface_name = m["interface_name"]
        label      = f"{iface_type}{iface_name}"

        try:
            r = _restconf_get_member(device_params, iface_type, iface_name)
        except Exception as e:
            mismatches.append(f"{label}: read error {e}")
            continue

        if r.status_code == 404:
            mismatches.append(f"{label}: interface not found")
            continue
        if not r.ok:
            mismatches.append(f"{label}: HTTP {r.status_code}")
            continue

        try:
            seen = _extract_member_channel(r, iface_type)
        except Exception as e:
            mismatches.append(f"{label}: parse error {e}")
            continue

        if seen["number"] != channel_id:
            mismatches.append(
                f"{label}: channel-group number {seen['number']!r} != {channel_id}"
            )
        if seen["mode"] != effective_mode:
            mismatches.append(
                f"{label}: channel-group mode {seen['mode']!r} != {effective_mode!r}"
            )
        if expected_protocol and seen["protocol"] != expected_protocol:
            mismatches.append(
                f"{label}: channel-protocol {seen['protocol']!r} != {expected_protocol!r}"
            )

    return (not mismatches), mismatches


def handle(device_params: dict, device_name: str, change: dict) -> dict:
    channel_id = norm.normalize_int(change.get("channel_id"))

    result = {
        "device_name": device_name,
        "type":        "etherchannel",
        "channel_id":  channel_id,
        "changed":     False,
        "verified":    False,
        "status":      None,
    }

    invalid = _validate_change(change)
    if invalid:
        result["status"] = "invalid_input"
        result["error"]  = invalid
        return result

    mode         = change.get("mode", "active")
    protocol     = change.get("protocol", "lacp")
    desired_desc = norm.normalize_str(change.get("description", "")) or ""
    members      = change.get("members", []) or []

    # 1. Read the Port-channel bundle (presence + description)
    try:
        response = _restconf_get(device_params, channel_id)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

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

    # 2. Idempotency check: bundle + every member must already match
    if current and current.get("description") == desired_desc:
        all_ok, mismatches = _verify_members(device_params, members, channel_id, mode, protocol)
        if all_ok:
            result["status"]   = "already_correct"
            result["verified"] = True
            return result
        result["pre_write_member_mismatches"] = mismatches

    # 3. Write
    try:
        _netconf_edit(device_params, change)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify — port-channel bundle and every member
    try:
        verify_response = _restconf_get(device_params, channel_id)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified = _extract_port_channel(verify_response)
        if not (verified and verified.get("description") == desired_desc):
            result["status"] = "verify_mismatch"
            result["error"]  = "Port-channel not found or description mismatch after write"
            _debug.capture(device_name, "etherchannel", "verify",
                           verify_response, change=change, force=True)
            return result

        all_ok, mismatches = _verify_members(device_params, members, channel_id, mode, protocol)
        if all_ok:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"member verification failed: {mismatches}"
            result["member_mismatches"] = mismatches

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
