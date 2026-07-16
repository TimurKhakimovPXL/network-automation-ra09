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

# dispatch.py lives at the repo root — make it importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dispatch import (  # noqa: E402
    HANDLERS,
    SUCCESS_STATUSES,
    SKIPPED_STATUS,
    check_dependencies,
    record_outcome,
)

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


# ─── Blank-mode convergence probe ────────────────────────────────────────────


def probe_has_config(device: Dict[str, Any]) -> bool:
    """Return True if the device has any non-default configuration on paths
    managed by this engine.

    Probes four RESTCONF paths that cover every handler type. A 200 response
    on any of them indicates configuration is present and the device is not in
    the desired blank state.

    Probe paths:
        /router/ospf      — any OSPF process present?
        /ip/route         — any static routes present?
        /ip/dhcp/pool     — any DHCP server pools present?
        /vlan             — any VLAN definitions present?

    Returns False on any network error so that a probe failure does not trigger
    an unexpected wipe. The conservative-safe direction is 'assume already blank'
    if we cannot reach the device — the is_reachable() gate already handles
    unreachable devices before we get here.
    """
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    host    = device["mgmt_ip"]
    auth    = (os.environ.get("LAB_USER", ""), os.environ.get("LAB_PASS", ""))
    headers = {"Accept": "application/yang-data+json"}
    base    = f"https://{host}/restconf/data/Cisco-IOS-XE-native:native"

    probe_paths = [
        "/router/ospf",
        "/ip/route",
        "/ip/dhcp/pool",
        "/vlan",
    ]

    for path in probe_paths:
        try:
            r = requests.get(
                f"{base}{path}",
                auth=auth,
                headers=headers,
                verify=False,
                timeout=5,
            )
            if r.status_code == 200:
                # Any 200 with body data means config exists on this path.
                # 404 = feature not configured = clean.
                data = r.json()
                if data:
                    log.debug(
                        "probe_has_config: %s has data on %s — device not blank",
                        device["name"], path,
                    )
                    return True
        except Exception as exc:
            # Network error: be conservative, do not trigger wipe on uncertainty.
            log.debug("probe_has_config: %s path %s error: %s", device["name"], path, exc)

    return False


# ─── Wipe handling ────────────────────────────────────────────────────────────


def load_wipe_state() -> Dict[str, Any]:
    """Return per-device progress for the current maintenance-wipe commit.

    Older state files tracked only a single completed SHA. They are treated as
    having no completed devices so a controller upgrade cannot silently skip
    devices that previously failed or were unreachable.
    """
    empty = {"commit_sha": None, "completed_devices": [], "updated_at": None}
    if not WIPE_STATE_FILE.exists():
        return empty
    try:
        with WIPE_STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("wipe-state.json unreadable, treating as empty")
        return empty

    if not isinstance(data, dict) or "commit_sha" not in data:
        log.warning("legacy wipe state detected; retrying with per-device tracking")
        return empty
    completed = data.get("completed_devices") or []
    if not isinstance(completed, list):
        return empty
    return {
        "commit_sha": data.get("commit_sha"),
        "completed_devices": [str(name) for name in completed],
        "updated_at": data.get("updated_at"),
    }


