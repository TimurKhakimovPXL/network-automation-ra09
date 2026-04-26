"""
handlers/static_routes.py

Domain: IPv4 static routes
YANG model: Cisco-IOS-XE-native (ip/route)
Read:  RESTCONF GET  → native/ip/route
Write: NETCONF edit-config → <ip><route> subtree

Change schema in changes.yaml:
    - type: static_route
      routes:
        - prefix: 0.0.0.0
          mask: 0.0.0.0
          next_hop: 10.199.65.1
          description: Default route via backbone        # optional
        - prefix: 192.168.10.0
          mask: 255.255.255.0
          next_hop: 172.17.9.1
"""

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

RESTCONF_BASE = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/ip/route"


# ── RESTCONF ───────────────────────────────────────────────────────────────────

def _restconf_get(device_params: dict) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    return requests.get(
        RESTCONF_BASE.format(host=host),
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_routes(response: requests.Response) -> set[tuple]:
    """
    Returns a set of (prefix, mask, next_hop) tuples from the device.
    """
    data   = response.json()
    routes = data.get("Cisco-IOS-XE-native:route", {})

    # IOS XE YANG represents the route table as a list under "ip-route-interface-forwarding-list"
    entries = norm.as_list(routes.get("ip-route-interface-forwarding-list"))

    result = set()
    for entry in entries:
        prefix   = norm.normalize_ipv4(entry.get("prefix"))
        mask     = norm.normalize_mask(entry.get("mask"))
        fwd_list = norm.as_list(entry.get("fwd-list"))
        for fwd in fwd_list:
            next_hop = norm.normalize_ipv4(fwd.get("fwd"))
            if prefix and mask and next_hop:
                result.add((prefix, mask, next_hop))

    return result


def _desired_routes(change: dict) -> set[tuple]:
    return {
        (norm.normalize_ipv4(r["prefix"]),
         norm.normalize_mask(r["mask"]),
         norm.normalize_ipv4(r["next_hop"]))
        for r in change.get("routes", [])
        if norm.normalize_ipv4(r["prefix"]) is not None
        and norm.normalize_mask(r["mask"]) is not None
        and norm.normalize_ipv4(r["next_hop"]) is not None
    }


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_route_xml(routes: list[dict]) -> str:
    lines = []
    for r in routes:
        desc_xml = f"<name>{r['description']}</name>" if r.get("description") else ""
        lines.append(f"""
          <ip-route-interface-forwarding-list>
            <prefix>{r['prefix']}</prefix>
            <mask>{r['mask']}</mask>
            <fwd-list>
              <fwd>{r['next_hop']}</fwd>
              {desc_xml}
            </fwd-list>
          </ip-route-interface-forwarding-list>""")
    return "".join(lines)


def _netconf_edit(device_params: dict, routes: list[dict]) -> None:
    route_xml = _build_route_xml(routes)

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <ip>
          <route>
            {route_xml}
          </route>
        </ip>
      </native>
    </config>
    """

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def handle(device_params: dict, device_name: str, change: dict) -> dict:
    result = {
        "device_name":    device_name,
        "type":           "static_route",
        "routes_desired": len(change.get("routes", [])),
        "changed":        False,
        "verified":       False,
        "status":         None,
    }

    # 1. Read
    try:
        response = _restconf_get(device_params)
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
        current_routes = _extract_routes(response) if response.ok else set()
    except Exception as e:
        result["status"] = "read_failed"
        result["error"]  = f"Failed to parse RESTCONF response: {e}"
        return result

    desired_routes = _desired_routes(change)
    missing = desired_routes - current_routes

    if not missing:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    result["missing_routes"] = [
        {"prefix": p, "mask": m, "next_hop": n} for p, m, n in missing
    ]

    # 3. Write — only push the missing routes (additive, idempotent)
    routes_to_add = [
        r for r in change.get("routes", [])
        if (
            norm.normalize_ipv4(r["prefix"]),
            norm.normalize_mask(r["mask"]),
            norm.normalize_ipv4(r["next_hop"]),
        ) in missing
    ]

    try:
        _netconf_edit(device_params, routes_to_add)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 4. Verify
    try:
        verify_response = _restconf_get(device_params)
        if not verify_response.ok:
            result["status"] = "verify_failed"
            result["error"]  = f"Verify HTTP {verify_response.status_code}"
            return result

        verified_routes = _extract_routes(verify_response)
        still_missing   = desired_routes - verified_routes

        if not still_missing:
            result["status"]   = "success"
            result["verified"] = True
        else:
            result["status"] = "verify_mismatch"
            result["error"]  = f"{len(still_missing)} route(s) still missing after write"
            _debug.capture(device_name, "static_route", "verify",
                           verify_response, change=change, force=True)

    except Exception as e:
        result["status"] = "verify_failed"
        result["error"]  = str(e)

    return result
