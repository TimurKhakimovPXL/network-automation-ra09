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

__all__ = ["HANDLERS", "HandlerFn"]
