"""Shared NETCONF transaction handling for all configuration handlers.

IOS XE exposes either ``:writable-running`` or ``:candidate`` as the writable
configuration datastore.  Candidate mode replaces writable-running on affected
platforms, so handlers must select the target from the server capabilities
rather than assuming ``running`` is writable.
"""

from __future__ import annotations

from ncclient import manager


def _supports(session, capability: str) -> bool:
    return any(capability in str(item) for item in session.server_capabilities)


def edit_config(device_params: dict, payload: str) -> str:
    """Apply *payload* atomically and return the datastore mode used.

    Candidate failures are discarded before the datastore is unlocked.  On a
    writable-running server, rollback-on-error is requested when advertised.
    Exceptions are deliberately propagated so the calling handler can report
    ``edit_failed`` with its existing result contract.
    """
    with manager.connect(**device_params) as session:
        if _supports(session, ":candidate"):
            session.lock(target="candidate")
            primary_error: Exception | None = None
            try:
                session.edit_config(target="candidate", config=payload)
                if _supports(session, ":validate"):
                    session.validate(source="candidate")
                session.commit()
            except Exception as exc:
                primary_error = exc
                try:
                    session.discard_changes()
                except Exception:
                    # Preserve the configuration failure; cleanup errors must
                    # not hide the actionable root cause from handler reports.
                    pass
            finally:
                try:
                    session.unlock(target="candidate")
                except Exception:
                    if primary_error is None:
                        raise

            if primary_error is not None:
                raise primary_error
            return "candidate"

        kwargs = {
            "target": "running",
            "config": payload,
        }
        if _supports(session, ":rollback-on-error"):
            kwargs["error_option"] = "rollback-on-error"
        session.edit_config(**kwargs)
        return "running"


__all__ = ["edit_config"]
