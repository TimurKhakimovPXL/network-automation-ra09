"""
handlers/dhcp_server.py

Domain: IOS XE DHCP server — pools, exclusions, DNS, default gateway
YANG model: Cisco-IOS-XE-dhcp (namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp)
            augments /ios:native/ios:ip/ios:dhcp
Read:  RESTCONF GET  → native/ip/dhcp/pool={pool_name}
Write: NETCONF edit-config → <ip><dhcp> subtree, with augmenting nodes
       (<excluded-address>, <pool>) carrying the Cisco-IOS-XE-dhcp namespace.

YANG structure differs between IOS XE versions:

  network:
    16.x: container network { leaf number; leaf mask }
          XML: <network><number>172.17.9.16</number><mask>255…</mask></network>
    17.x: container network { container primary-network { leaf number; leaf mask } }
          XML: <network><primary-network><number>…</number><mask>…</mask></primary-network></network>

  excluded-address:
    16.x: flat list excluded-address (key low-address)
          XML: <excluded-address><low-address>X</low-address><high-address>Y</high-address></excluded-address>
    17.x: container excluded-address { list low-high-address-list (key low-address, high-address) }
          XML: <excluded-address><low-high-address-list><low-address>X</low-address><high-address>Y</high-address></low-high-address-list></excluded-address>

  default-router:
    16.x: leaf-list default-router
          XML: <default-router>172.17.9.17</default-router>
    17.x: container default-router { leaf-list default-router-list }
          XML: <default-router><default-router-list>172.17.9.17</default-router-list></default-router>

  dns-server:
    16.x: leaf-list dns-server
          XML: <dns-server>10.199.64.66</dns-server>
    17.x: container dns-server { leaf-list dns-server-list }
          XML: <dns-server><dns-server-list>10.199.64.66</dns-server-list></dns-server>

  lease:
    16.x: list lease { key "Days"; leaf Days }
          XML: <lease><Days>1</Days></lease>
    17.x: container lease { choice lease { container lease-value { leaf days } } }
          XML: <lease><lease-value><days>1</days></lease-value></lease>

Version is detected from NETCONF capabilities at runtime.

Verification status:
  17.x — YANG-verified against yang/ios-xe-1731/Cisco-IOS-XE-dhcp.yang.
         Live verification deferred: no current profile exercises this handler.
  16.x — YANG-verified against yang/ios-xe-1681/Cisco-IOS-XE-dhcp.yang.
         The only 16.x device in inventory (CSR1000v 16.9.5) has not had
         this code path exercised; treat 16.x as best-effort until validated.

Change schema in changes.yaml:
    - type: dhcp_server
      excluded:
        - start: 172.17.9.1
          end: 172.17.9.5
      pools:
        - name: RA09-L-Data
          network: 172.17.9.16
          mask: 255.255.255.240
          default_router: 172.17.9.17
          dns_servers:
            - 10.199.64.66
          lease_days: 1            # optional, default 1
"""

import re
import urllib.parse
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

RESTCONF_POOL = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/ip/dhcp/pool={pool_name}"
RESTCONF_DHCP = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/ip/dhcp"

DHCP_NS = "http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp"


# ── Version detection ──────────────────────────────────────────────────────────

