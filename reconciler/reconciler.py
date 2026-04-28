"""
reconciler.py — The continuous reconciliation loop.

This is the always-on service that watches Git, observes device state, and
converges devices to declared intent. It is the new heart of the system.

Architecture:
  1. Pull latest from Git
  2. Resolve intent + inventory + profile → per-device target state
  3. For each device: probe reachability, compare target vs observed, apply delta
  4. Handle maintenance.wipe_now (idempotent on commit SHA)
  5. Write a report
  6. Sleep 60 seconds
  7. Repeat forever

Failure handling:
  - Invalid YAML       → log error, skip iteration, retry next loop
  - Git pull fails     → continue with last good state
  - Device unreachable → mark pending, skip
  - Handler fails      → record failure, continue with other changes/devices

Invocation:
  python -m reconciler.reconciler

Or under systemd:
  systemctl start network-reconciler

Configuration via environment variables (typically set in systemd unit):
  RECONCILER_INTERVAL_SECONDS  — loop interval, default 60
  RECONCILER_REPORT_DIR        — where to write reports, default /var/lib/network-automation/reports
  RECONCILER_STATE_DIR         — where to track wipe state, default /var/lib/network-automation
  LAB_USER, LAB_PASS           — device credentials (loaded from .env or environment)
"""

import json
import logging
import os
import signal
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from reconciler import git_watcher, state_resolver
from reconciler.state_resolver import ResolverError


# ─── Configuration ────────────────────────────────────────────────────────────

INTERVAL_SECONDS = int(os.environ.get("RECONCILER_INTERVAL_SECONDS", "60"))
REPORT_DIR = Path(os.environ.get("RECONCILER_REPORT_DIR", "/var/lib/network-automation/reports"))
STATE_DIR = Path(os.environ.get("RECONCILER_STATE_DIR", "/var/lib/network-automation"))
WIPE_STATE_FILE = STATE_DIR / "wipe-state.json"


# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("reconciler")


# ─── Graceful shutdown ────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    log.info("received signal %d, shutting down at end of current iteration", signum)
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ─── Reachability probe ──────────────────────────────────────────────────────


