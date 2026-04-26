"""
handlers/dhcp_server.py

Domain: IOS XE DHCP server — pools, exclusions, DNS, default gateway
YANG model: Cisco-IOS-XE-dhcp (namespace: http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp)
Read:  RESTCONF GET  → native/ip/dhcp/pool={pool_name}
Write: NETCONF edit-config → <ip><dhcp> subtree

YANG structure differs between IOS XE versions:

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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

RESTCONF_POOL = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/ip/dhcp/pool={pool_name}"
RESTCONF_DHCP = "https://{host}/restconf/data/Cisco-IOS-XE-native:native/ip/dhcp"


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

    network = pool.get("network", {})

    if pre_17:
        # 16.x: leaf-list default-router and dns-server
        dns = pool.get("dns-server", [])
        if isinstance(dns, str):
            dns = [dns]
        gw = pool.get("default-router", [])
        if isinstance(gw, str):
            gw = [gw]
    else:
        # 17.x: container default-router { leaf-list default-router-list }
        #        container dns-server { leaf-list dns-server-list }
        gw_container = pool.get("default-router", {})
        gw = gw_container.get("default-router-list", []) if isinstance(gw_container, dict) else []
        if isinstance(gw, str):
            gw = [gw]

        dns_container = pool.get("dns-server", {})
        dns = dns_container.get("dns-server-list", []) if isinstance(dns_container, dict) else []
        if isinstance(dns, str):
            dns = [dns]

    return {
        "network":        network.get("number"),
        "mask":           network.get("mask"),
        "default_router": gw[0] if gw else None,
        "dns_servers":    dns,
    }


def _pool_matches(current: dict, desired_pool: dict) -> bool:
    desired_dns = sorted(desired_pool.get("dns_servers", []))
    current_dns = sorted(current.get("dns_servers", []))
    return (
        current["network"]        == desired_pool["network"] and
        current["mask"]           == desired_pool["mask"] and
        current["default_router"] == desired_pool["default_router"] and
        current_dns               == desired_dns
    )


# ── NETCONF ────────────────────────────────────────────────────────────────────

def _build_excluded_xml(excluded: list[dict]) -> str:
    lines = []
    for ex in excluded:
        lines.append(f"""
          <excluded-address>
            <low-address>{ex['start']}</low-address>
            <high-address>{ex['end']}</high-address>
          </excluded-address>""")
    return "".join(lines)


def _build_pool_xml(pool: dict, pre_17: bool) -> str:
    lease = pool.get("lease_days", 1)

    if pre_17:
        # 16.x: leaf-list elements, lease list with capital Days
        gw_xml  = f"<default-router>{pool['default_router']}</default-router>" if pool.get("default_router") else ""
        dns_xml = "".join(
            f"<dns-server>{dns}</dns-server>"
            for dns in pool.get("dns_servers", [])
        )
        lease_xml = f"<lease><Days>{lease}</Days></lease>"
    else:
        # 17.x: container elements with inner leaf-lists, lease container/choice
        gw_xml = ""
        if pool.get("default_router"):
            gw_xml = f"""<default-router>
              <default-router-list>{pool['default_router']}</default-router-list>
            </default-router>"""
        dns_inner = "".join(
            f"<dns-server-list>{dns}</dns-server-list>"
            for dns in pool.get("dns_servers", [])
        )
        dns_xml   = f"<dns-server>{dns_inner}</dns-server>" if dns_inner else ""
        lease_xml = f"<lease><lease-value><days>{lease}</days></lease-value></lease>"

    return f"""
      <pool>
        <id>{pool['name']}</id>
        <network>
          <number>{pool['network']}</number>
          <mask>{pool['mask']}</mask>
        </network>
        {gw_xml}
        {dns_xml}
        {lease_xml}
      </pool>"""


def _netconf_edit(device_params: dict, change: dict, pre_17: bool) -> None:
    excluded     = change.get("excluded", [])
    pools        = change.get("pools", [])

    excluded_xml = _build_excluded_xml(excluded)
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

    with manager.connect(**device_params) as m:
        m.edit_config(target="running", config=payload)


# ── Handler ────────────────────────────────────────────────────────────────────

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
    for pool in pools:
        try:
            verify_response = _restconf_get_pool(device_params, pool["name"])
            if not verify_response.ok:
                failed_pools.append(pool["name"])
                continue
            verified = _extract_pool(verify_response, pre_17)
            if not verified or not _pool_matches(verified, pool):
                failed_pools.append(pool["name"])
        except Exception:
            failed_pools.append(pool["name"])

    if not failed_pools:
        result["status"]   = "success"
        result["verified"] = True
    else:
        result["status"] = "verify_mismatch"
        result["error"]  = f"Pools failed verification: {failed_pools}"

    return result
