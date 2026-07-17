"""Inventory validation tests."""

from unittest.mock import patch

from dispatch import validate_ncclient_device_type
from reconciler import reconciler


def test_ncclient_device_type_accepts_supported_values():
    assert validate_ncclient_device_type(
        {"name": "router", "ncclient_device_type": "csr"}
    ) is None
    assert validate_ncclient_device_type(
        {"name": "switch", "ncclient_device_type": "iosxe"}
    ) is None


def test_ncclient_device_type_rejects_missing_value():
    error = validate_ncclient_device_type({"name": "router"})

    assert error is not None
    assert "missing required ncclient_device_type" in error


def test_ncclient_device_type_rejects_unknown_value():
    error = validate_ncclient_device_type(
        {"name": "router", "ncclient_device_type": "ios"}
    )

    assert error is not None
    assert "invalid ncclient_device_type 'ios'" in error


def test_invalid_inventory_is_reported_without_stopping_other_devices():
    inventory = [
        {"name": "bad", "mgmt_ip": "192.0.2.1"},
        {
            "name": "good",
            "mgmt_ip": "192.0.2.2",
            "ncclient_device_type": "iosxe",
        },
    ]
    target = {"bad": None, "good": None}

    with (
        patch.object(reconciler.git_watcher, "pull", return_value=True),
        patch.object(
            reconciler.git_watcher,
            "current_commit_sha",
            return_value="abc123",
        ),
        patch.object(reconciler.state_resolver, "resolve", return_value=target),
        patch.object(
            reconciler.state_resolver,
            "get_inventory",
            return_value=inventory,
        ),
        patch.object(
            reconciler.state_resolver,
            "get_wipe_directive",
            return_value=False,
        ),
        patch.object(reconciler, "is_reachable", return_value=True),
    ):
        report = reconciler.reconcile_once()

    assert report["devices"]["bad"]["status"] == "invalid_inventory"
    assert report["devices"]["good"]["status"] == "observed_reachable"
