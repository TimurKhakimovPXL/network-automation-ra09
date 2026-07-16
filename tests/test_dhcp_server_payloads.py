"""
YANG-shape tests for the DHCP server handler.

Asserts the NETCONF payload matches the structure declared in
yang/ios-xe-1731/Cisco-IOS-XE-dhcp.yang for 17.x and
yang/ios-xe-1681/Cisco-IOS-XE-dhcp.yang for 16.x, and that the
RESTCONF parser handles the 17.x primary-network wrapper.

Pure-function tests — no device, no network.
"""

import re
from types import SimpleNamespace

from handlers import dhcp_server


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ── 17.x XML payload ──────────────────────────────────────────────────────────

def test_pool_xml_17x_wraps_network_in_primary_network():
    pool = {
        "name": "RA09-L-Data",
        "network": "172.17.9.16",
        "mask": "255.255.255.240",
        "default_router": "172.17.9.17",
        "dns_servers": ["10.199.64.66"],
        "lease_days": 1,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=False))

    assert 'xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp"' in out
    assert "<primary-network>" in out
    assert "<number>172.17.9.16</number>" in out
    assert "<mask>255.255.255.240</mask>" in out
    # the <number>/<mask> must be inside <primary-network>, not directly under <network>
    primary = re.search(r"<primary-network>(.*?)</primary-network>", out)
    assert primary and "<number>172.17.9.16</number>" in primary.group(1)


def test_pool_xml_17x_default_router_is_container_with_list():
    pool = {
        "name": "p", "network": "10.0.0.0", "mask": "255.255.255.0",
        "default_router": "10.0.0.1", "dns_servers": [], "lease_days": 1,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=False))
    assert "<default-router> <default-router-list>10.0.0.1</default-router-list>" in out


def test_pool_xml_17x_dns_server_is_container_with_list():
    pool = {
        "name": "p", "network": "10.0.0.0", "mask": "255.255.255.0",
        "default_router": None,
        "dns_servers": ["8.8.8.8", "1.1.1.1"], "lease_days": 1,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=False))
    assert "<dns-server-list>8.8.8.8</dns-server-list>" in out
    assert "<dns-server-list>1.1.1.1</dns-server-list>" in out
    # The two leaf-list entries share a single dns-server container
    assert out.count("<dns-server>") == 1


def test_pool_xml_17x_lease_uses_lease_value_days():
    pool = {
        "name": "p", "network": "10.0.0.0", "mask": "255.255.255.0",
        "default_router": None, "dns_servers": [], "lease_days": 7,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=False))
    assert "<lease><lease-value><days>7</days></lease-value></lease>" in out


def test_excluded_xml_17x_wraps_in_low_high_address_list():
    excluded = [
        {"start": "172.17.9.1", "end": "172.17.9.5"},
        {"start": "172.17.9.20", "end": "172.17.9.25"},
    ]
    out = _strip(dhcp_server._build_excluded_xml(excluded, pre_17=False))

    assert 'xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-dhcp"' in out
    # one container holds both ranges as low-high-address-list children
    assert out.count("<excluded-address") == 1
    assert out.count("<low-high-address-list>") == 2
    assert "<low-address>172.17.9.1</low-address>" in out
    assert "<high-address>172.17.9.5</high-address>" in out


def test_excluded_xml_17x_empty_returns_empty_string():
    assert dhcp_server._build_excluded_xml([], pre_17=False) == ""


# ── 16.x XML payload ──────────────────────────────────────────────────────────

def test_pool_xml_16x_uses_flat_network():
    pool = {
        "name": "RA09-L-Data",
        "network": "172.17.9.16",
        "mask": "255.255.255.240",
        "default_router": "172.17.9.17",
        "dns_servers": ["10.199.64.66"],
        "lease_days": 1,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=True))
    assert "<primary-network>" not in out
    # 16.x: number and mask sit directly under <network>
    assert re.search(r"<network>\s*<number>172.17.9.16</number>", out)


def test_pool_xml_16x_lease_uses_capital_days_list():
    pool = {
        "name": "p", "network": "10.0.0.0", "mask": "255.255.255.0",
        "default_router": None, "dns_servers": [], "lease_days": 3,
    }
    out = _strip(dhcp_server._build_pool_xml(pool, pre_17=True))
    assert "<lease><Days>3</Days></lease>" in out


def test_excluded_xml_16x_is_flat_list():
    excluded = [{"start": "10.0.0.1", "end": "10.0.0.5"}]
    out = _strip(dhcp_server._build_excluded_xml(excluded, pre_17=True))
    # 16.x has no low-high-address-list wrapper
    assert "<low-high-address-list>" not in out
    assert "<excluded-address" in out
    assert "<low-address>10.0.0.1</low-address>" in out
    assert "<high-address>10.0.0.5</high-address>" in out


# ── RESTCONF parser ───────────────────────────────────────────────────────────

def _fake_response(payload: dict):
    return SimpleNamespace(json=lambda: payload)


def test_extract_pool_17x_reads_primary_network():
    payload = {
        "Cisco-IOS-XE-native:pool": {
            "id": "RA09-L-Data",
            "network": {
                "primary-network": {
                    "number": "172.17.9.16",
                    "mask":   "255.255.255.240",
                }
            },
            "default-router": {"default-router-list": "172.17.9.17"},
            "dns-server":     {"dns-server-list":     "10.199.64.66"},
        }
    }
    extracted = dhcp_server._extract_pool(_fake_response(payload), pre_17=False)
    assert extracted == {
        "network":        "172.17.9.16",
        "mask":           "255.255.255.240",
        "default_router": "172.17.9.17",
        "dns_servers":    ["10.199.64.66"],
    }


def test_extract_pool_16x_reads_flat_network():
    payload = {
        "Cisco-IOS-XE-native:pool": {
            "id": "p",
            "network": {"number": "10.0.0.0", "mask": "255.255.255.0"},
            "default-router": "10.0.0.1",
            "dns-server":     ["8.8.8.8", "1.1.1.1"],
        }
    }
    extracted = dhcp_server._extract_pool(_fake_response(payload), pre_17=True)
    assert extracted["network"] == "10.0.0.0"
    assert extracted["default_router"] == "10.0.0.1"
    assert sorted(extracted["dns_servers"]) == ["1.1.1.1", "8.8.8.8"]


# ── Input validation ──────────────────────────────────────────────────────────

def test_validate_rejects_invalid_network():
    err = dhcp_server._validate_change({
        "pools": [{"name": "p", "network": "not-an-ip", "mask": "255.255.255.0"}]
    })
    assert err and "network" in err


def test_validate_rejects_invalid_mask():
    err = dhcp_server._validate_change({
        "pools": [{"name": "p", "network": "10.0.0.0", "mask": "garbage"}]
    })
    assert err and "mask" in err


def test_validate_rejects_bad_excluded_range():
    err = dhcp_server._validate_change({
        "pools":    [{"name": "p", "network": "10.0.0.0", "mask": "24"}],
        "excluded": [{"start": "10.0.0.1", "end": "nope"}],
    })
    assert err and "excluded" in err


def test_validate_accepts_well_formed_input():
    err = dhcp_server._validate_change({
        "pools": [{
            "name": "p", "network": "10.0.0.0", "mask": "255.255.255.0",
            "default_router": "10.0.0.1", "dns_servers": ["8.8.8.8"],
            "lease_days": 1,
        }],
        "excluded": [{"start": "10.0.0.2", "end": "10.0.0.5"}],
    })
    assert err is None
