"""Per-device maintenance-wipe progress tests."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reconciler import reconciler


DEVICES = [
    {"name": "r1", "mgmt_ip": "192.0.2.1"},
    {"name": "r2", "mgmt_ip": "192.0.2.2"},
]
TARGET = {"r1": [], "r2": []}


def _run_with_wipe_result(wipe_result, state_dir):
    with (
        patch.object(reconciler, "STATE_DIR", state_dir),
        patch.object(reconciler, "WIPE_STATE_FILE", state_dir / "wipe-state.json"),
        patch.object(reconciler.git_watcher, "pull", return_value=True),
        patch.object(reconciler.git_watcher, "current_commit_sha", return_value="abc123"),
        patch.object(reconciler.state_resolver, "resolve", return_value=TARGET),
        patch.object(reconciler.state_resolver, "get_inventory", return_value=DEVICES),
        patch.object(reconciler.state_resolver, "get_wipe_directive", return_value=True),
        patch.object(reconciler, "perform_wipe", return_value=wipe_result) as perform,
    ):
        report = reconciler.reconcile_once()
    return report, perform


def test_partial_wipe_retries_only_incomplete_devices():
    first_result = {
        "total": 2,
        "wiped": 1,
        "unreachable": 1,
        "failed": 0,
        "details": [
            {"device": "r1", "status": "wiped"},
            {"device": "r2", "status": "unreachable"},
        ],
    }
    second_result = {
        "total": 1,
        "wiped": 1,
        "unreachable": 0,
        "failed": 0,
        "details": [{"device": "r2", "status": "wiped"}],
    }

    with TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        first_report, first_perform = _run_with_wipe_result(first_result, state_dir)
        assert [d["name"] for d in first_perform.call_args.args[0]] == ["r1", "r2"]
        assert first_report["wipe"]["wiped"] == 1

        second_report, second_perform = _run_with_wipe_result(second_result, state_dir)
        assert [d["name"] for d in second_perform.call_args.args[0]] == ["r2"]
        assert second_report["wipe"]["already_completed"] == ["r1"]

        final_report, final_perform = _run_with_wipe_result(second_result, state_dir)
        final_perform.assert_not_called()
        assert final_report["wipe"]["reason"] == "all_eligible_devices_completed_for_commit"

        state = json.loads((state_dir / "wipe-state.json").read_text())
        assert state["commit_sha"] == "abc123"
        assert state["completed_devices"] == ["r1", "r2"]


def test_legacy_single_sha_state_is_treated_as_incomplete():
    with TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        state_file = state_dir / "wipe-state.json"
        state_file.write_text(json.dumps({"last_completed_sha": "abc123"}))
        with patch.object(reconciler, "WIPE_STATE_FILE", state_file):
            assert reconciler.load_wipe_state() == {
                "commit_sha": None,
                "completed_devices": [],
                "updated_at": None,
            }
