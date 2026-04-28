#!/bin/bash
# deploy_to_tftp.sh — Push ztp.py from Git to the TFTP server.
#
# Same single-source-of-truth principle as everything else: ztp.py lives in
# Git. The TFTP server's copy is a downstream render target. This script
# updates the render whenever the source changes.
#
# Run from the repo root:
#   ./labs/ztp/deploy_to_tftp.sh
#
# Optional Git hook integration: add to .git/hooks/post-commit to auto-deploy
# whenever ztp.py is committed:
#   git diff --name-only HEAD~1 HEAD | grep -q '^labs/ztp/ztp.py$' && \
#     ./labs/ztp/deploy_to_tftp.sh

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────

TFTP_HOST="${TFTP_HOST:-10.199.64.134}"
TFTP_USER="${TFTP_USER:-tftpadmin}"
TFTP_PATH="${TFTP_PATH:-/srv/tftp/ztp.py}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="${SCRIPT_DIR}/ztp.py"

# ─── Sanity checks ──────────────────────────────────────────────────────────

if [[ ! -f "${SOURCE_FILE}" ]]; then
    echo "ERROR: source file not found: ${SOURCE_FILE}" >&2
    exit 1
fi

if ! command -v scp >/dev/null 2>&1; then
    echo "ERROR: scp not in PATH" >&2
    exit 1
fi

# ─── Deploy ─────────────────────────────────────────────────────────────────

echo "Deploying ${SOURCE_FILE} → ${TFTP_USER}@${TFTP_HOST}:${TFTP_PATH}"

scp -o BatchMode=yes -o ConnectTimeout=10 \
    "${SOURCE_FILE}" \
    "${TFTP_USER}@${TFTP_HOST}:${TFTP_PATH}"

ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${TFTP_USER}@${TFTP_HOST}" \
    "chmod 644 '${TFTP_PATH}'"

echo "Deployed. Devices fetching ${TFTP_PATH} via DHCP option 67 will get this version on next ZTP."