def is_reachable(mgmt_ip: str, port: int = 830, timeout: float = 3.0) -> bool:
    """TCP probe to NETCONF port. Faster and more reliable than ICMP for our
    purposes — if NETCONF is up the device is genuinely manageable."""
    try:
        with socket.create_connection((mgmt_ip, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


# ─── Apply changes via existing engine ───────────────────────────────────────


def apply_changes_to_device(device: Dict[str, Any], changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Bridge between the reconciler and the existing handler engine.

    The existing automate.py expects to be invoked as a CLI with changes.yaml.
    To call it programmatically per-device, we import the handlers directly and
    invoke them the same way automate.py does.

    Returns a list of result dicts, one per change attempted.
    """
    # Imported lazily so the reconciler can start even if the engine path is
    # being adjusted during a refactor.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "labs" / "network-automation"))
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

    device_params = {
        "host": device["mgmt_ip"],
        "port": 830,
        "username": os.environ["LAB_USER"],
        "password": os.environ["LAB_PASS"],
        "hostkey_verify": False,
        "device_params": {"name": "csr"},
        "allow_agent": False,
        "look_for_keys": False,
    }

    results = []
    for change in changes:
        change_type = change.get("type")
        if not change_type:
            results.append({"status": "missing_type", "change": change})
            continue

        handler = HANDLERS.get(change_type)
        if handler is None:
            results.append({"status": "unknown_type", "type": change_type})
            continue

        try:
            result = handler(device_params, device["name"], change)
        except Exception as e:
            result = {
                "status": "handler_exception",
                "type": change_type,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
        results.append(result)

    return results


# ─── Wipe handling ────────────────────────────────────────────────────────────


def load_wipe_state() -> Dict[str, Any]:
    """Returns {'last_completed_sha': str | None, 'last_completed_at': iso8601 | None}"""
    if not WIPE_STATE_FILE.exists():
        return {"last_completed_sha": None, "last_completed_at": None}
    try:
        with WIPE_STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("wipe-state.json unreadable, treating as empty")
        return {"last_completed_sha": None, "last_completed_at": None}


def save_wipe_state(commit_sha: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_completed_sha": commit_sha,
        "last_completed_at": datetime.now(timezone.utc).isoformat(),
    }
    with WIPE_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def perform_wipe(devices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wipe all reachable devices. Each wipe is independent; failures don't
    abort the rest. Returns a summary dict for the report."""
    from ncclient import manager

    summary = {"total": len(devices), "wiped": 0, "unreachable": 0, "failed": 0, "details": []}

    wipe_rpc = """
    <cisco-ia:save-config xmlns:cisco-ia="http://cisco.com/yang/cisco-ia">
    </cisco-ia:save-config>
    """  # placeholder; actual wipe RPC documented below

    for device in devices:
        if not is_reachable(device["mgmt_ip"]):
            summary["unreachable"] += 1
            summary["details"].append({"device": device["name"], "status": "unreachable"})
            continue

        # The actual "write erase + reload" sequence on IOS XE is best done via
        # NETCONF default-deny-write to running, followed by a save-config call.
        # The cleanest approach in production is to invoke the IOS-XE-specific
        # default-config RPC if available, or fall back to SSH "write erase".
        # Implementation deferred — see TODO.

        # TODO: implement actual wipe via ncclient or paramiko.
        # For now this is a placeholder that records intent.

        log.warning(
            "WIPE NOT YET IMPLEMENTED — would wipe %s (%s)",
            device["name"], device["mgmt_ip"]
        )
        summary["details"].append({
            "device": device["name"],
            "status": "wipe_not_implemented",
        })

    return summary


# ─── Report writing ──────────────────────────────────────────────────────────


def write_report(report: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"reconcile-{timestamp}.json"
    latest_link = REPORT_DIR / "latest.json"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # Update "latest" symlink for easy `cat` access
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(report_path.name)


# ─── Main loop ────────────────────────────────────────────────────────────────


def reconcile_once() -> Dict[str, Any]:
    """Single iteration of the reconciliation loop. Returns the report for this
    iteration."""
    iteration_start = datetime.now(timezone.utc)
    report: Dict[str, Any] = {
        "iteration_start": iteration_start.isoformat(),
        "git": {},
        "devices": {},
        "wipe": None,
        "errors": [],
    }

    # 1. Pull Git
    try:
        pulled = git_watcher.pull()
        sha = git_watcher.current_commit_sha()
        report["git"] = {"pull_succeeded": pulled, "head_sha": sha}
    except git_watcher.GitError as e:
        log.error("Git error (operator action required): %s", e)
        report["errors"].append({"phase": "git", "error": str(e)})
        return report

    # 2. Resolve target state
    try:
        target_state = state_resolver.resolve()
        inventory = state_resolver.get_inventory()
        wipe_directive = state_resolver.get_wipe_directive()
    except ResolverError as e:
        log.error("Resolver error (fix YAML and recommit): %s", e)
        report["errors"].append({"phase": "resolve", "error": str(e)})
        return report

    # 3. Per-device convergence
    inventory_by_name = {d["name"]: d for d in inventory}

    for device_name, target_changes in target_state.items():
        device = inventory_by_name.get(device_name)
        if device is None:
            report["errors"].append({"phase": "convergence", "device": device_name, "error": "not in inventory"})
            continue

        device_report: Dict[str, Any] = {"mgmt_ip": device["mgmt_ip"]}

        if not is_reachable(device["mgmt_ip"]):
            device_report["status"] = "unreachable"
            device_report["pending_changes"] = len(target_changes)
            report["devices"][device_name] = device_report
            continue

        if not target_changes:
            device_report["status"] = "blank_no_changes"
            report["devices"][device_name] = device_report
            continue

        try:
            results = apply_changes_to_device(device, target_changes)
            device_report["status"] = "converged"
            device_report["change_results"] = results
        except Exception as e:
            device_report["status"] = "convergence_exception"
            device_report["error"] = str(e)
            device_report["traceback"] = traceback.format_exc()

        report["devices"][device_name] = device_report

    # 4. Wipe handling (after normal convergence so wipes win on conflict)
    if wipe_directive and report["git"]["head_sha"]:
        wipe_state = load_wipe_state()
        current_sha = report["git"]["head_sha"]
        if wipe_state["last_completed_sha"] != current_sha:
            log.info("wipe_now=true and SHA differs from last completed wipe — performing wipe")
            wipe_summary = perform_wipe(inventory)
            save_wipe_state(current_sha)
            report["wipe"] = wipe_summary
        else:
            log.debug("wipe_now=true but already acted on this commit; skipping")
            report["wipe"] = {"skipped": True, "reason": "already_acted_on_this_commit"}

    report["iteration_end"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    load_dotenv()

    if "LAB_USER" not in os.environ or "LAB_PASS" not in os.environ:
        log.error("LAB_USER and LAB_PASS must be set in .env or environment")
        sys.exit(1)

    log.info("reconciler starting (interval=%ds)", INTERVAL_SECONDS)
    log.info("report dir: %s", REPORT_DIR)
    log.info("state dir:  %s", STATE_DIR)

    while not _shutdown_requested:
        try:
            report = reconcile_once()
            write_report(report)
        except Exception as e:
            log.exception("unhandled exception in reconcile_once: %s", e)

        # Sleep in 1s chunks so SIGTERM is responsive
        for _ in range(INTERVAL_SECONDS):
            if _shutdown_requested:
                break
            time.sleep(1)

    log.info("reconciler stopped")


if __name__ == "__main__":
    main()
