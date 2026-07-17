"""Continuous reconciliation service for the lab devices.

Architecture:
  1. Pull latest from Git
  2. Resolve intent, inventory, and profiles into per-device target state
  3. For each device: probe reachability, compare target vs observed, apply delta
  4. Handle maintenance.wipe_now (idempotent on commit SHA)
  5. Write a report
  6. Sleep 60 seconds
  7. Repeat until the service is stopped

Failure handling:
  - Invalid YAML: log the error and retry on the next loop
  - Git pull failure: continue with the last local state
  - Unreachable device: mark it pending and continue
  - Handler failure: record it and continue with other work

Invocation:
  python -m reconciler.reconciler

Or under systemd:
  systemctl start network-reconciler

Environment variables are normally set in the systemd unit:
  RECONCILER_INTERVAL_SECONDS: loop interval, default 60
  RECONCILER_REPORT_DIR: report directory
  RECONCILER_STATE_DIR: wipe-state directory
  LAB_USER and LAB_PASS: device credentials
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

# dispatch.py lives at the repo root: make it importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dispatch import (  # noqa: E402
    HANDLERS,
    SUCCESS_STATUSES,
    SKIPPED_STATUS,
    check_dependencies,
    record_outcome,
    validate_ncclient_device_type,
)

from reconciler import git_watcher, state_resolver
from reconciler.state_resolver import ResolverError


# Configuration

INTERVAL_SECONDS = int(os.environ.get("RECONCILER_INTERVAL_SECONDS", "60"))
REPORT_DIR = Path(os.environ.get("RECONCILER_REPORT_DIR", "/var/lib/network-automation/reports"))
STATE_DIR = Path(os.environ.get("RECONCILER_STATE_DIR", "/var/lib/network-automation"))
WIPE_STATE_FILE = STATE_DIR / "wipe-state.json"


# Logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("reconciler")


# Graceful shutdown

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    log.info("received signal %d, shutting down at end of current iteration", signum)
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# Reachability probe


def is_reachable(mgmt_ip: str, port: int = 830, timeout: float = 3.0) -> bool:
    """Return whether the NETCONF TCP port accepts a connection."""
    try:
        with socket.create_connection((mgmt_ip, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


# Blank-mode convergence probe


_MISSING = object()
_MANAGED_INTERFACE_TYPES = frozenset({
    "GigabitEthernet",
    "TenGigabitEthernet",
    "FortyGigabitEthernet",
    "Loopback",
    "Vlan",
    "Port-channel",
    "Tunnel",
})
_DEFAULT_VLAN_IDS = frozenset({1, 1002, 1003, 1004, 1005})
_CONFIG_PROBE_PATHS = {
    "interface": "/interface",
    "router": "/router",
    "ip": "/ip",
    "vlan": "/vlan",
}


def _local_value(mapping: Dict[str, Any], name: str) -> Any:
    """Return one unqualified or module-qualified child value."""
    if not isinstance(mapping, dict):
        raise ValueError(f"expected an object while reading {name}")
    matches = [
        value
        for key, value in mapping.items()
        if key == name or key.endswith(f":{name}")
    ]
    if len(matches) > 1:
        raise ValueError(f"ambiguous RESTCONF keys for {name}")
    return matches[0] if matches else _MISSING


def _records(value: Any, label: str) -> List[Dict[str, Any]]:
    """Normalise a RESTCONF list that may be encoded as one object."""
    if value is _MISSING or value is None:
        return []
    if isinstance(value, dict):
        return [value] if value else []
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise ValueError(f"unexpected RESTCONF shape for {label}")


def _interface_has_managed_config(payload: Dict[str, Any]) -> bool:
    interface_data = _local_value(payload, "interface")
    if not isinstance(interface_data, dict):
        raise ValueError("RESTCONF interface container is missing or malformed")

    for qualified_type, value in interface_data.items():
        interface_type = qualified_type.rsplit(":", 1)[-1]
        if interface_type not in _MANAGED_INTERFACE_TYPES:
            continue

        for interface in _records(value, f"interface/{interface_type}"):
            name = _local_value(interface, "name")
            if name is _MISSING:
                raise ValueError(f"{interface_type} entry has no name")

            # GigabitEthernet1 carries management access on this fleet. Nothing
            # on that interface can authorize a destructive wipe.
            if interface_type == "GigabitEthernet" and str(name) == "1":
                continue

            description = _local_value(interface, "description")
            if description is not _MISSING:
                if description is not None and not isinstance(description, str):
                    raise ValueError("interface description is not text")
                if description and description.strip():
                    return True

            ip_data = _local_value(interface, "ip")
            if ip_data is not _MISSING:
                if not isinstance(ip_data, dict):
                    raise ValueError("interface IP container is malformed")
                address_data = _local_value(ip_data, "address")
                if address_data is not _MISSING:
                    if not isinstance(address_data, dict):
                        raise ValueError("interface address container is malformed")
                    primary = _local_value(address_data, "primary")
                    secondary = _local_value(address_data, "secondary")
                    if _records(primary, "interface primary address"):
                        return True
                    if _records(secondary, "interface secondary address"):
                        return True
                helpers = _local_value(ip_data, "helper-address")
                if _records(helpers, "interface helper-address"):
                    return True

            standby = _local_value(interface, "standby")
            if standby is not _MISSING:
                if not isinstance(standby, dict):
                    raise ValueError("interface standby container is malformed")
                if _records(
                    _local_value(standby, "standby-list"),
                    "interface standby-list",
                ):
                    return True

            channel_group = _local_value(interface, "channel-group")
            if channel_group is not _MISSING:
                if not isinstance(channel_group, dict):
                    raise ValueError("interface channel-group is malformed")
                if _local_value(channel_group, "number") is not _MISSING:
                    return True

            switchport = _local_value(interface, "switchport")
            if switchport is not _MISSING:
                if not isinstance(switchport, dict):
                    raise ValueError("interface switchport container is malformed")
                mode = _local_value(switchport, "mode")
                if mode is not _MISSING:
                    if not isinstance(mode, dict):
                        raise ValueError("interface switchport mode is malformed")
                    if _local_value(mode, "trunk") is not _MISSING:
                        return True

                access = _local_value(switchport, "access")
                if access is not _MISSING:
                    if not isinstance(access, dict):
                        raise ValueError("interface access VLAN is malformed")
                    vlan = _local_value(access, "vlan")
                    if vlan is not _MISSING:
                        if not isinstance(vlan, dict):
                            raise ValueError("interface access VLAN is malformed")
                        vlan_id = _local_value(vlan, "vlan")
                        if vlan_id is not _MISSING and str(vlan_id) != "1":
                            return True

                trunk = _local_value(switchport, "trunk")
                if trunk is not _MISSING:
                    if not isinstance(trunk, dict):
                        raise ValueError("interface trunk container is malformed")
                    if trunk:
                        return True

            # A Port-channel is not present in the factory configuration. Its
            # existence is enough to identify EtherChannel configuration.
            if interface_type == "Port-channel":
                return True

    return False


def _router_has_managed_config(payload: Dict[str, Any]) -> bool:
    router = _local_value(payload, "router")
    if not isinstance(router, dict):
        raise ValueError("RESTCONF router container is missing or malformed")

    if _records(_local_value(router, "ospf"), "legacy OSPF process"):
        return True

    wrapped = _local_value(router, "router-ospf")
    if wrapped is _MISSING:
        return False
    if not isinstance(wrapped, dict):
        raise ValueError("wrapped OSPF container is malformed")
    ospf = _local_value(wrapped, "ospf")
    if ospf is _MISSING:
        return False
    if not isinstance(ospf, dict):
        raise ValueError("wrapped OSPF process container is malformed")
    return bool(_records(_local_value(ospf, "process-id"), "wrapped OSPF process"))


def _ip_has_managed_config(payload: Dict[str, Any]) -> bool:
    ip_data = _local_value(payload, "ip")
    if not isinstance(ip_data, dict):
        raise ValueError("RESTCONF IP container is missing or malformed")

    route = _local_value(ip_data, "route")
    if route is not _MISSING:
        if not isinstance(route, dict):
            raise ValueError("static route container is malformed")
        routes = _local_value(route, "ip-route-interface-forwarding-list")
        if _records(routes, "static route"):
            return True

    dhcp = _local_value(ip_data, "dhcp")
    if dhcp is _MISSING:
        return False
    if not isinstance(dhcp, dict):
        raise ValueError("DHCP container is malformed")
    if _records(_local_value(dhcp, "pool"), "DHCP pool"):
        return True
    excluded = _local_value(dhcp, "excluded-address")
    if excluded is not _MISSING and excluded not in ({}, []):
        if not isinstance(excluded, (dict, list)):
            raise ValueError("DHCP excluded-address container is malformed")
        return True
    return False


def _vlan_has_managed_config(payload: Dict[str, Any]) -> bool:
    vlan_data = _local_value(payload, "vlan")
    if not isinstance(vlan_data, dict):
        raise ValueError("RESTCONF VLAN container is missing or malformed")

    for vlan in _records(_local_value(vlan_data, "vlan-list"), "VLAN list"):
        vlan_id = _local_value(vlan, "id")
        if vlan_id is _MISSING:
            raise ValueError("VLAN entry has no id")
        try:
            numeric_id = int(vlan_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid VLAN id {vlan_id!r}") from exc
        if numeric_id not in _DEFAULT_VLAN_IDS:
            return True
    return False


def _payloads_have_managed_config(payloads: Dict[str, Optional[Dict[str, Any]]]) -> bool:
    inspectors = {
        "interface": _interface_has_managed_config,
        "router": _router_has_managed_config,
        "ip": _ip_has_managed_config,
        "vlan": _vlan_has_managed_config,
    }
    results = [
        payload is not None and inspectors[name](payload)
        for name, payload in payloads.items()
    ]
    return any(results)


def probe_has_config(device: Dict[str, Any]) -> bool:
    """Return whether configuration managed by the handlers is present.

    Every RESTCONF read must succeed or be an unconfigured-path 404 before the
    result can be True. Any read or parsing ambiguity returns False so this
    probe can never authorize a wipe from incomplete information.
    """
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    host    = device["mgmt_ip"]
    auth    = (os.environ.get("LAB_USER", ""), os.environ.get("LAB_PASS", ""))
    headers = {"Accept": "application/yang-data+json"}
    base    = f"https://{host}/restconf/data/Cisco-IOS-XE-native:native"

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}
    try:
        for name, path in _CONFIG_PROBE_PATHS.items():
            r = requests.get(
                f"{base}{path}",
                auth=auth,
                headers=headers,
                verify=False,
                timeout=5,
            )
            if r.status_code == 404:
                payloads[name] = None
                continue
            if r.status_code != 200:
                raise ValueError(f"{path} returned HTTP {r.status_code}")
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError(f"{path} returned non-object JSON")
            payloads[name] = data

        has_config = _payloads_have_managed_config(payloads)
        if has_config:
            log.debug("probe_has_config: %s has managed configuration", device["name"])
        return has_config
    except Exception as exc:
        log.warning(
            "probe_has_config: %s probe was inconclusive; refusing wipe: %s",
            device.get("name", host),
            exc,
        )
        return False


# Wipe handling


def load_wipe_state() -> Dict[str, Any]:
    """Return per-device progress for the current maintenance-wipe commit.

    Old state files stored only one completed SHA. Treat them as empty so each
    device is checked again after an upgrade.
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


