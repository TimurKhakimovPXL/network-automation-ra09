"""
handlers/_debug.py

Per-run debug capture for raw RESTCONF responses.

Why this exists:
    When verify_mismatch or any unexpected status occurs against real
    hardware, the raw response body is the only artifact that shows
    what the device actually returned vs what the parser expected.
    Without it, debugging is guesswork.

Usage in a handler:
    from . import _debug

    response = _restconf_get(...)
    _debug.capture(device_name, change_type, "read", response, change)

Files are written to ./debug/<run_timestamp>/<device>/<change_type>_<seq>.json
The capture is best-effort — failure to write a debug file is logged
to stderr and never propagates to the handler.

Toggle via environment variable:
    DEBUG_CAPTURE=1   # capture every read (verbose, useful first run)
    DEBUG_CAPTURE=0   # capture only on failure (default)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests

# One-time per process: stamp a single timestamp directory for this run
_RUN_DIR: Path | None = None
_RUN_LOCK = threading.Lock()
_TASK_COUNTER: dict[tuple[str, str], int] = {}


def _get_run_dir() -> Path:
    global _RUN_DIR
    with _RUN_LOCK:
        if _RUN_DIR is None:
            stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            _RUN_DIR = Path("debug") / stamp
            _RUN_DIR.mkdir(parents=True, exist_ok=True)
    return _RUN_DIR


def _verbose() -> bool:
    return os.getenv("DEBUG_CAPTURE", "0") == "1"


def capture(device_name: str,
            change_type: str,
            phase: str,
            response: requests.Response | None,
            change: dict | None = None,
            *,
            force: bool = False) -> None:
    """
    Write a debug record for this RESTCONF interaction.

    phase: "read" | "verify" | "error"
    force: write even if DEBUG_CAPTURE=0 (use for failures)

    Never raises — debug capture failure must not affect the handler.
    """
    if not (force or _verbose()):
        return

    try:
        run_dir = _get_run_dir()
        device_dir = run_dir / device_name.replace("/", "_")
        device_dir.mkdir(parents=True, exist_ok=True)

        key = (device_name, change_type)
        with _RUN_LOCK:
            seq = _TASK_COUNTER.get(key, 0) + 1
            _TASK_COUNTER[key] = seq

        filename = device_dir / f"{seq:03d}_{change_type}_{phase}.json"

        record: dict = {
            "timestamp":   datetime.now().isoformat(),
            "device_name": device_name,
            "change_type": change_type,
            "phase":       phase,
            "change":      change,
        }

        if response is not None:
            record["http_status"] = response.status_code
            record["url"]         = response.url
            try:
                record["body_json"] = response.json()
            except (ValueError, json.JSONDecodeError):
                record["body_text"] = response.text[:8192]  # cap at 8 KiB

        with open(filename, "w") as f:
            json.dump(record, f, indent=2, default=str)

    except Exception as e:
        # Never let debug capture interfere with the actual handler outcome.
        print(f"[debug capture failed: {e}]", file=sys.stderr)
