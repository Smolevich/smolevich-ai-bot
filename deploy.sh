#!/usr/bin/env bash
# Manual deploy: ./deploy.sh
# Updates only the bot binary. Use the GitHub Actions workflow
# (push to main) to also ship migrations, the migrate runner,
# the health-check script, and the systemd unit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="vscale"
REMOTE_PATH="/usr/local/bin/vds-agent"

echo "Deploying bot/vds-agent.py -> ${REMOTE}:${REMOTE_PATH}"
scp "${SCRIPT_DIR}/bot/vds-agent.py" "${REMOTE}:${REMOTE_PATH}"
echo "Deploying bot/agent/ -> ${REMOTE}:/usr/local/bin/agent/"
ssh "${REMOTE}" "sudo mkdir -p /usr/local/bin/agent && sudo chown \$USER /usr/local/bin/agent"
scp -r "${SCRIPT_DIR}/bot/agent/"* "${REMOTE}:/usr/local/bin/agent/"
ssh "${REMOTE}" "sudo systemctl restart vds-agent && sleep 1 && sudo systemctl is-active vds-agent"
echo "Done."