def _should_retry_legacy_ssh(exc: BaseException) -> bool:
    """Return whether a Paramiko error identifies RSA algorithm negotiation."""
    try:
        import paramiko
    except ImportError:
        return False

    if not isinstance(
        exc,
        (paramiko.ssh_exception.AuthenticationException,
         paramiko.ssh_exception.SSHException),
    ):
        return False

    messages = []
    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current).lower())
        current = current.__cause__ or current.__context__

    text = " ".join(messages)
    markers = (
        "ssh-rsa",
        "rsa-sha2-256",
        "rsa-sha2-512",
        "pubkey algorithm",
        "public key algorithm",
        "signature algorithm",
    )
    return any(marker in text for marker in markers)


def _new_ssh_client(paramiko_module):
    client = paramiko_module.SSHClient()
    client.set_missing_host_key_policy(paramiko_module.AutoAddPolicy())
    return client


def _wipe_device_ssh(
    device: Dict[str, Any],
    username: str,
    password: str,
) -> tuple[str, Optional[str]]:
    """Erase one device and return the result plus SSH compatibility mode.

    An interactive Paramiko channel is used because both commands may prompt
    for confirmation. Scheduling the reload one minute ahead lets the SSH
    session close and allows the loop to reach the remaining devices first.
    """
    import time
    try:
        import paramiko
    except ImportError:
        return "error: paramiko not installed: run: pip install paramiko", None

    client = _new_ssh_client(paramiko)
    ssh_compat: Optional[str] = None
    connect_args = {
        "hostname": device["mgmt_ip"],
        "port": 22,
        "username": username,
        "password": password,
        "timeout": 15,
        "look_for_keys": False,
        "allow_agent": False,
    }
    try:
        try:
            client.connect(**connect_args)
            ssh_compat = "modern"
        except Exception as exc:
            if not _should_retry_legacy_ssh(exc):
                raise

            try:
                client.close()
            finally:
                client = _new_ssh_client(paramiko)

            # IOS XE 16.8 devices in this fleet may offer only ssh-rsa user-key
            # signatures and diffie-hellman-group14-sha1 KEX. Paramiko 3.4 can
            # still negotiate the KEX, but prefers RSA-SHA2 signatures. Retry
            # once without those pubkey algorithms when negotiation reports
            # that specific mismatch.
            log.warning(
                "wipe: %s retrying SSH with legacy ssh-rsa compatibility",
                device["name"],
            )
            client.connect(
                **connect_args,
                disabled_algorithms={
                    "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
                },
            )
            ssh_compat = "legacy-sha1"

        log.info("wipe: %s SSH mode: %s", device["name"], ssh_compat)
        channel = client.invoke_shell()
        time.sleep(1.5)
        channel.recv(4096)  # drain banner/motd

        # Clear startup-config.
        channel.send("write erase\n")
        time.sleep(2)
        output = channel.recv(4096).decode("utf-8", errors="replace")
        if "confirm" in output.lower():
            channel.send("\n")
            time.sleep(1)
            channel.recv(4096)

        # Schedule the reload so the SSH session can close normally.
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
        return "success", ssh_compat

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return f"error: {e}", ssh_compat


