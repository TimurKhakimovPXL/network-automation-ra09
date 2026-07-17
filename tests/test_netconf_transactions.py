"""Pure tests for the shared writable-running/candidate transaction helper."""

from unittest.mock import patch

from handlers import _netconf


class FakeSession:
    def __init__(self, capabilities, fail_edit=False, fail_unlock=False):
        self.server_capabilities = capabilities
        self.fail_edit = fail_edit
        self.fail_unlock = fail_unlock
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def lock(self, **kwargs):
        self.calls.append(("lock", kwargs))

    def edit_config(self, **kwargs):
        self.calls.append(("edit_config", kwargs))
        if self.fail_edit:
            raise RuntimeError("edit rejected")

    def validate(self, **kwargs):
        self.calls.append(("validate", kwargs))

    def commit(self, **kwargs):
        self.calls.append(("commit", kwargs))

    def discard_changes(self, **kwargs):
        self.calls.append(("discard_changes", kwargs))

    def unlock(self, **kwargs):
        self.calls.append(("unlock", kwargs))
        if self.fail_unlock:
            raise RuntimeError("unlock rejected")


def _apply(session):
    with patch.object(_netconf.manager, "connect", return_value=session):
        return _netconf.edit_config({"host": "router"}, "<config/>")


def test_candidate_transaction_locks_validates_commits_and_unlocks():
    session = FakeSession([":candidate", ":validate"])
    assert _apply(session) == "candidate"
    assert [name for name, _ in session.calls] == [
        "lock", "edit_config", "validate", "commit", "unlock"
    ]
    assert session.calls[1][1]["target"] == "candidate"


def test_candidate_failure_discards_before_unlocking():
    session = FakeSession([":candidate", ":validate"], fail_edit=True)
    try:
        _apply(session)
    except RuntimeError as exc:
        assert str(exc) == "edit rejected"
    else:
        raise AssertionError("candidate edit failure was not propagated")

    assert [name for name, _ in session.calls] == [
        "lock", "edit_config", "discard_changes", "unlock"
    ]


def test_candidate_commit_survives_unlock_failure_for_readback(caplog):
    session = FakeSession([":candidate", ":validate"], fail_unlock=True)

    assert _apply(session) == "candidate"
    assert [name for name, _ in session.calls] == [
        "lock", "edit_config", "validate", "commit", "unlock"
    ]
    assert "commit succeeded but unlock failed" in caplog.text


def test_writable_running_uses_rollback_on_error_when_supported():
    session = FakeSession([":writable-running", ":rollback-on-error"])
    assert _apply(session) == "running"
    assert [name for name, _ in session.calls] == ["edit_config"]
    assert session.calls[0][1] == {
        "target": "running",
        "config": "<config/>",
        "error_option": "rollback-on-error",
    }


def test_writable_running_omits_unsupported_error_option():
    session = FakeSession([":writable-running"])
    assert _apply(session) == "running"
    assert "error_option" not in session.calls[0][1]
