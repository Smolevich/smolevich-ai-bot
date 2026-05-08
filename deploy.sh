#!/usr/bin/env bash
# Manual deploy: ./deploy.sh
# Copies bot to VDS and restarts the service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="vscale"
REMOTE_PATH="/usr/local/bin/vds-agent"

echo "Deploying bot/vds-agent.py -> ${REMOTE}:${REMOTE_PATH}"
scp "${SCRIPT_DIR}/bot/vds-agent.py" "${REMOTE}:${REMOTE_PATH}"
ssh "${REMOTE}" "sudo systemctl restart vds-agent && sleep 1 && sudo systemctl is-active vds-agent"
echo "Done."
