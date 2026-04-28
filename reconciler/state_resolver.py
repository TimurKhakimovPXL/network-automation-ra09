"""
state_resolver.py — Resolve intent + inventory + profile into per-device target state.

Inputs:
  intent/class_state.yaml  — what the lab should look like
  infra/inventory.yaml     — what hardware exists
  intent/profiles/*.yaml   — reusable device-state declarations

Output:
  Dict[device_name, List[change_dict]] — per-device list of changes to apply

The resolver is pure: same inputs → same outputs, no side effects, no I/O beyond
file reads. This makes it trivially testable. The reconciler calls resolve() once
per loop iteration to produce the target state, then diffs it against the
observed state.
"""

from pathlib import Path
from typing import Any, Dict, List

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError


# ─── Paths (relative to repo root) ────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
INTENT_FILE = REPO_ROOT / "intent" / "class_state.yaml"
INVENTORY_FILE = REPO_ROOT / "infra" / "inventory.yaml"
PROFILES_DIR = REPO_ROOT / "intent" / "profiles"


# ─── Custom exceptions ────────────────────────────────────────────────────────


class ResolverError(Exception):
    """Raised when intent + inventory + profile cannot be resolved into a valid
    target state. The reconciler catches this, logs it, and waits for the
    supervisor to fix the YAML."""


# ─── Public API ───────────────────────────────────────────────────────────────


def resolve() -> Dict[str, List[Dict[str, Any]]]:
    """
    Top-level entry point. Reads all three YAML sources and produces a per-device
    target-state dict.

    Returns:
        {
          "LAB-RA01-C01-R01": [ {change_dict}, ... ],
          "LAB-RA01-C02-R01": [ {change_dict}, ... ],
          ...
        }

    Devices that should remain blank yield an empty list (still present in dict).
    """
    intent = _load_yaml(INTENT_FILE, "intent/class_state.yaml")
    inventory = _load_yaml(INVENTORY_FILE, "infra/inventory.yaml")

    devices = inventory.get("devices") or []
    if not devices:
        raise ResolverError("infra/inventory.yaml has no devices declared")

    target_state: Dict[str, List[Dict[str, Any]]] = {}

    for device in devices:
        device_name = device.get("name")
        if not device_name:
            raise ResolverError(f"inventory entry missing 'name': {device!r}")

        # Determine which profile applies — overrides win, otherwise session default
        rack_id = f"RA{device.get('rack', 0):02d}"
        override = (intent.get("overrides") or {}).get(rack_id)

        if override is not None:
            mode = override.get("mode", "blank")
            profile_name = override.get("profile")
        else:
            pre_class = (intent.get("session") or {}).get("pre_class") or {}
            mode = pre_class.get("mode", "blank")
            profile_name = pre_class.get("profile")

        if mode == "blank":
            target_state[device_name] = []
            continue

        if mode != "preconfigured":
            raise ResolverError(
                f"unknown mode '{mode}' for device {device_name}; "
                f"expected 'blank' or 'preconfigured'"
            )

        if not profile_name:
            raise ResolverError(
                f"mode 'preconfigured' selected for {device_name} but no profile named"
            )

        target_state[device_name] = _render_profile(profile_name, device)

    return target_state


def get_wipe_directive() -> bool:
    """Returns True iff intent/class_state.yaml has maintenance.wipe_now == true.
    The reconciler combines this with the commit-SHA tracking to decide whether
    to actually wipe."""
    intent = _load_yaml(INTENT_FILE, "intent/class_state.yaml")
    return bool((intent.get("maintenance") or {}).get("wipe_now", False))


def get_inventory() -> List[Dict[str, Any]]:
    """Returns the list of device dicts from inventory.yaml. Used by the
    reconciler to know which devices to probe."""
    inventory = _load_yaml(INVENTORY_FILE, "infra/inventory.yaml")
    return inventory.get("devices") or []


# ─── Internals ────────────────────────────────────────────────────────────────


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


# ─── Standalone debug entry point ─────────────────────────────────────────────


if __name__ == "__main__":
    """Run this directly to dump the resolved target state. Useful for verifying
    a profile renders correctly without involving the full reconciler."""
    import json

    try:
        state = resolve()
        print(json.dumps(state, indent=2, default=str))
    except ResolverError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
