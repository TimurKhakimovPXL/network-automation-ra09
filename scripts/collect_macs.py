"""
collect_macs.py — Collect GigabitEthernet0 MAC addresses from all lab devices.

Reads inventory.yaml, attempts a RESTCONF GET against the IOS XE operational
interface model for each device, and outputs:

  1. Live per-device status as it runs
  2. A summary table at the end
  3. A ready-to-paste YAML block for inventory.yaml
  4. Manual console commands for any unreachable devices

RESTCONF path used:
  GET /restconf/data/Cisco-IOS-XE-interfaces-oper:interfaces/interface=GigabitEthernet0
  Field: Cisco-IOS-XE-interfaces-oper:interface.phys-address

Usage:
  python collect_macs.py                   # all devices
  python collect_macs.py --rack 9          # single rack only
  python collect_macs.py --timeout 5       # custom per-device timeout (seconds)
  python collect_macs.py --output macs.yaml  # write YAML patch to file

Credentials:
  Loaded from .env (LAB_USER, LAB_PASS) in the same directory or any parent.

Requirements:
  pip install requests pyyaml python-dotenv urllib3
"""

import argparse
import json
import os
import sys
import urllib3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Constants ──────────────────────────────────────────────────────────────────

RESTCONF_HEADERS = {
    "Accept": "application/yang-data+json",
}

OPER_URL_TEMPLATE = (
    "https://{host}/restconf/data/"
    "Cisco-IOS-XE-interfaces-oper:interfaces/interface=GigabitEthernet0"
)

CONSOLE_CMD = "show interface GigabitEthernet0 | include Hardware"

# ── ANSI colours (disabled on non-TTY) ────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

OK    = lambda t: _c(t, "32")   # green
WARN  = lambda t: _c(t, "33")   # yellow
ERR   = lambda t: _c(t, "31")   # red
BOLD  = lambda t: _c(t, "1")    # bold


# ── Core fetch ─────────────────────────────────────────────────────────────────

def fetch_mac(host: str, username: str, password: str, timeout: float) -> Optional[str]:
    """
    Attempt RESTCONF GET for GigabitEthernet0 operational data.

    Returns the MAC string (e.g. "00:1a:2b:3c:4d:5e") on success,
    or None if the device is unreachable or the response is unexpected.

    Raises nothing — all exceptions are caught and returned as None so the
    caller can continue with the next device.
    """
    url = OPER_URL_TEMPLATE.format(host=host)
    try:
        response = requests.get(
            url,
            auth=(username, password),
            headers=RESTCONF_HEADERS,
            verify=False,
            timeout=timeout,
        )
    except requests.exceptions.ConnectTimeout:
        return None
    except requests.exceptions.ConnectionError:
        return None
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.RequestException:
        return None

    if response.status_code != 200:
        return None

    try:
        data = response.json()
    except (ValueError, KeyError):
        return None

    iface = data.get("Cisco-IOS-XE-interfaces-oper:interface", {})
    mac = iface.get("phys-address")

    if not mac or not isinstance(mac, str):
        return None

    return mac.lower()  # normalise to lowercase for DHCP reservation consistency


# ── Inventory loader ───────────────────────────────────────────────────────────

