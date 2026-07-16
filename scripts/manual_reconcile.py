#!/usr/bin/env python3
"""
manual_reconcile.py — One-shot reconciliation, bypasses the loop.

Useful for:
  - Debugging — run reconciliation manually and see the report immediately
  - Testing changes to a profile or class_state.yaml before relying on the
    background loop to pick them up
  - First-time validation against a single rack before enabling the systemd unit

Usage:
    python3 scripts/manual_reconcile.py [--dry-run]

--dry-run:
    Resolve the target state and probe device reachability, but do NOT apply
    any changes. Prints what would be done. Use this to verify your profile
    renders correctly before letting the reconciler touch real devices.
"""

import argparse
import json
import sys
from pathlib import Path

# Make reconciler importable without installing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconciler import reconciler as r
from reconciler import state_resolver
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and probe but do not apply changes")
    args = parser.parse_args()

    load_dotenv()

    if args.dry_run:
        print("=== DRY RUN ===")
        target = state_resolver.resolve()
        inventory = state_resolver.get_inventory()
        wipe = state_resolver.get_wipe_directive()

        print(f"\nIntent declares wipe_now={wipe}")
        print(f"\nResolved target state:")
        print(json.dumps(target, indent=2, default=str))

        print(f"\nReachability probes:")
        for dev in inventory:
            reachable = r.is_reachable(dev["mgmt_ip"])
            status = "REACHABLE" if reachable else "unreachable"
            print(f"  {dev['name']:30s} {dev['mgmt_ip']:18s} {status}")
        return

    print("=== LIVE RECONCILE ===")
    report = r.reconcile_once()
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
