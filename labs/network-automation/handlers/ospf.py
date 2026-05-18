"""
handlers/ospf.py

Domain: OSPF process configuration
YANG model: Cisco-IOS-XE-ospf (namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-ospf)
Read:  RESTCONF GET  → native/router/ospf={process_id}
Write: NETCONF edit-config → <router><ospf> subtree

Branches on Cisco-IOS-XE-ospf YANG model revision: revisions before
2020-11-01 use <mask>, later revisions use <wildcard>. The device's
IOS XE release number is NOT a reliable proxy; see _get_ospf_model_revision.

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

RESTCONF_BASE = (
    "https://{host}/restconf/data/Cisco-IOS-XE-native:native/router/"
    "Cisco-IOS-XE-ospf:router-ospf/ospf/process-id={process_id}"
)


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

    Returns '2020-11-01' as a safe default if the capability is not
    found; that date is at-or-after the mask→wildcard transition, so
    the handler will default to the modern <wildcard> element when
    detection fails.
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
    return "2020-11-01"


def _uses_mask_element(device_params: dict) -> bool:
    """
    Return True if the device's Cisco-IOS-XE-ospf YANG model uses <mask>
    for the network list key.

    Field-observed evidence on LAB-R11-C01-R01 (IOS XE 17.3.4a, model
    revision 2020-07-01): the wrapped router-ospf/ospf/process-id schema
    uses <wildcard>, not <mask>. Earlier diagnosis suggesting a
    2020-11-01 cutoff was based on a different device's behaviour against
    the flat (legacy, non-augmenting) schema — where the device's CLI
    translation layer expected <mask>. That path is no longer reachable
    now that the handler uses the augmented container.

    Every IOS XE device we currently target advertises the augmenting
    Cisco-IOS-XE-ospf module, and every revision of that module uses
    <wildcard>. The <mask> branch is therefore unreachable in practice.
    Keep _get_ospf_model_revision and this function in place as
    seatbelts — if we ever encounter an older model variant that
    genuinely needs <mask>, flip the return based on the revision
    string. For now: always wildcard.
    """
    return False


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


def _extract_ospf_state(response: requests.Response, uses_mask: bool) -> dict | None:
    """
    Parse the RESTCONF response for a single OSPF process.

    Path is .../router-ospf/ospf/process-id={id}, so the top-level JSON key
    is module-qualified to the augmenting module: 'Cisco-IOS-XE-ospf:process-id'.
    Per RFC 8040 a keyed list GET returns the entry as a single-element list;
    as_list handles dict vs list shape defensively.
    """
    data    = response.json()
    entries = norm.as_list(data.get("Cisco-IOS-XE-ospf:process-id"))
    if not entries:
        return None
    entry = entries[0]

    wildcard_key = "mask" if uses_mask else "wildcard"
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

def _build_network_xml(networks: list[dict], uses_mask: bool) -> str:
    """
    Older YANG revision (<2020-11-01): <mask> element (YANG key "ip mask")
    Newer YANG revision:               <wildcard> element (YANG key "ip wildcard")
    """
    wildcard_elem = "mask" if uses_mask else "wildcard"
    lines = []
    for n in networks:
        lines.append(f"""
          <network>
            <ip>{xml.text(n['prefix'])}</ip>
            <{wildcard_elem}>{xml.text(n['wildcard'])}</{wildcard_elem}>
            <area>{xml.text(n['area'])}</area>
          </network>""")
    return "".join(lines)


def _netconf_edit(device_params: dict, change: dict, uses_mask: bool) -> None:
    """
    Wrapped OSPF schema (Cisco-IOS-XE-ospf revision 2020-07-01 onward, both
    'mask' and 'wildcard' variants).

    Layout:
      native/router
        Cisco-IOS-XE-ospf:router-ospf
          ospf                  (container)
            process-id          (list, keyed on <id>)
              id
              router-id
              network*
    """
    process_id = change["process_id"]
    router_id  = change.get("router_id", "")
    networks   = change.get("networks", [])

    network_xml   = _build_network_xml(networks, uses_mask)
    router_id_xml = f"<router-id>{xml.text(router_id)}</router-id>" if router_id else ""

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <router>
          <router-ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">
            <ospf>
              <process-id>
                <id>{xml.text(process_id)}</id>
                {router_id_xml}
                {network_xml}
              </process-id>
            </ospf>
          </router-ospf>
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

    # Detect YANG model revision once — recorded in the report for audit.
    # The wrapped router-ospf schema always uses <wildcard>; see
    # _uses_mask_element for the seatbelt that would flip this if a
    # genuinely <mask>-only model variant ever surfaced.
    ospf_model_revision = _get_ospf_model_revision(device_params)
    uses_mask = False
    result["ospf_model_revision"] = ospf_model_revision

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
            current = _extract_ospf_state(response, uses_mask)
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
        _netconf_edit(device_params, change, uses_mask)
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

        verified = _extract_ospf_state(verify_response, uses_mask)

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