def load_inventory(repo_root: Path) -> list:
    """Load devices from infra/inventory.yaml. Falls back to inventory.yaml at
    repo root for the flat project-folder layout used during development."""
    candidates = [
        repo_root / "infra" / "inventory.yaml",
        repo_root / "inventory.yaml",
    ]
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            devices = (data or {}).get("devices", [])
            print(f"Loaded {len(devices)} devices from {path.relative_to(repo_root)}")
            return devices

    print(ERR("ERROR: inventory.yaml not found. Checked:"))
    for p in candidates:
        print(f"  {p}")
    sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect GigabitEthernet0 MAC addresses from all lab devices via RESTCONF."
    )
    parser.add_argument(
        "--rack", type=int, default=None, metavar="N",
        help="Only query rack N (1-10). Default: all racks."
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, metavar="SECONDS",
        help="Per-device connection timeout in seconds. Default: 5."
    )
    parser.add_argument(
        "--output", type=str, default=None, metavar="FILE",
        help="Write YAML patch to FILE instead of stdout only."
    )
    args = parser.parse_args()

    # ── Credentials ───────────────────────────────────────────────────────────
    load_dotenv()
    username = os.environ.get("LAB_USER")
    password = os.environ.get("LAB_PASS")
    if not username or not password:
        print(ERR("ERROR: LAB_USER and LAB_PASS must be set in .env"))
        sys.exit(1)

    # ── Devices ───────────────────────────────────────────────────────────────
    repo_root = Path(__file__).resolve().parent.parent
    all_devices = load_inventory(repo_root)

    devices = all_devices
    if args.rack is not None:
        devices = [d for d in all_devices if d.get("rack") == args.rack]
        if not devices:
            print(ERR(f"ERROR: No devices found for rack {args.rack}"))
            sys.exit(1)

    # ── Run ───────────────────────────────────────────────────────────────────
    print()
    print(BOLD(f"Collecting MACs — {len(devices)} device(s) — timeout {args.timeout}s per device"))
    print(BOLD("─" * 64))

    results = []   # list of (device_dict, mac_or_none)
    start = datetime.now(timezone.utc)

    for device in devices:
        name    = device.get("name", "unknown")
        host    = device.get("mgmt_ip", "")
        current = device.get("mac", "TODO")

        sys.stdout.write(f"  {name:<28} {host:<16} → ")
        sys.stdout.flush()

        mac = fetch_mac(host, username, password, args.timeout)

        if mac:
            status = OK(f"✓  {mac}")
        else:
            status = WARN("UNREACHABLE")

        print(status)
        results.append((device, mac))

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(BOLD("─" * 64))
    print(f"Done in {elapsed:.1f}s\n")

    # ── Summary table ─────────────────────────────────────────────────────────
    found       = [(d, m) for d, m in results if m]
    unreachable = [(d, m) for d, m in results if not m]

    print(BOLD(f"Results: {len(found)} collected, {len(unreachable)} unreachable"))

    # ── YAML patch ────────────────────────────────────────────────────────────
    if found:
        print()
        print(BOLD("─── inventory.yaml patch (devices with MAC collected) ───"))
        print("# Paste these mac: values into infra/inventory.yaml\n")

        patch_lines = []
        for device, mac in found:
            line = f"  # {device['name']}\n  mac: \"{mac}\""
            patch_lines.append(line)
            print(line)

        if args.output:
            out_path = Path(args.output)
            out_path.write_text(
                "# MAC address patch for infra/inventory.yaml\n"
                f"# Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
                + "\n\n".join(patch_lines) + "\n"
            )
            print(f"\n{OK(f'Patch written to {args.output}')}")

    # ── Manual instructions for unreachable devices ────────────────────────────
    if unreachable:
        print()
        print(BOLD("─── Unreachable — collect manually via console ───"))
        print(f"  Console command: {BOLD(CONSOLE_CMD)}\n")
        print("  Then update inventory.yaml:")
        print()
        for device, _ in unreachable:
            print(f"  # {device['name']}  ({device['mgmt_ip']})")
            print(f"  mac: \"<paste-here>\"")
            print()

    # ── Machine-readable output ───────────────────────────────────────────────
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total": len(results),
        "collected": len(found),
        "unreachable": len(unreachable),
        "devices": [
            {
                "name": d["name"],
                "rack": d.get("rack"),
                "side": d.get("side"),
                "mgmt_ip": d.get("mgmt_ip"),
                "mac": mac if mac else None,
                "status": "collected" if mac else "unreachable",
            }
            for d, mac in results
        ],
    }

    report_path = repo_root / "mac_collection_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Machine-readable report: {report_path}")


if __name__ == "__main__":
    main()
