"""
automate.py — Universal network automation engine
PXL DEVNET — Cisco IOS XE (ISR4200 / Catalyst)

Usage:
    python3 automate.py

Reads desired state from changes.yaml.
Routes each change to the correct domain handler.
Writes a structured report to report.json on completion.

Adding a new domain:
    1. Create handlers/<domain>.py implementing handle(device_params, device_name, change) -> dict
    2. Register it in HANDLERS below — that's it.

Credentials are loaded from .env — never put them in changes.yaml.
"""

import json
import os
import sys
from datetime import datetime

import yaml
from pathlib import Path
from dotenv import load_dotenv

# ── Handler registry ───────────────────────────────────────────────────────────
# To add a new domain: import the module and add it here.
from handlers import (
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

HANDLERS = {
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

CHANGES_FILE = "changes.yaml"
REPORT_FILE  = "report.json"


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{timestamp}] {msg}")


# ── YAML loading ───────────────────────────────────────────────────────────────

def load_changes(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ── Device params ──────────────────────────────────────────────────────────────

def build_device_params(device: dict, username: str, password: str) -> dict:
    """
    Build the ncclient connection parameter dict for a device.
    Host and port come from changes.yaml.
    Credentials come from .env — never from the YAML file.
    """
    return {
        "host":                    device["host"],
        "port":                    device.get("port", 830),
        "username":                username,
        "password":                password,
        "hostkey_verify":          False,
        "device_params":           {"name": "csr"},
        "allow_agent":             False,
        "look_for_keys":           False,
    }


# ── Dispatcher ─────────────────────────────────────────────────────────────────

def dispatch(device_params: dict, device_name: str, change: dict) -> dict:
    """
    Route a single change to the correct handler based on change["type"].
    Returns a result dict suitable for inclusion in report.json.

    If the change type is unrecognised, returns an error result immediately
    without touching the device — the run continues with remaining changes.
    """
    change_type = change.get("type")

    if not change_type:
        return {
            "device_name": device_name,
            "type":        None,
            "status":      "missing_type",
            "error":       "Change entry has no 'type' field — check changes.yaml",
        }

    handler = HANDLERS.get(change_type)

    if not handler:
        return {
            "device_name": device_name,
            "type":        change_type,
            "status":      "unknown_type",
            "error":       f"No handler registered for type '{change_type}'. "
                           f"Available: {list(HANDLERS.keys())}",
        }

    log(f"  [{change_type}] dispatching...")
    try:
        result = handler(device_params, device_name, change)
        result.setdefault("type", change_type)
        result.setdefault("device_name", device_name)
        return result
    except Exception as e:
        # Handler raised an unexpected exception — record it, continue the run.
        return {
            "device_name": device_name,
            "type":        change_type,
            "status":      "handler_exception",
            "error":       str(e),
        }


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(results: list[dict]) -> None:
    success        = sum(1 for r in results if r.get("status") == "success")
    already        = sum(1 for r in results if r.get("status") == "already_correct")
    failed         = sum(1 for r in results if r.get("status") not in ("success", "already_correct"))

    report = {
        "generated_at":   datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_tasks":    len(results),
        "success":        success,
        "already_correct": already,
        "failed":         failed,
        "results":        results,
    }

    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    log(f"Report written to {REPORT_FILE} "
        f"({success} success, {already} already_correct, {failed} failed)")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

    username = os.getenv("LAB_USER")
    password = os.getenv("LAB_PASS")

    if not username or not password:
        print("[ERROR] LAB_USER and LAB_PASS must be set in .env")
        sys.exit(1)

    if not os.path.exists(CHANGES_FILE):
        print(f"[ERROR] {CHANGES_FILE} not found in working directory")
        sys.exit(1)

    data    = load_changes(CHANGES_FILE)
    devices = data.get("devices", [])

    if not devices:
        print("[WARN] No devices defined in changes.yaml — nothing to do.")
        sys.exit(0)

    all_results: list[dict] = []

    for device in devices:
        device_name = device.get("name", device.get("host", "unknown"))
        changes     = device.get("changes", [])

        log(f"=== {device_name} ({device['host']}) — {len(changes)} change(s) ===")

        if not changes:
            log("  No changes defined for this device — skipping.")
            continue

        device_params = build_device_params(device, username, password)

        for change in changes:
            result = dispatch(device_params, device_name, change)
            all_results.append(result)

            status = result.get("status", "unknown")
            if status == "success":
                log(f"  [OK]   {result.get('type')} — {status}")
            elif status == "already_correct":
                log(f"  [SKIP] {result.get('type')} — already correct")
            else:
                log(f"  [FAIL] {result.get('type')} — {status}: {result.get('error', '')}")

    write_report(all_results)


if __name__ == "__main__":
    main()
