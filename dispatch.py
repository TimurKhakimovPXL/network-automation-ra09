"""Shared handler registry and dependency helpers.

Both the reconciler and the single-device CLI import this module. Handler
modules still live under ``labs/network-automation`` for compatibility, so the
path is added below before they are imported.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict

# Resolve the handler path relative to this file rather than the current
# working directory.
_HANDLERS_PATH = Path(__file__).resolve().parent / "labs" / "network-automation"
if str(_HANDLERS_PATH) not in sys.path:
    sys.path.insert(0, str(_HANDLERS_PATH))

from handlers import (  # noqa: E402  (sys.path mutation above is intentional)
    dhcp_relay,
    dhcp_server,
    etherchannel,
    hsrp,
    interface_description,
    interface_ip,
    interface_state,
    interface_switchport,
    ospf,
    static_routes,
    vlan,
)

# Handler protocol: handle(device_params, device_name, change) -> dict
HandlerFn = Callable[[dict, str, dict], dict]

HANDLERS: Dict[str, HandlerFn] = {
    "interface_description": interface_description.handle,
    "interface_ip":          interface_ip.handle,
    "interface_switchport":  interface_switchport.handle,
    "interface_state":       interface_state.handle,
    "ospf":                  ospf.handle,
    "static_route":          static_routes.handle,
    "vlan":                  vlan.handle,
    "etherchannel":          etherchannel.handle,
    "dhcp_server":           dhcp_server.handle,
    "dhcp_relay":            dhcp_relay.handle,
    "hsrp":                  hsrp.handle,
}


# Dependency resolution
# Dependencies refer to task IDs from the same device run. Only ``success`` and
# ``already_correct`` allow a dependent task to continue.

SUCCESS_STATUSES = frozenset({"success", "already_correct"})
SKIPPED_STATUS = "skipped_due_to_dependency"
NCCLIENT_DEVICE_TYPES = frozenset({"csr", "iosxe"})


def validate_ncclient_device_type(device: dict) -> str | None:
    """Return an inventory error for a missing or unsupported device type."""
    value = device.get("ncclient_device_type")
    if value in NCCLIENT_DEVICE_TYPES:
        return None

    device_name = device.get("name", device.get("host", "unknown"))
    if value is None:
        return (
            f"Device '{device_name}' is missing required "
            "ncclient_device_type (allowed: csr, iosxe)"
        )
    return (
        f"Device '{device_name}' has invalid ncclient_device_type {value!r} "
        "(allowed: csr, iosxe)"
    )


def check_dependencies(
    change: dict,
    task_status: dict[str, str],
) -> list[str]:
    """Return the list of unmet prerequisite IDs for this change.

    A prerequisite is unmet when it did not succeed, has not run yet, or does
    not exist in this device's task list.

    An empty list means that the change can run.
    """
    depends_on = change.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]

    return [
        dep_id for dep_id in depends_on
        if task_status.get(dep_id) not in SUCCESS_STATUSES
    ]


def record_outcome(
    change: dict,
    result: dict,
    task_status: dict[str, str],
) -> None:
    """Record outcome of `change` in task_status if it declared an id.

    Tasks without an id are still executed and reported but can't
    be referenced by depends_on from later tasks.
    """
    task_id = change.get("id")
    if task_id:
        task_status[task_id] = result.get("status", "unknown")


__all__ = [
    "HANDLERS",
    "HandlerFn",
    "NCCLIENT_DEVICE_TYPES",
    "SUCCESS_STATUSES",
    "SKIPPED_STATUS",
    "check_dependencies",
    "record_outcome",
    "validate_ncclient_device_type",
]
