"""Payload, RESTCONF-path, and parser tests for both IOS XE OSPF schemas."""

from types import SimpleNamespace

from handlers import ospf


CHANGE = {
    "process_id": 1,
    "router_id": "1.1.1.1",
    "networks": [{"prefix": "10.0.0.0", "wildcard": "0.0.0.255", "area": 0}],
}


def _response(payload):
    return SimpleNamespace(json=lambda: payload)


def test_2018_revision_selects_legacy_flat_schema():
    assert ospf._schema_for_revision("2018-10-08") == ospf.LEGACY_SCHEMA


def test_2020_revision_selects_wrapped_schema():
    assert ospf._schema_for_revision("2020-07-01") == ospf.WRAPPED_SCHEMA


def test_invalid_revision_fails_safe_to_wrapped_schema():
    assert ospf._schema_for_revision("unknown") == ospf.WRAPPED_SCHEMA


def test_legacy_restconf_path_targets_flat_keyed_list():
    url = ospf._restconf_url("192.0.2.1", 7, ospf.LEGACY_SCHEMA)
    assert url.endswith("Cisco-IOS-XE-ospf:ospf=7")
    assert "router-ospf" not in url


def test_wrapped_restconf_path_targets_process_id():
    url = ospf._restconf_url("192.0.2.1", 7, ospf.WRAPPED_SCHEMA)
    assert url.endswith("router-ospf/ospf/process-id=7")


def test_legacy_payload_uses_flat_ospf_and_mask_key():
    payload = ospf._build_config(CHANGE, ospf.LEGACY_SCHEMA)
    assert "<router-ospf" not in payload
    assert '<ospf xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf">' in payload
    assert "<id>1</id>" in payload
    assert "<mask>0.0.0.255</mask>" in payload
    assert "<wildcard>" not in payload


def test_wrapped_payload_uses_process_id_and_wildcard_key():
    payload = ospf._build_config(CHANGE, ospf.WRAPPED_SCHEMA)
    assert "<router-ospf" in payload
    assert "<process-id>" in payload
    assert "<wildcard>0.0.0.255</wildcard>" in payload
    assert "<mask>" not in payload


def test_legacy_parser_reads_flat_ospf_response():
    current = ospf._extract_ospf_state(_response({
        "Cisco-IOS-XE-ospf:ospf": {
            "id": 1,
            "router-id": "1.1.1.1",
            "network": [{"ip": "10.0.0.0", "mask": "0.0.0.255", "area": 0}],
        }
    }), ospf.LEGACY_SCHEMA)
    assert current == {
        "router_id": "1.1.1.1",
        "networks": [{"prefix": "10.0.0.0", "wildcard": "0.0.0.255", "area": "0"}],
    }


def test_wrapped_parser_reads_process_id_response():
    current = ospf._extract_ospf_state(_response({
        "Cisco-IOS-XE-ospf:process-id": [{
            "id": 1,
            "router-id": "1.1.1.1",
            "network": [{"ip": "10.0.0.0", "wildcard": "0.0.0.255", "area": 0}],
        }]
    }), ospf.WRAPPED_SCHEMA)
    assert current == {
        "router_id": "1.1.1.1",
        "networks": [{"prefix": "10.0.0.0", "wildcard": "0.0.0.255", "area": "0"}],
    }
