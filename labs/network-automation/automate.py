"""
automate.py — Network automation engine (CLI debug surface)
PXL DEVNET — Cisco IOS XE (ISR4200 / Catalyst)

Usage:
    python3 automate.py

Reads desired state from changes.yaml.
Routes each change to the correct domain handler.
Writes a structured report to report.json on completion.

Adding a new domain:
    1. Create handlers/<domain>.py implementing
       handle(device_params, device_name, change) -> dict
    2. Register it in dispatch.py::HANDLERS at the repo root — both
       this CLI debug entry point and the reconciler will pick it up
       automatically.

This file is a single-device CLI debug surface: it reads changes.yaml
and pushes against one device without involving intent/inventory/profile.
For GitOps reconciliation, the systemd unit network-reconciler.service
runs reconciler/reconciler.py on a 60s loop. For one-shot reconciliation
against the real intent stack, use scripts/manual_reconcile.py --dry-run.

Credentials are loaded from .env — never put them in changes.yaml.
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Handler registry ───────────────────────────────────────────────────────────
# HANDLERS is defined once in dispatch.py at the repo root and imported here
# so both entry points (this CLI tool and reconciler/reconciler.py) share a
# single registration site.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dispatch import (  # noqa: E402
    HANDLERS,
    SUCCESS_STATUSES,
    SKIPPED_STATUS,
    check_dependencies,
    record_outcome,
)

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

    The 'ncclient_device_type' field selects which ncclient profile is used
    for the NETCONF SSH subsystem negotiation. Values:
        csr    — CSR1000v (default for backward compatibility)
        iosxe  — ISR4200, Catalyst 9000 series, any other IOS XE platform
    The field is set per-device in inventory.yaml (or per-entry in changes.yaml)
    and falls back to 'csr' if absent so existing inventory entries keep working.
    """
    return {
        "host":                    device["host"],
        "port":                    device.get("port", 830),
        "username":                username,
        "password":                password,
        "hostkey_verify":          False,
        "device_params":           {"name": device.get("ncclient_device_type", "csr")},
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
        # Handler raised an unexpected exception — record full traceback,
        # continue the run. Without the traceback, debugging unattended
        # runs against many devices is miserable.
        return {
            "device_name": device_name,
            "type":        change_type,
            "status":      "handler_exception",
            "error":       str(e),
            "traceback":   traceback.format_exc(),
        }


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(results: list[dict], report_file: str = REPORT_FILE) -> None:
    success = sum(1 for r in results if r.get("status") == "success")
    already = sum(1 for r in results if r.get("status") == "already_correct")
    ok_or_skipped = SUCCESS_STATUSES | {SKIPPED_STATUS}
    skipped = sum(1 for r in results if r.get("status") == SKIPPED_STATUS)
    failed  = sum(1 for r in results if r.get("status") not in ok_or_skipped)

    report = {
        "generated_at":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_tasks":     len(results),
        "success":         success,
        "already_correct": already,
        "skipped":         skipped,
        "failed":          failed,
        "results":         results,
    }

    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    log(f"Report written to {report_file} "
        f"({success} success, {already} already_correct, "
        f"{skipped} skipped, {failed} failed)")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the single-device automation engine")
    parser.add_argument(
        "--changes", default=CHANGES_FILE,
        help=f"Desired-state YAML file (default: {CHANGES_FILE})",
    )
    parser.add_argument(
        "--report", default=REPORT_FILE,
        help=f"JSON report destination (default: {REPORT_FILE})",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

    username = os.getenv("LAB_USER")
    password = os.getenv("LAB_PASS")

    if not username or not password:
        print("[ERROR] LAB_USER and LAB_PASS must be set in .env")
        sys.exit(1)

    if not os.path.exists(args.changes):
        print(f"[ERROR] {args.changes} not found")
        sys.exit(1)

    data    = load_changes(args.changes)
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

        device_task_status: dict[str, str] = {}

        for change in changes:
            unmet = check_dependencies(change, device_task_status)
            if unmet:
                result = {
                    "device_name": device_name,
                    "type":        change.get("type"),
                    "id":          change.get("id"),
                    "status":      SKIPPED_STATUS,
                    "error":       f"Prerequisite task(s) did not succeed: {unmet}",
                }
                all_results.append(result)
                record_outcome(change, result, device_task_status)
                log(f"  [SKIP] {change.get('type')} — depends_on unmet: {unmet}")
                continue

            result = dispatch(device_params, device_name, change)
            all_results.append(result)
            record_outcome(change, result, device_task_status)

            status = result.get("status", "unknown")
            if status == "success":
                log(f"  [OK]   {result.get('type')} — {status}")
            elif status == "already_correct":
                log(f"  [SKIP] {result.get('type')} — already correct")
            else:
                log(f"  [FAIL] {result.get('type')} — {status}: {result.get('error', '')}")

    write_report(all_results, args.report)


if __name__ == "__main__":
    main()
