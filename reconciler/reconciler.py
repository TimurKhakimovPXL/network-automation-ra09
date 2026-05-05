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
        "last_completed_at":  datetime.now(timezone.utc).isoformat(),
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

    save_wipe_state is only called by the caller (reconcile_once) if
    summary['wiped'] > 0. If all devices were unreachable the SHA is NOT
    persisted, so the reconciler retries the wipe on the next iteration.
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


def apply_changes_to_device(device: Dict[str, Any], changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Bridge between the reconciler and the existing handler engine.

    The existing automate.py expects to be invoked as a CLI with changes.yaml.
    To call it programmatically per-device, we import the handlers directly and
    invoke them the same way automate.py does.

    depends_on support:
        A change may declare:
            depends_on: interface_ip          # single string
            depends_on: [interface_ip, vlan]  # list
        If any declared dependency type failed earlier in this device's run,
        the change is skipped and recorded as status='skipped_depends_on'.
        The skip cascades: a skipped change also adds its own type to the
        failed set, so anything depending on it is also skipped.

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
        "host":             device["mgmt_ip"],
        "port":             830,
        "username":         os.environ["LAB_USER"],
        "password":         os.environ["LAB_PASS"],
        "hostkey_verify":   False,
        "device_params":    {"name": "csr"},
        "allow_agent":      False,
        "look_for_keys":    False,
    }

    results: List[Dict[str, Any]] = []

    # Track which change types have produced a non-success result so that
    # downstream changes declaring depends_on can be skipped.
    # Keyed on type string — if finer-grained control is needed, add an 'id'
    # leaf to the profile schema and key on that instead.
    failed_types: set = set()

    for change in changes:
        change_type = change.get("type")
        if not change_type:
            results.append({"status": "missing_type", "change": change})
            continue

        handler = HANDLERS.get(change_type)
        if handler is None:
            results.append({"status": "unknown_type", "type": change_type})
            failed_types.add(change_type)
            continue

        # ── depends_on check ─────────────────────────────────────────────────
        depends_on = change.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        blocked_by = [dep for dep in depends_on if dep in failed_types]
        if blocked_by:
            log.warning(
                "[%s] skipping %s — blocked by failed dependency: %s",
                device["name"], change_type, blocked_by,
            )
            results.append({
                "status":     "skipped_depends_on",
                "type":       change_type,
                "blocked_by": blocked_by,
            })
            # Cascade: anything that depends on this change is also skipped.
            failed_types.add(change_type)
            continue

        try:
            result = handler(device_params, device["name"], change)
        except Exception as e:
            result = {
                "status":    "handler_exception",
                "type":      change_type,
                "error":     str(e),
                "traceback": traceback.format_exc(),
            }

        # 'already_correct' is a success — idempotent no-op must not block
        # downstream changes that depend on this type.
        if result.get("status") not in ("success", "already_correct"):
            failed_types.add(change_type)

        results.append(result)

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

        if not is_reachable(device["mgmt_ip"]):
            device_report["status"]          = "unreachable"
            device_report["pending_changes"] = len(target_changes)
            report["devices"][device_name]   = device_report
            continue

        if not target_changes:
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
                if wipe_result["wiped"] > 0:
                    save_wipe_state(report["git"]["head_sha"])
            else:
                device_report["status"] = "blank_confirmed"
                log.debug("%s blank confirmed — no managed config found", device_name)
            report["devices"][device_name] = device_report
            continue

        try:
            results = apply_changes_to_device(device, target_changes)
            device_report["status"]         = "converged"
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
        if wipe_state["last_completed_sha"] != current_sha:
            log.info("wipe_now=true and SHA differs from last completed wipe — performing wipe")
            wipe_summary = perform_wipe(inventory)
            report["wipe"] = wipe_summary
            # Bug 2 fix — only persist the SHA if at least one device was
            # actually wiped. If all devices were unreachable the SHA is NOT
            # saved, so the wipe is retried on the next loop iteration rather
            # than being silently declared complete when nothing was touched.
            if wipe_summary["wiped"] > 0:
                save_wipe_state(current_sha)
                log.info(
                    "wipe complete: %d wiped, %d unreachable, %d failed",
                    wipe_summary["wiped"], wipe_summary["unreachable"], wipe_summary["failed"],
                )
            else:
                log.warning(
                    "wipe attempted but 0 devices wiped (unreachable=%d, failed=%d) — "
                    "SHA not persisted, will retry next iteration",
                    wipe_summary["unreachable"], wipe_summary["failed"],
                )
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
