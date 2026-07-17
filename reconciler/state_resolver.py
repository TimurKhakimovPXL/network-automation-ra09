"""Resolve intent, inventory, and profiles into per-device target state.

Inputs:
  intent/class_state.yaml : what the lab should look like
  infra/inventory.yaml    : what hardware exists
  intent/profiles/*.yaml  : reusable device-state declarations

Output:
  Dict[device_name, List[change_dict]]: per-device list of changes to apply

The resolver reads the three sources once per reconciliation loop. It does not
write files or contact devices.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError


# Paths (relative to repo root)

REPO_ROOT = Path(__file__).resolve().parent.parent
INTENT_FILE = REPO_ROOT / "intent" / "class_state.yaml"
INVENTORY_FILE = REPO_ROOT / "infra" / "inventory.yaml"
PROFILES_DIR = REPO_ROOT / "intent" / "profiles"


# Custom exceptions


class ResolverError(Exception):
    """Raised when intent + inventory + profile cannot be resolved into a valid
    target state. The reconciler catches this, logs it, and waits for the
    supervisor to fix the YAML."""


# Public API


def resolve() -> Dict[str, Optional[List[Dict[str, Any]]]]:
    """Read the YAML sources and return target state by device name.

    Returns:
        {
          "LAB-RA01-C01-R01": [ {change_dict}, ... ],
          "LAB-RA01-C02-R01": [ {change_dict}, ... ],
          ...
        }

    Per-device values encode the mode:
        list of changes: preconfigured (apply these)
        []             : blank (wipe any managed config)
        None           : observe (probe only, no writes, no wipes)
    """
    intent = _load_yaml(INTENT_FILE, "intent/class_state.yaml")
    inventory = _load_yaml(INVENTORY_FILE, "infra/inventory.yaml")

    devices = inventory.get("devices") or []
    if not devices:
        raise ResolverError("infra/inventory.yaml has no devices declared")

    target_state: Dict[str, Optional[List[Dict[str, Any]]]] = {}

    for device in devices:
        device_name = device.get("name")
        if not device_name:
            raise ResolverError(f"inventory entry missing 'name': {device!r}")

        # Select the most specific override.
        #   1. overrides.devices[<device-name>]   : single device
        #   2. overrides.racks[<RAxx>]            : whole rack
        #   3. overrides[<RAxx>]                  : legacy flat rack key (back-compat)
        #   4. session.pre_class                  : default for everything else
        rack_id = f"RA{device.get('rack', 0):02d}"
        overrides = intent.get("overrides") or {}

        device_override = (overrides.get("devices") or {}).get(device_name)
        rack_override = (overrides.get("racks") or {}).get(rack_id)
        legacy_override = overrides.get(rack_id)

        override = next(
            (o for o in (device_override, rack_override, legacy_override) if o is not None),
            None,
        )

        if override is not None:
            mode = override.get("mode", "blank")
            profile_name = override.get("profile")
        else:
            pre_class = (intent.get("session") or {}).get("pre_class") or {}
            mode = pre_class.get("mode", "blank")
            profile_name = pre_class.get("profile")

        if mode == "blank":
            # Empty list: reconciler will wipe any managed config.
            target_state[device_name] = []
            continue

        if mode == "observe":
            # None: reconciler probes reachability but never writes or wipes.
            # Distinct from blank ([]) which actively converges to empty.
            target_state[device_name] = None
            continue

        if mode != "preconfigured":
            raise ResolverError(
                f"unknown mode '{mode}' for device {device_name}; "
                f"expected 'blank', 'preconfigured', or 'observe'"
            )

        if not profile_name:
            raise ResolverError(
                f"mode 'preconfigured' selected for {device_name} but no profile named"
            )

        target_state[device_name] = _render_profile(profile_name, device)

    return target_state


def get_wipe_directive() -> bool:
    """Return the value of ``maintenance.wipe_now`` from class_state.yaml."""
    intent = _load_yaml(INTENT_FILE, "intent/class_state.yaml")
    return bool((intent.get("maintenance") or {}).get("wipe_now", False))


def get_inventory() -> List[Dict[str, Any]]:
    """Return the device entries from inventory.yaml."""
    inventory = _load_yaml(INVENTORY_FILE, "infra/inventory.yaml")
    return inventory.get("devices") or []


# Internals


def _load_yaml(path: Path, label: str) -> Dict[str, Any]:
    if not path.exists():
        raise ResolverError(f"{label} not found at {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ResolverError(f"{label} is invalid YAML: {e}") from e
    if data is None:
        raise ResolverError(f"{label} is empty")
    if not isinstance(data, dict):
        raise ResolverError(f"{label} must be a mapping at top level")
    return data


def _render_profile(profile_name: str, device: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Loads a profile, renders it as a Jinja2 template against the device's
    inventory fields, then parses the result as YAML. Returns the resolved
    per_device_changes list."""
    profile_path = PROFILES_DIR / f"{profile_name}.yaml"
    if not profile_path.exists():
        raise ResolverError(
            f"profile '{profile_name}' referenced but {profile_path} does not exist"
        )

    with profile_path.open("r", encoding="utf-8") as f:
        template_source = f.read()

    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)

    try:
        template = env.from_string(template_source)
        rendered = template.render(**device)
    except TemplateError as e:
        raise ResolverError(
            f"profile '{profile_name}' failed to render for device "
            f"{device.get('name', '?')}: {e}"
        ) from e

    try:
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError as e:
        raise ResolverError(
            f"profile '{profile_name}' rendered output is invalid YAML "
            f"for device {device.get('name', '?')}: {e}"
        ) from e

    changes = (parsed or {}).get("per_device_changes")
    if changes is None:
        return []
    if not isinstance(changes, list):
        raise ResolverError(
            f"profile '{profile_name}' has 'per_device_changes' that is not a list"
        )

    return changes


# Standalone debug entry point


if __name__ == "__main__":
    # Print resolved state without starting the reconciler.
    import json

    try:
        state = resolve()
        print(json.dumps(state, indent=2, default=str))
    except ResolverError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
