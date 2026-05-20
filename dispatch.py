"""
dispatch.py
───────────
Single registration site for the change_type → handler mapping.

Lives at the repo root, not inside reconciler/, because the
HANDLERS dict is shared between two entry points and ownership
is neutral:

  - reconciler/reconciler.py::apply_changes_to_device
        production GitOps loop, intent-driven
  - labs/network-automation/automate.py
        single-device CLI debug, changes.yaml-driven

Adding a new handler is one edit: import the module here, add an
entry to the dict below. Both entry points pick it up.

KNOWN WART (tracked for follow-up):
    Handler modules currently live under labs/network-automation/
    handlers/, which is documented as a "dev and historical" area.
    This file reaches in via sys.path. The cleaner fix is to
    relocate handlers/ to a neutral top-level location (e.g.
    engine/handlers/) in a follow-up PR. Naming the wart here so
    the next reader doesn't wonder.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict

# Make labs/network-automation/handlers/ importable regardless of
# which entry point loads this module. Path is computed relative
# to this file so CWD doesn't matter.
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


# ─── Dependency resolution ───────────────────────────────────────
# Shared between reconciler/reconciler.py::apply_changes_to_device
# and labs/network-automation/automate.py. Both entry points use
# task-id based dependencies — profiles declare `depends_on: <id>`
# referring to other tasks in the same per-device run.
#
# Statuses that count as "success" for dependency purposes:
#   - success         — task ran and verified
#   - already_correct — task was idempotent, no write needed
#
# Any other status (failure, skipped, exception) blocks dependents.

SUCCESS_STATUSES = frozenset({"success", "already_correct"})
SKIPPED_STATUS = "skipped_due_to_dependency"


def check_dependencies(
    change: dict,
    task_status: dict[str, str],
) -> list[str]:
    """Return the list of unmet prerequisite IDs for this change.

    A prerequisite is unmet if its recorded status is not in
    SUCCESS_STATUSES, or if it's referenced but never ran (not in
    task_status at all — operator typo, or referenced a task that
    comes later in document order).

    Returns [] if all prerequisites are met or the change has no
    depends_on.
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
    "SUCCESS_STATUSES",
    "SKIPPED_STATUS",
    "check_dependencies",
    "record_outcome",
]
