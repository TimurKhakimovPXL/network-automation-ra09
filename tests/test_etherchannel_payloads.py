"""
YANG-shape tests for the EtherChannel handler.

Asserts that the NETCONF payload matches Cisco-IOS-XE-ethernet's
config-interface-ethernet-grouping (channel-group sibling channel-protocol,
both in the Cisco-IOS-XE-ethernet namespace), and that mode/protocol
combinations are validated before any device write.

Pure-function tests — no device, no network.
"""

import re
from types import SimpleNamespace

from handlers import etherchannel as ec


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ── Member XML ────────────────────────────────────────────────────────────────

def test_member_xml_lacp_emits_channel_protocol():
    member = {"interface_type": "GigabitEthernet", "interface_name": "0/1"}
    out = _strip(ec._build_member_xml(member, channel_id=1, mode="active", protocol="lacp"))

    assert "<GigabitEthernet>" in out
    assert "<name>0/1</name>" in out
    assert '<channel-group xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet">' in out
    assert "<number>1</number>" in out
    assert "<mode>active</mode>" in out
    assert '<channel-protocol xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet">lacp</channel-protocol>' in out


def test_member_xml_pagp_emits_channel_protocol():
    member = {"interface_type": "GigabitEthernet", "interface_name": "0/2"}
    out = _strip(ec._build_member_xml(member, channel_id=2, mode="desirable", protocol="pagp"))
    assert "<mode>desirable</mode>" in out
    assert ">pagp</channel-protocol>" in out


def test_member_xml_none_protocol_omits_channel_protocol_and_forces_on():
    member = {"interface_type": "GigabitEthernet", "interface_name": "0/3"}
    out = _strip(ec._build_member_xml(member, channel_id=3, mode="active", protocol="none"))
    assert "<channel-protocol" not in out
    # protocol=none degenerates to static channel — mode must be "on" on the wire
    assert "<mode>on</mode>" in out


def test_member_xml_uses_ethernet_namespace_on_both_leaves():
    member = {"interface_type": "GigabitEthernet", "interface_name": "0/1"}
    out = _strip(ec._build_member_xml(member, channel_id=1, mode="passive", protocol="lacp"))
    # both <channel-group> and <channel-protocol> carry the augmenting ns
    assert out.count('xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ethernet"') == 2


# ── Validation ────────────────────────────────────────────────────────────────

def _base():
    return {
        "channel_id": 1,
        "mode": "active",
        "protocol": "lacp",
        "members": [{"interface_type": "GigabitEthernet", "interface_name": "0/1"}],
    }


def test_validate_accepts_canonical_lacp():
    assert ec._validate_change(_base()) is None


def test_validate_rejects_non_integer_channel_id():
    bad = _base() | {"channel_id": "one"}
    assert "channel_id" in (ec._validate_change(bad) or "")


def test_validate_rejects_unknown_mode():
    bad = _base() | {"mode": "fast"}
    assert "mode" in (ec._validate_change(bad) or "")


def test_validate_rejects_unknown_protocol():
    bad = _base() | {"protocol": "mlag"}
    assert "protocol" in (ec._validate_change(bad) or "")


def test_validate_rejects_lacp_with_pagp_mode():
    bad = _base() | {"protocol": "lacp", "mode": "desirable"}
    err = ec._validate_change(bad) or ""
    assert "lacp" in err and "mode" in err


def test_validate_rejects_pagp_with_lacp_mode():
    bad = _base() | {"protocol": "pagp", "mode": "active"}
    err = ec._validate_change(bad) or ""
    assert "pagp" in err and "mode" in err


def test_validate_rejects_none_with_non_on_mode():
    bad = _base() | {"protocol": "none", "mode": "active"}
    err = ec._validate_change(bad) or ""
    assert "mode=on" in err


def test_validate_rejects_unsupported_interface_type():
    bad = _base()
    bad["members"] = [{"interface_type": "BananaEthernet", "interface_name": "0/1"}]
    err = ec._validate_change(bad) or ""
    assert "invalid member interface" in err


def test_validate_rejects_empty_members():
    bad = _base() | {"members": []}
    assert "no member interfaces" in (ec._validate_change(bad) or "")


# ── Member RESTCONF parser ────────────────────────────────────────────────────

def _fake_response(payload):
    return SimpleNamespace(json=lambda: payload)


def test_extract_member_channel_reads_augment_qualified_keys():
    payload = {
        "Cisco-IOS-XE-native:GigabitEthernet": {
            "name": "0/1",
            "Cisco-IOS-XE-ethernet:channel-group": {"number": 1, "mode": "active"},
            "Cisco-IOS-XE-ethernet:channel-protocol": "lacp",
        }
    }
    out = ec._extract_member_channel(_fake_response(payload), "GigabitEthernet")
    assert out == {"number": 1, "mode": "active", "protocol": "lacp"}


def test_extract_member_channel_handles_unqualified_keys():
    # Some devices return the augment children without the module prefix.
    payload = {
        "Cisco-IOS-XE-native:GigabitEthernet": {
            "name": "0/1",
            "channel-group": {"number": "2", "mode": "passive"},
        }
    }
    out = ec._extract_member_channel(_fake_response(payload), "GigabitEthernet")
    assert out["number"] == 2
    assert out["mode"] == "passive"
    assert out["protocol"] is None


def test_extract_member_channel_returns_nones_when_absent():
    payload = {"Cisco-IOS-XE-native:GigabitEthernet": {"name": "0/1"}}
    out = ec._extract_member_channel(_fake_response(payload), "GigabitEthernet")
    assert out == {"number": None, "mode": None, "protocol": None}
