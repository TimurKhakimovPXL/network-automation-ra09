"""Git operations used by the reconciler.

Responsibilities:
  - Pull latest from the current branch's configured upstream on each iteration
  - Report the current commit SHA (used by the wipe_now idempotency check)
  - Continue from local state after a transient pull failure

The controller only reads from Git; commits are created in an operator's clone.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


class GitError(Exception):
    """Raised for Git failures that require operator action."""


def pull() -> bool:
    """
    Pull the latest from origin. Returns True on success, False on transient
    failure (caller should continue with last good state).

    Network errors return False. Repository or branch errors raise GitError.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
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

    # Continue from local state for common network failures.
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
