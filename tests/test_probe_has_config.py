"""Safety tests for the blank-mode configuration probe."""

from unittest.mock import patch

import pytest
import requests

from reconciler import reconciler


DEVICE = {"name": "router", "mgmt_ip": "192.0.2.10"}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def _responses(interface, router=None, ip=None, vlan=None):
    return [
        FakeResponse(interface),
        FakeResponse(router or {"Cisco-IOS-XE-native:router": {}}),
        FakeResponse(ip or {"Cisco-IOS-XE-native:ip": {}}),
        FakeResponse(vlan or {"Cisco-IOS-XE-native:vlan": {}}),
    ]


def _payloads(interface=None, router=None, ip=None, vlan=None):
    return {
        "interface": interface or {"Cisco-IOS-XE-native:interface": {}},
        "router": router or {"Cisco-IOS-XE-native:router": {}},
        "ip": ip or {"Cisco-IOS-XE-native:ip": {}},
        "vlan": vlan or {"Cisco-IOS-XE-native:vlan": {}},
    }


def test_factory_default_config_does_not_trigger_wipe():
    interface = {
        "Cisco-IOS-XE-native:interface": {
            "GigabitEthernet": [
                {
                    "name": "0/0/0",
                    "negotiation": {"auto": None},
                    "ip": {"redirects": False, "unreachables": False},
                    "shutdown": [None],
                }
            ]
        }
    }
    vlan = {
        "Cisco-IOS-XE-native:vlan": {
            "vlan-list": [
                {"id": 1, "name": "default"},
                {"id": 1002, "name": "fddi-default"},
            ]
        }
    }

    with patch("requests.get", side_effect=_responses(interface, vlan=vlan)):
        assert reconciler.probe_has_config(DEVICE) is False


def test_managed_config_on_non_management_interface_triggers_wipe_probe():
    interface = {
        "Cisco-IOS-XE-native:interface": {
            "Loopback": [
                {"name": "0", "description": "Managed by network automation"}
            ]
        }
    }

    with patch("requests.get", side_effect=_responses(interface)):
        assert reconciler.probe_has_config(DEVICE) is True


def test_only_management_interface_config_does_not_trigger_wipe():
    interface = {
        "Cisco-IOS-XE-native:interface": {
            "GigabitEthernet": [
                {
                    "name": "1",
                    "description": "Management",
                    "ip": {
                        "address": {
                            "primary": {
                                "address": "192.0.2.10",
                                "mask": "255.255.255.0",
                            }
                        }
                    },
                }
            ]
        }
    }

    with patch("requests.get", side_effect=_responses(interface)):
        assert reconciler.probe_has_config(DEVICE) is False


def test_read_error_fails_safe(caplog):
    with patch("requests.get", side_effect=requests.ConnectionError("offline")):
        assert reconciler.probe_has_config(DEVICE) is False

    assert "refusing wipe" in caplog.text


def test_garbage_json_fails_safe(caplog):
    responses = _responses({"Cisco-IOS-XE-native:interface": {}})
    responses[0] = FakeResponse(json_error=ValueError("not JSON"))

    with patch("requests.get", side_effect=responses):
        assert reconciler.probe_has_config(DEVICE) is False

    assert "refusing wipe" in caplog.text


def test_later_parse_ambiguity_overrides_an_earlier_positive_match(caplog):
    interface = {
        "Cisco-IOS-XE-native:interface": {
            "Loopback": [{"name": "0", "description": "managed"}]
        }
    }
    malformed_router = {"Cisco-IOS-XE-native:router": []}

    with patch(
        "requests.get",
        side_effect=_responses(interface, router=malformed_router),
    ):
        assert reconciler.probe_has_config(DEVICE) is False

    assert "refusing wipe" in caplog.text


@pytest.mark.parametrize(
    "interface_entry",
    [
        {
            "name": "0/0/1",
            "ip": {
                "address": {
                    "primary": {
                        "address": "198.51.100.1",
                        "mask": "255.255.255.0",
                    }
                }
            },
        },
        {
            "name": "0/0/1",
            "ip": {"helper-address": [{"address": "198.51.100.10"}]},
        },
        {"name": "0/0/1", "standby": {"standby-list": [{"group-number": 1}]}},
        {
            "name": "0/0/1",
            "Cisco-IOS-XE-ethernet:channel-group": {"number": 1, "mode": "active"},
        },
        {"name": "0/0/1", "switchport": {"mode": {"trunk": {}}}},
    ],
)
def test_interface_handler_domains_are_detected(interface_entry):
    interface = {
        "Cisco-IOS-XE-native:interface": {
            "GigabitEthernet": [interface_entry]
        }
    }

    assert reconciler._payloads_have_managed_config(
        _payloads(interface=interface)
    ) is True


@pytest.mark.parametrize(
    "payload_overrides",
    [
        {
            "router": {
                "Cisco-IOS-XE-native:router": {
                    "Cisco-IOS-XE-ospf:router-ospf": {
                        "ospf": {"process-id": [{"id": 1}]}
                    }
                }
            }
        },
        {
            "ip": {
                "Cisco-IOS-XE-native:ip": {
                    "route": {
                        "ip-route-interface-forwarding-list": [
                            {"prefix": "198.51.100.0", "mask": "255.255.255.0"}
                        ]
                    }
                }
            }
        },
        {
            "ip": {
                "Cisco-IOS-XE-native:ip": {
                    "dhcp": {"Cisco-IOS-XE-dhcp:pool": [{"id": "lab"}]}
                }
            }
        },
        {
            "vlan": {
                "Cisco-IOS-XE-native:vlan": {
                    "vlan-list": [{"id": 92, "name": "Data"}]
                }
            }
        },
    ],
)
def test_non_interface_handler_domains_are_detected(payload_overrides):
    assert reconciler._payloads_have_managed_config(
        _payloads(**payload_overrides)
    ) is True
