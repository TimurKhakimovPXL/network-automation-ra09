"""Shared NETCONF transaction handling for all configuration handlers.

IOS XE exposes either ``:writable-running`` or ``:candidate`` as the writable
configuration datastore.  Candidate mode replaces writable-running on affected
platforms, so handlers must select the target from the server capabilities
rather than assuming ``running`` is writable.
"""

from __future__ import annotations

import logging

from ncclient import manager


log = logging.getLogger(__name__)


def _supports(session, capability: str) -> bool:
    return any(capability in str(item) for item in session.server_capabilities)


def edit_config(device_params: dict, payload: str) -> str:
    """Apply *payload* atomically and return the datastore mode used.

    Candidate failures are discarded before the datastore is unlocked.  On a
    writable-running server, rollback-on-error is requested when advertised.
    Configuration errors are re-raised for the handler to report as
    ``edit_failed``.
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
                    # Keep the original edit error if cleanup also fails.
                    pass
            finally:
                try:
                    session.unlock(target="candidate")
                except Exception as exc:
                    if primary_error is None:
                        # The commit is already active. Raising here would make
                        # the handler skip its RESTCONF verification and report
                        # an edit failure for configuration that may have
                        # landed. Closing the NETCONF session releases its lock.
                        log.warning(
                            "candidate commit succeeded but unlock failed; "
                            "closing the session to release the lock: %s",
                            exc,
                        )

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