def save_wipe_state(commit_sha: str, completed_devices: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "commit_sha": commit_sha,
        "completed_devices": sorted(completed_devices),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with WIPE_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _wipe_device_ssh(device: Dict[str, Any], username: str, password: str) -> str:
    """Execute 'write erase' + 'reload in 1' on a single device via SSH.

    Returns 'success' on clean execution, or an error string describing the
    failure. Never raises — caller accumulates results across all devices.

    Why paramiko interactive shell rather than exec_command:
        IOS XE 'write erase' and 'reload' are interactive commands that emit
        confirmation prompts. exec_command opens a non-interactive exec channel
        that does not handle these prompts. invoke_shell() gives a PTY-backed
        channel that mirrors the CLI behaviour exactly.

    Why 'reload in 1' instead of 'reload':
        'reload' (immediate) races against the SSH session teardown and can
        leave the channel hanging. 'reload in 1' schedules the reload 1 minute
        in the future, returns cleanly, and lets the automation finish touching
        all other devices before any device goes offline.

    After the reload timer fires the device is unreachable for ~2 minutes
    (IOS XE boot time on ISR4200). The reconciler's 60-second polling loop
    will see the device as unreachable during that window and skip it, then
    resume normal convergence once it comes back up blank.
    """
    import time
    try:
        import paramiko
    except ImportError:
        return "error: paramiko not installed — run: pip install paramiko"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            device["mgmt_ip"],
            port=22,
            username=username,
            password=password,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        channel = client.invoke_shell()
        time.sleep(1.5)
        channel.recv(4096)  # drain banner/motd

        # Step 1: write erase — clears startup-config
        channel.send("write erase\n")
        time.sleep(2)
        output = channel.recv(4096).decode("utf-8", errors="replace")
        if "confirm" in output.lower():
            channel.send("\n")
            time.sleep(1)
            channel.recv(4096)

        # Step 2: reload in 1 — scheduled reload so SSH exits cleanly
        channel.send("reload in 1\n")
        time.sleep(1)
        output2 = channel.recv(4096).decode("utf-8", errors="replace")
        # IOS XE prompts: "Proceed with reload? [confirm]" or
        # "System configuration has been modified. Save? [yes/no]:"
        if "confirm" in output2.lower() or "modified" in output2.lower():
            channel.send("\n")
            time.sleep(1)
            channel.recv(4096)

        channel.close()
        client.close()
        return "success"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return f"error: {e}"


def perform_wipe(devices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wipe all reachable devices. Each wipe is independent; failures do not
    abort the rest. Returns a summary dict for the report.

    Implementation: SSH to each device, issue 'write erase' to clear startup
    config, then 'reload in 1' to schedule a reload 1 minute from now.
    On reload the device comes up with a factory-default running config,
    ready for Day-0 re-provisioning by the reconciler's next iteration.

    The caller records the names of successful devices. Failed and unreachable
    devices remain absent from state and are retried on the next iteration.
    """
    username = os.environ.get("LAB_USER", "")
    password = os.environ.get("LAB_PASS", "")

    summary = {
        "total":       len(devices),
        "wiped":       0,
        "unreachable": 0,
        "failed":      0,
        "details":     [],
    }

    for device in devices:
        if not is_reachable(device["mgmt_ip"]):
            summary["unreachable"] += 1
            summary["details"].append({"device": device["name"], "status": "unreachable"})
            log.warning("wipe: %s (%s) unreachable — skipping", device["name"], device["mgmt_ip"])
            continue

        log.info("wiping %s (%s)…", device["name"], device["mgmt_ip"])
        result = _wipe_device_ssh(device, username, password)

        if result == "success":
            summary["wiped"] += 1
            summary["details"].append({"device": device["name"], "status": "wiped"})
            log.info("wipe: %s — OK (reload scheduled in 1 min)", device["name"])
        else:
            summary["failed"] += 1
            summary["details"].append({"device": device["name"], "status": "failed", "error": result})
            log.error("wipe: %s — FAILED: %s", device["name"], result)

    return summary


# ─── Apply changes via existing engine ───────────────────────────────────────


def apply_changes_to_device(
    device: Dict[str, Any],
    changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Apply each change to one device by dispatching through the
    shared HANDLERS registry (defined in dispatch.py at the repo
    root).

    Dependency model — ID-based:
        depends_on: gi001-ip             # single id
        depends_on: [gi001-ip, vlans]    # list of ids
        (omitted)                        # no prerequisites

    If any prerequisite did not finish in (success, already_correct),
    the task is skipped with status='skipped_due_to_dependency' and
    the skip cascades — anything depending on a skipped task is also
    skipped. Tasks without an `id` are executed in document order
    but cannot be referenced as prerequisites.

    Both this function and labs/network-automation/automate.py call
    the same dependency helpers from dispatch.py, so CLI debug runs
    and reconciler runs apply identical dependency semantics.
    """
    device_params = {
        "host":           device["mgmt_ip"],
        "port":           830,
        "username":       os.environ["LAB_USER"],
        "password":       os.environ["LAB_PASS"],
        "hostkey_verify": False,
        "device_params":  {"name": device.get("ncclient_device_type", "csr")},
        "allow_agent":    False,
        "look_for_keys":  False,
    }

    results: List[Dict[str, Any]] = []
    task_status: Dict[str, str] = {}  # id → status, per-device scoped

    for change in changes:
        change_type = change.get("type")
        if not change_type:
            result = {"status": "missing_type", "change": change}
            results.append(result)
            record_outcome(change, result, task_status)
            continue

        handler = HANDLERS.get(change_type)
        if handler is None:
            result = {"status": "unknown_type", "type": change_type}
            results.append(result)
            record_outcome(change, result, task_status)
            continue

        # Dependency gate
        unmet = check_dependencies(change, task_status)
        if unmet:
            log.warning(
                "[%s] skipping %s (id=%s) — unmet prerequisites: %s",
                device["name"], change_type, change.get("id"), unmet,
            )
            result = {
                "status": SKIPPED_STATUS,
                "type":   change_type,
                "id":     change.get("id"),
                "error":  f"Prerequisite task(s) did not succeed: {unmet}",
            }
            results.append(result)
            record_outcome(change, result, task_status)
            continue

        # Execute
        try:
            result = handler(device_params, device["name"], change)
        except Exception as e:
            result = {
                "status":    "handler_exception",
                "type":      change_type,
                "id":        change.get("id"),
                "error":     str(e),
                "traceback": traceback.format_exc(),
            }

        results.append(result)
        record_outcome(change, result, task_status)

    return results


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
        "git":     {},
        "devices": {},
        "wipe":    None,
        "errors":  [],
    }

    # 1. Pull Git
    try:
        pulled = git_watcher.pull()
        sha    = git_watcher.current_commit_sha()
        report["git"] = {"pull_succeeded": pulled, "head_sha": sha}
    except git_watcher.GitError as e:
        log.error("Git error (operator action required): %s", e)
        report["errors"].append({"phase": "git", "error": str(e)})
        return report

    # 2. Resolve target state
    try:
        target_state   = state_resolver.resolve()
        inventory      = state_resolver.get_inventory()
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

        # Observe mode: probe reachability only. No writes, no wipes, no
        # config probing. Right mode for devices the engine cannot safely
        # manage yet (e.g. switches before switch-specific ZTP and handlers).
        if target_changes is None:
            device_report["status"] = (
                "observed_reachable" if is_reachable(device["mgmt_ip"])
                else "observed_unreachable"
            )
            report["devices"][device_name] = device_report
            continue

        if not is_reachable(device["mgmt_ip"]):
            device_report["status"]          = "unreachable"
            device_report["pending_changes"] = len(target_changes)
            report["devices"][device_name]   = device_report
            continue

        if not target_changes:
            if wipe_directive:
                # The explicit maintenance wipe below owns this device for the
                # current iteration. Avoid wiping a blank-mode device twice and
                # keep maintenance progress isolated from blank convergence.
                device_report["status"] = "pending_maintenance_wipe"
                report["devices"][device_name] = device_report
                continue

            # Bug 4 fix — blank mode must actively converge, not passively skip.
            # Probe the device; if managed config is present, wipe it.
            if probe_has_config(device):
                log.info(
                    "%s is in blank mode but has managed config — wiping",
                    device_name,
                )
                wipe_result = perform_wipe([device])
                device_report["status"]      = "wiped_for_blank_convergence"
                device_report["wipe_result"] = wipe_result
            else:
                device_report["status"] = "blank_confirmed"
                log.debug("%s blank confirmed — no managed config found", device_name)
            report["devices"][device_name] = device_report
            continue

        try:
            results = apply_changes_to_device(device, target_changes)
            # Status reflects what actually happened. "converged" is only valid
            # if every change succeeded or was already correct. If any handler
            # failed, surface that; if changes were only skipped via depends_on,
            # surface that distinctly so the operator sees the cascade.
            all_ok = all(r.get("status") in SUCCESS_STATUSES for r in results)
            ok_or_skipped = SUCCESS_STATUSES | {SKIPPED_STATUS}
            any_failed = any(r.get("status") not in ok_or_skipped for r in results)
            device_report["status"] = (
                "converged" if all_ok else
                "converged_with_failures" if any_failed else
                "converged_with_skips"
            )
            device_report["change_results"] = results
        except Exception as e:
            device_report["status"]    = "convergence_exception"
            device_report["error"]     = str(e)
            device_report["traceback"] = traceback.format_exc()

        report["devices"][device_name] = device_report

    # 4. Wipe handling (after normal convergence so wipes win on conflict)
    if wipe_directive and report["git"]["head_sha"]:
        wipe_state  = load_wipe_state()
        current_sha = report["git"]["head_sha"]
        completed = (
            set(wipe_state["completed_devices"])
            if wipe_state["commit_sha"] == current_sha
            else set()
        )
        eligible = [
            d for d in inventory
            if target_state.get(d["name"]) is not None
        ]
        wipe_targets = [d for d in eligible if d["name"] not in completed]

        if wipe_targets:
            log.info(
                "wipe_now=true — wiping %d remaining device(s), %d already complete",
                len(wipe_targets), len(completed),
            )
            wipe_summary = perform_wipe(wipe_targets)
            wipe_summary["already_completed"] = sorted(completed)
            report["wipe"] = wipe_summary
            newly_completed = {
                item["device"]
                for item in wipe_summary["details"]
                if item["status"] == "wiped"
            }
            if newly_completed:
                completed.update(newly_completed)
                save_wipe_state(current_sha, completed)
                log.info(
                    "wipe progress: %d/%d complete, %d unreachable, %d failed",
                    len(completed), len(eligible),
                    wipe_summary["unreachable"], wipe_summary["failed"],
                )
            else:
                log.warning(
                    "wipe attempted but 0 devices wiped (unreachable=%d, failed=%d) — "
                    "SHA not persisted, will retry next iteration",
                    wipe_summary["unreachable"], wipe_summary["failed"],
                )
        else:
            log.debug("wipe_now=true but every eligible device is complete for this commit")
            report["wipe"] = {
                "skipped": True,
                "reason": "all_eligible_devices_completed_for_commit",
                "completed_devices": sorted(completed),
            }

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
