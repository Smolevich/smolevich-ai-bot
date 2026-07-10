#!/usr/bin/env bash
# Manual deploy: ./deploy.sh
# Updates only the bot binary. Use the GitHub Actions workflow
# (push to main) to also ship migrations, the migrate runner,
# the health-check script, and the systemd unit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="hetzner-bot"
REMOTE_PATH="/usr/local/bin/smolevich-ai-bot"

echo "Deploying bot/smolevich-ai-bot.py -> ${REMOTE}:${REMOTE_PATH}"
scp "${SCRIPT_DIR}/bot/smolevich-ai-bot.py" "${REMOTE}:${REMOTE_PATH}"
echo "Deploying bot/agent/ -> ${REMOTE}:/usr/local/bin/agent/"
ssh "${REMOTE}" "sudo mkdir -p /usr/local/bin/agent && sudo chown \$USER /usr/local/bin/agent"
scp -r "${SCRIPT_DIR}/bot/agent/"* "${REMOTE}:/usr/local/bin/agent/"
ssh "${REMOTE}" "sudo systemctl restart smolevich-ai-bot && sleep 1 && sudo systemctl is-active smolevich-ai-bot"
echo "Done."