def _get_ios_xe_version(device_params: dict) -> float:
    """
    Connect to device and extract IOS XE major.minor version from NETCONF
    capabilities. Returns 17.0 as safe default if version cannot be determined.
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

def _restconf_get_pool(device_params: dict, pool_name: str) -> requests.Response:
    host     = device_params["host"]
    username = device_params["username"]
    password = device_params["password"]

    encoded = urllib.parse.quote(pool_name, safe="")
    url = RESTCONF_POOL.format(host=host, pool_name=encoded)

    return requests.get(
        url,
        auth=(username, password),
        headers=RESTCONF_HEADERS,
        verify=False,
        timeout=10,
    )


def _extract_pool(response: requests.Response, pre_17: bool) -> dict | None:
    data = response.json()
    pool = data.get("Cisco-IOS-XE-native:pool", {})
    if not pool:
        return None

    network_container = pool.get("network", {}) or {}

    if pre_17:
        # 16.x: container network { leaf number; leaf mask }
        net_number = network_container.get("number")
        net_mask   = network_container.get("mask")
        # 16.x: leaf-list default-router and dns-server (may be single string,
        # list, or absent — as_list handles all three uniformly)
        dns = norm.as_list(pool.get("dns-server"))
        gw  = norm.as_list(pool.get("default-router"))
    else:
        # 17.x: container network { container primary-network { number; mask } }
        primary = network_container.get("primary-network", {}) or {}
        net_number = primary.get("number")
        net_mask   = primary.get("mask")
        # 17.x: container default-router { leaf-list default-router-list }
        #        container dns-server { leaf-list dns-server-list }
        gw_container  = pool.get("default-router")
        dns_container = pool.get("dns-server")
        gw  = norm.as_list(gw_container.get("default-router-list")) if isinstance(gw_container, dict) else []
        dns = norm.as_list(dns_container.get("dns-server-list")) if isinstance(dns_container, dict) else []

    # Canonicalise IP values so e.g. zero-padding or whitespace can't fool comparison
    gw_clean  = [norm.normalize_ipv4(g) for g in gw if norm.normalize_ipv4(g) is not None]
    dns_clean = [norm.normalize_ipv4(d) for d in dns if norm.normalize_ipv4(d) is not None]

    return {
        "network":        norm.normalize_ipv4(net_number),
        "mask":           norm.normalize_mask(net_mask),
        "default_router": gw_clean[0] if gw_clean else None,
        "dns_servers":    dns_clean,
    }


def _normalize_desired_pool(pool: dict) -> dict:
    """Return a copy of the YAML-declared pool with values canonicalised
    so it can be compared apples-to-apples against _extract_pool output."""
    return {
        "name":           pool.get("name"),
        "network":        norm.normalize_ipv4(pool.get("network")),
        "mask":           norm.normalize_mask(pool.get("mask")),
        "default_router": norm.normalize_ipv4(pool.get("default_router")),
        "dns_servers":    [norm.normalize_ipv4(d) for d in pool.get("dns_servers", []) if norm.normalize_ipv4(d) is not None],
        "lease_days":     norm.normalize_int(pool.get("lease_days", 1)) or 1,
    }


def _pool_matches(current: dict, desired_pool: dict) -> bool:
    desired_norm = _normalize_desired_pool(desired_pool)
    desired_dns = sorted(desired_norm.get("dns_servers", []))
    current_dns = sorted(current.get("dns_servers", []))
    return (
        current["network"]        == desired_norm["network"] and
        current["mask"]           == desired_norm["mask"] and
        current["default_router"] == desired_norm["default_router"] and
        current_dns               == desired_dns
    )


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_excluded_xml(excluded: list[dict], pre_17: bool) -> str:
    """
    16.x: flat list at native/ip/dhcp/excluded-address (key low-address).
    17.x: container excluded-address with list low-high-address-list
          (key low-address, high-address). Container itself appears once
          even with multiple ranges; list entries repeat inside it.
    Both forms carry the Cisco-IOS-XE-dhcp namespace because the augment
    target (native/ip/dhcp) introduces them outside the native namespace.
    """
    if not excluded:
        return ""

    if pre_17:
        return "".join(
            f"""
          <excluded-address xmlns="{DHCP_NS}">
            <low-address>{xml.text(ex['start'])}</low-address>
            <high-address>{xml.text(ex['end'])}</high-address>
          </excluded-address>"""
            for ex in excluded
        )

    inner = "".join(
        f"""
            <low-high-address-list>
              <low-address>{xml.text(ex['start'])}</low-address>
              <high-address>{xml.text(ex['end'])}</high-address>
            </low-high-address-list>"""
        for ex in excluded
    )
    return f"""
          <excluded-address xmlns="{DHCP_NS}">{inner}
          </excluded-address>"""


def _build_pool_xml(pool: dict, pre_17: bool) -> str:
    lease = pool.get("lease_days", 1)

    if pre_17:
        # 16.x: leaf-list elements, lease list with capital Days, flat network
        gw_xml  = f"<default-router>{xml.text(pool['default_router'])}</default-router>" if pool.get("default_router") else ""
        dns_xml = "".join(
            f"<dns-server>{xml.text(dns)}</dns-server>"
            for dns in pool.get("dns_servers", [])
        )
        lease_xml   = f"<lease><Days>{xml.text(lease)}</Days></lease>"
        network_xml = f"""<network>
          <number>{xml.text(pool['network'])}</number>
          <mask>{xml.text(pool['mask'])}</mask>
        </network>"""
    else:
        # 17.x: container elements with inner leaf-lists, lease container/choice,
        # network wrapped in primary-network sub-container.
        gw_xml = ""
        if pool.get("default_router"):
            gw_xml = f"""<default-router>
              <default-router-list>{xml.text(pool['default_router'])}</default-router-list>
            </default-router>"""
        dns_inner = "".join(
            f"<dns-server-list>{xml.text(dns)}</dns-server-list>"
            for dns in pool.get("dns_servers", [])
        )
        dns_xml     = f"<dns-server>{dns_inner}</dns-server>" if dns_inner else ""
        lease_xml   = f"<lease><lease-value><days>{xml.text(lease)}</days></lease-value></lease>"
        network_xml = f"""<network>
          <primary-network>
            <number>{xml.text(pool['network'])}</number>
            <mask>{xml.text(pool['mask'])}</mask>
          </primary-network>
        </network>"""

    return f"""
      <pool xmlns="{DHCP_NS}">
        <id>{xml.text(pool['name'])}</id>
        {network_xml}
        {gw_xml}
        {dns_xml}
        {lease_xml}
      </pool>"""


def _netconf_edit(device_params: dict, change: dict, pre_17: bool) -> None:
    excluded     = change.get("excluded", [])
    pools        = change.get("pools", [])

    excluded_xml = _build_excluded_xml(excluded, pre_17)
    pools_xml    = "".join(_build_pool_xml(p, pre_17) for p in pools)

    payload = f"""
    <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <ip>
          <dhcp>
            {excluded_xml}
            {pools_xml}
          </dhcp>
        </ip>
      </native>
    </config>
    """

    _netconf.edit_config(device_params, payload)


# ── Handler ────────────────────────────────────────────────────────────────────

def _validate_change(change: dict) -> str | None:
    """
    Return None if the change shape is acceptable; otherwise return a
    human-readable error string. Caught before any device I/O.
    """
    pools = change.get("pools", [])
    if not pools:
        return "dhcp_server change has no 'pools' entries"

    for pool in pools:
        if not pool.get("name"):
            return f"DHCP pool missing 'name': {pool!r}"
        if norm.normalize_ipv4(pool.get("network")) is None:
            return f"DHCP pool {pool.get('name')!r} has invalid 'network': {pool.get('network')!r}"
        if norm.normalize_mask(pool.get("mask")) is None:
            return f"DHCP pool {pool.get('name')!r} has invalid 'mask': {pool.get('mask')!r}"
        if pool.get("default_router") is not None and norm.normalize_ipv4(pool["default_router"]) is None:
            return f"DHCP pool {pool.get('name')!r} has invalid 'default_router': {pool['default_router']!r}"
        for dns in pool.get("dns_servers", []) or []:
            if norm.normalize_ipv4(dns) is None:
                return f"DHCP pool {pool.get('name')!r} has invalid dns_server: {dns!r}"
        if pool.get("lease_days") is not None and norm.normalize_int(pool["lease_days"]) is None:
            return f"DHCP pool {pool.get('name')!r} has invalid 'lease_days': {pool['lease_days']!r}"

    for ex in change.get("excluded", []) or []:
        if norm.normalize_ipv4(ex.get("start")) is None or norm.normalize_ipv4(ex.get("end")) is None:
            return f"Invalid excluded-address range: {ex!r}"

    return None


def handle(device_params: dict, device_name: str, change: dict) -> dict:
    pools = change.get("pools", [])

    result = {
        "device_name":   device_name,
        "type":          "dhcp_server",
        "pools_desired": len(pools),
        "changed":       False,
        "verified":      False,
        "status":        None,
    }

    invalid = _validate_change(change)
    if invalid:
        result["status"] = "invalid_input"
        result["error"]  = invalid
        return result

    # Detect IOS XE version once — determines YANG structure for this device
    pre_17 = _is_pre_17(device_params)
    result["ios_xe_pre_17"] = pre_17

    # 1. Check each pool — if all already match, skip the write
    all_correct = True
    for pool in pools:
        try:
            response = _restconf_get_pool(device_params, pool["name"])
            if response.status_code == 404:
                all_correct = False
                break
            if not response.ok:
                result["status"] = "read_failed"
                result["error"]  = f"HTTP {response.status_code} reading pool '{pool['name']}'"
                return result
            current = _extract_pool(response, pre_17)
            if not current or not _pool_matches(current, pool):
                all_correct = False
                break
        except Exception as e:
            result["status"] = "read_failed"
            result["error"]  = str(e)
            return result

    if all_correct:
        result["status"]   = "already_correct"
        result["verified"] = True
        return result

    # 2. Write all pools and exclusions in one edit-config
    try:
        _netconf_edit(device_params, change, pre_17)
        result["changed"] = True
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"]  = str(e)
        return result

    # 3. Verify each pool
    failed_pools = []
    last_verify_response = None
    for pool in pools:
        try:
            verify_response = _restconf_get_pool(device_params, pool["name"])
            last_verify_response = verify_response
            if not verify_response.ok:
                failed_pools.append(pool["name"])
                continue
            verified = _extract_pool(verify_response, pre_17)
            if not verified or not _pool_matches(verified, pool):
                failed_pools.append(pool["name"])
                _debug.capture(device_name, "dhcp_server", "verify",
                               verify_response, change={"pool": pool}, force=True)
        except Exception:
            failed_pools.append(pool["name"])

    if not failed_pools:
        result["status"]   = "success"
        result["verified"] = True
    else:
        result["status"] = "verify_mismatch"
        result["error"]  = f"Pools failed verification: {failed_pools}"

    return result
