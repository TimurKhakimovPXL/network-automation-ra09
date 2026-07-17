"""Legacy IOS XE SSH retry decision tests."""

from unittest.mock import patch

import paramiko

from reconciler import reconciler
from reconciler.reconciler import _should_retry_legacy_ssh


def test_retries_ssh_rsa_signature_negotiation_failure():
    error = paramiko.SSHException(
        "Unable to agree on a pubkey algorithm for signing an ssh-rsa key"
    )

    assert _should_retry_legacy_ssh(error) is True


def test_retries_rsa_sha2_authentication_negotiation_failure():
    error = paramiko.AuthenticationException(
        "server rejected rsa-sha2-512 signature algorithm"
    )

    assert _should_retry_legacy_ssh(error) is True


def test_does_not_retry_generic_authentication_failure():
    error = paramiko.AuthenticationException("Authentication failed")

    assert _should_retry_legacy_ssh(error) is False


def test_does_not_retry_unrelated_ssh_failure():
    error = paramiko.SSHException("Error reading SSH protocol banner")

    assert _should_retry_legacy_ssh(error) is False


def test_does_not_retry_non_paramiko_exception_even_with_matching_text():
    error = TimeoutError("ssh-rsa negotiation timed out")

    assert _should_retry_legacy_ssh(error) is False


def test_successful_ssh_mode_is_recorded_in_wipe_report():
    device = {"name": "router", "mgmt_ip": "192.0.2.10"}

    with (
        patch.object(reconciler, "is_reachable", return_value=True),
        patch.object(
            reconciler,
            "_wipe_device_ssh",
            return_value=("success", "legacy-sha1"),
        ),
    ):
        report = reconciler.perform_wipe([device])

    assert report["details"] == [
        {"device": "router", "status": "wiped", "ssh_compat": "legacy-sha1"}
    ]