def perform_wipe(devices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wipe reachable devices and return a per-device summary.

    A failure does not stop the remaining devices. The caller records successful
    names so failed and unreachable devices can be retried.
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
            log.warning("wipe: %s (%s) unreachable: skipping", device["name"], device["mgmt_ip"])
            continue

        log.info("wiping %s (%s)...", device["name"], device["mgmt_ip"])
        result, ssh_compat = _wipe_device_ssh(device, username, password)

        if result == "success":
            summary["wiped"] += 1
            summary["details"].append({
                "device": device["name"],
                "status": "wiped",
                "ssh_compat": ssh_compat,
            })
            log.info("wipe: %s: OK (reload scheduled in 1 min)", device["name"])
        else:
            summary["failed"] += 1
            detail = {"device": device["name"], "status": "failed", "error": result}
            if ssh_compat:
                detail["ssh_compat"] = ssh_compat
            summary["details"].append(detail)
            log.error("wipe: %s: FAILED: %s", device["name"], result)

    return summary


# Apply changes via existing engine


def apply_changes_to_device(
    device: Dict[str, Any],
    changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply a list of changes to one device through ``HANDLERS``.

    Dependency model: ID-based:
        depends_on: gi001-ip             # single id
        depends_on: [gi001-ip, vlans]    # list of ids
        (omitted)                        # no prerequisites

    If any prerequisite did not finish in (success, already_correct),
    the task is skipped with status='skipped_due_to_dependency' and
    the skip cascades: anything depending on a skipped task is also
    skipped. Tasks without an `id` are executed in document order
    but cannot be referenced as prerequisites.

    Tasks without an ID still run in order but cannot be dependencies.
    """
    device_params = {
        "host":           device["mgmt_ip"],
        "port":           830,
        "username":       os.environ["LAB_USER"],
        "password":       os.environ["LAB_PASS"],
        "hostkey_verify": False,
        "device_params":  {"name": device["ncclient_device_type"]},
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

        unmet = check_dependencies(change, task_status)
        if unmet:
            log.warning(
                "[%s] skipping %s (id=%s): unmet prerequisites: %s",
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


# Report writing


def write_report(report: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"reconcile-{timestamp}.json"
    latest_link = REPORT_DIR / "latest.json"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # Point latest.json at the report from this iteration.
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(report_path.name)


# Main loop


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
    inventory_errors = {}
    for device in inventory:
        inventory_error = validate_ncclient_device_type(device)
        if not inventory_error:
            continue
        device_name = device["name"]
        inventory_errors[device_name] = inventory_error
        log.error("[%s] invalid inventory: %s", device_name, inventory_error)
        report["devices"][device_name] = {
            "mgmt_ip": device.get("mgmt_ip"),
            "status": "invalid_inventory",
            "error": inventory_error,
        }

    for device_name, target_changes in target_state.items():
        device = inventory_by_name.get(device_name)
        if device is None:
            report["errors"].append({"phase": "convergence", "device": device_name, "error": "not in inventory"})
            continue

        device_report: Dict[str, Any] = {"mgmt_ip": device["mgmt_ip"]}

        if device_name in inventory_errors:
            continue

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

            # Blank mode removes managed configuration when any is found.
            if probe_has_config(device):
                log.info(
                    "%s is in blank mode but has managed config: wiping",
                    device_name,
                )
                wipe_result = perform_wipe([device])
                device_report["status"]      = "wiped_for_blank_convergence"
                device_report["wipe_result"] = wipe_result
            else:
                device_report["status"] = "blank_confirmed"
                log.debug("%s blank confirmed: no managed config found", device_name)
            report["devices"][device_name] = device_report
            continue

        try:
            results = apply_changes_to_device(device, target_changes)
            # Distinguish handler failures from dependency skips in the summary.
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

    # 4. Process explicit wipes after regular convergence.
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
            and d["name"] not in inventory_errors
        ]
        wipe_targets = [d for d in eligible if d["name"] not in completed]

        if wipe_targets:
            log.info(
                "wipe_now=true: wiping %d remaining device(s), %d already complete",
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
                    "wipe attempted but 0 devices wiped (unreachable=%d, failed=%d): "
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

        # Poll once per second so SIGTERM does not wait for the full interval.
        for _ in range(INTERVAL_SECONDS):
            if _shutdown_requested:
                break
            time.sleep(1)

    log.info("reconciler stopped")


if __name__ == "__main__":
    main()
