#!/usr/bin/env bash
# Test Claude Code with OpenRouter + free model locally
# Usage: ./test-claude-openrouter.sh "your prompt here"

set -euo pipefail

# OpenRouter key — same as on VPS
OPENROUTER_KEY_FILE="/etc/socks-monitor/.openrouter_key"
if [[ -f "$OPENROUTER_KEY_FILE" ]]; then
  OPENROUTER_KEY="$(cat "$OPENROUTER_KEY_FILE")"
elif [[ -f "$(dirname "$0")/../openrouter.key" ]]; then
  OPENROUTER_KEY="$(cat "$(dirname "$0")/../openrouter.key")"
else
  echo "Put your OpenRouter key in openrouter.key or set OPENROUTER_API_KEY env var" >&2
  exit 1
fi
OPENROUTER_KEY="${OPENROUTER_API_KEY:-$OPENROUTER_KEY}"

# Model to test — change as needed
MODEL="${MODEL:-openai/gpt-oss-120b:free}"

echo "=== Claude Code via OpenRouter ==="
echo "Model: $MODEL"
echo "==================================="

export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
export ANTHROPIC_API_KEY="$OPENROUTER_KEY"
export OPENROUTER_API_KEY="$OPENROUTER_KEY"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$MODEL"

claude
