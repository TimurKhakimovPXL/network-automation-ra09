"""
handlers/ospf.py

Domain: OSPF process configuration
YANG model: Cisco-IOS-XE-ospf

Two field-observed schema families are supported:
  legacy flat (2018-era): native/router/ospf={id}, network key <mask>
  wrapped (2020-era):     native/router/router-ospf/ospf/process-id={id},
                          network key <wildcard>

The advertised YANG revision selects the schema. IOS XE release numbers are
not a reliable proxy for the model installed on a device.

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
from . import _netconf
from . import _xml as xml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_WRAPPED = (
    "https://{host}/restconf/data/Cisco-IOS-XE-native:native/router/"
    "Cisco-IOS-XE-ospf:router-ospf/ospf/process-id={process_id}"
)
RESTCONF_LEGACY = (
    "https://{host}/restconf/data/Cisco-IOS-XE-native:native/router/"
    "Cisco-IOS-XE-ospf:ospf={process_id}"
)

LEGACY_SCHEMA = "legacy_flat"
WRAPPED_SCHEMA = "wrapped"
WRAPPED_SCHEMA_MIN_REVISION = "2020-07-01"


# ── Version detection ──────────────────────────────────────────────────────────

def _get_ospf_model_revision(device_params: dict) -> str:
    """
    Return the Cisco-IOS-XE-ospf YANG model revision date the device
    advertises in its NETCONF capabilities, e.g. '2020-07-01'.

    We query the device at runtime because the YANG model revision is
    what determines the schema (and therefore the element name) that
    the device's NETCONF parser will accept. The IOS XE release number
    is NOT a reliable proxy: a device on IOS XE 17.3.4a can ship the
    mid-2020 (mask) model revision, contradicting the assumption that
    17.x always uses <wildcard>.

    Returns the first wrapped-schema revision as a conservative modern default
    if capabilities cannot be queried. This preserves the behaviour used by
    previously validated IOS XE 17.x devices.
    """
    try:
        with manager.connect(**device_params) as m:
            pattern = re.compile(
                r'Cisco-IOS-XE-ospf\?module=Cisco-IOS-XE-ospf&revision=(\d{4}-\d{2}-\d{2})'
            )
            for cap in m.server_capabilities:
                match = pattern.search(cap)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return WRAPPED_SCHEMA_MIN_REVISION


def _schema_for_revision(revision: str) -> str:
    """Map an advertised ISO revision date to its OSPF schema family."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", revision or ""):
        return LEGACY_SCHEMA if revision < WRAPPED_SCHEMA_MIN_REVISION else WRAPPED_SCHEMA
    return WRAPPED_SCHEMA


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_url(host: str, process_id: int, schema: str) -> str:
    template = RESTCONF_LEGACY if schema == LEGACY_SCHEMA else RESTCONF_WRAPPED
    return template.format(host=host, process_id=process_id)


def _restconf_get(device_params: dict, process_id: int, schema: str) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    url = _restconf_url(host, process_id, schema)

    return requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_ospf_state(response: requests.Response, schema: str) -> dict | None:
    """
    Parse the RESTCONF response for a single OSPF process.

    Keyed-list GET responses may contain either a mapping or a one-element
    list. The top-level key differs between the legacy and wrapped schemas.
    """
    data    = response.json()
    response_key = (
        "Cisco-IOS-XE-ospf:ospf"
        if schema == LEGACY_SCHEMA
        else "Cisco-IOS-XE-ospf:process-id"
    )
    entries = norm.as_list(data.get(response_key))
    if not entries:
        return None
    entry = entries[0]

    wildcard_key = "mask" if schema == LEGACY_SCHEMA else "wildcard"
    networks = [
        {
            "prefix":   norm.normalize_ipv4(n.get("ip")),
            "wildcard": norm.normalize_ipv4(n.get(wildcard_key)),
            "area":     str(n.get("area", "")),
        }
        for n in norm.as_list(entry.get("network"))
    ]

    return {
        "router_id": norm.normalize_ipv4(entry.get("router-id")),
        "networks":  networks,
    }


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

def _build_network_xml(networks: list[dict], schema: str) -> str:
    """
    The legacy flat schema names its wildcard-valued key ``mask``; the wrapped
    schema names it ``wildcard``.
    """
    wildcard_elem = "mask" if schema == LEGACY_SCHEMA else "wildcard"
    lines = []
    for n in networks:
        lines.append(f"""
          <network>
            <ip>{xml.text(n['prefix'])}</ip>
            <{wildcard_elem}>{xml.text(n['wildcard'])}</{wildcard_elem}>
            <area>{xml.text(n['area'])}</area>
          </network>""")
    return "".join(lines)


def _build_config(change: dict, schema: str) -> str:
    process_id = change["process_id"]
    router_id  = change.get("router_id", "")
    networks   = change.get("networks", [])

    network_xml   = _build_network_xml(networks, schema)
    router_id_xml = f"<router-id>{xml.text(router_id)}</router-id>" if router_id else ""

    if schema == LEGACY_SCHEMA:
        ospf_xml = f"""
          <ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">
            <id>{xml.text(process_id)}</id>
            {router_id_xml}
            {network_xml}
          </ospf>"""
    else:
        ospf_xml = f"""
          <router-ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">
            <ospf>
              <process-id>
                <id>{xml.text(process_id)}</id>
                {router_id_xml}
                {network_xml}
              </process-id>
            </ospf>
          </router-ospf>"""

    return f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <router>
          {ospf_xml}
        </router>
      </native>
    </config>
    """


def _netconf_edit(device_params: dict, change: dict, schema: str) -> None:
    _netconf.edit_config(device_params, _build_config(change, schema))


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

    ospf_model_revision = _get_ospf_model_revision(device_params)
    schema = _schema_for_revision(ospf_model_revision)
    result["ospf_model_revision"] = ospf_model_revision
    result["ospf_schema"] = schema

    # 1. Read
    try:
        response = _restconf_get(device_params, process_id, schema)
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = str(e)
        return result

    # 2. Compare
    if response.status_code == 404:
        current = None
    elif response.ok:
        try:
            current = _extract_ospf_state(response, schema)
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
        _netconf_edit(device_params, change, schema)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify
    try:
        verify_response = _restconf_get(device_params, process_id, schema)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified = _extract_ospf_state(verify_response, schema)

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
