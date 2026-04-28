"""
git_watcher.py — Git interaction for the reconciler.

Responsibilities:
  - Pull latest from origin/main on each loop iteration
  - Report the current commit SHA (used by the wipe_now idempotency check)
  - Survive transient Git failures gracefully

The reconciler does not write to Git. All commits originate from the supervisor's
local clone. This is deliberate: keeping Git access read-only on the controller
reduces the risk surface and aligns with the architectural principle that the
controller is downstream of Git.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


class GitError(Exception):
    """Raised on unrecoverable Git failures. Recoverable failures (network blip
    on pull, etc.) are logged and ignored — the reconciler continues with the
    last successfully-pulled state."""


def pull() -> bool:
    """
    Pull the latest from origin. Returns True on success, False on transient
    failure (caller should continue with last good state).

    Does not raise on network errors — those are expected and recoverable.
    Raises GitError only on configuration-level problems (not a Git repo,
    branch detached, etc.) where the operator must intervene.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitError("git binary not found in PATH") from e
    except subprocess.TimeoutExpired:
        log.warning("git pull timed out after 30s; using last good state")
        return False

    if result.returncode == 0:
        if result.stdout.strip() and "Already up to date" not in result.stdout:
            log.info("git pull: %s", result.stdout.strip().splitlines()[0])
        return True

    stderr = result.stderr.strip()

    # Common transient failures that we can survive
    if any(
        marker in stderr.lower()
        for marker in [
            "could not resolve host",
            "connection refused",
            "connection timed out",
            "operation timed out",
            "temporarily unavailable",
        ]
    ):
        log.warning("git pull transient failure (continuing with last good state): %s", stderr)
        return False

    # Anything else is operator-action required
    raise GitError(f"git pull failed: {stderr}")


def current_commit_sha() -> Optional[str]:
    """Returns the SHA of HEAD, or None if it cannot be determined."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    sha = result.stdout.strip()
    return sha if sha else None
