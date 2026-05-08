#!/usr/bin/env bash
set -euo pipefail

SESSION=""
PROVIDER="openrouter"
TASK=""
MODEL=""
WORKDIR="/var/lib/vds-agent/sessions"
PROXY_URL="http://REDACTED-PROXY"
OPENROUTER_KEY_FILE="/etc/socks-monitor/.openrouter_key"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session) SESSION="${2:-}"; shift 2 ;;
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --task) TASK="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$SESSION" || -z "$TASK" ]]; then
  echo "Usage: opx --session <id> --provider <openrouter|groq|cerebras|nvidia> --task <text> [--model <slug>]" >&2
  exit 2
fi

if ! command -v opencode >/dev/null 2>&1; then
  echo "opx: opencode binary not found on server" >&2
  exit 127
fi

if [[ ! -f "$OPENROUTER_KEY_FILE" && -f "/etc/socks-monitor/config.json" ]]; then
  DETECTED_KEY_FILE="$(python3 - <<'PY'
import json
from pathlib import Path
cfg = Path('/etc/socks-monitor/config.json')
try:
    data = json.loads(cfg.read_text())
    print(data.get('openrouter_key_file', ''))
except Exception:
    print('')
PY
)"
  if [[ -n "${DETECTED_KEY_FILE:-}" ]]; then
    OPENROUTER_KEY_FILE="$DETECTED_KEY_FILE"
  fi
fi

if [[ ! -f "$OPENROUTER_KEY_FILE" ]]; then
  echo "opx: OpenRouter key file not found: $OPENROUTER_KEY_FILE" >&2
  exit 1
fi

OPENROUTER_KEY="$(cat "$OPENROUTER_KEY_FILE")"

case "$PROVIDER" in
  openrouter|groq|cerebras|nvidia) ;;
  *) echo "Unsupported provider: $PROVIDER" >&2; exit 2 ;;
esac

: "${MODEL:=openai/gpt-oss-120b:free}"

mkdir -p "$WORKDIR/$SESSION"
chmod 0777 "$WORKDIR/$SESSION" || true

RAW_LOG="$WORKDIR/$SESSION/.opencode-last-raw.log"
ERR_LOG="$WORKDIR/$SESSION/.opencode-last-err.log"

set +e
RAW="$({
  OPENROUTER_API_KEY="$OPENROUTER_KEY" \
  OPENAI_API_KEY="$OPENROUTER_KEY" \
  ANTHROPIC_BASE_URL="https://openrouter.ai/api" \
  ANTHROPIC_AUTH_TOKEN="$OPENROUTER_KEY" \
  ANTHROPIC_API_KEY="" \
  OPENAI_BASE_URL="https://openrouter.ai/api/v1" \
  OPENAI_MODEL="$MODEL" \
  HTTP_PROXY="$PROXY_URL" \
  HTTPS_PROXY="$PROXY_URL" \
  ALL_PROXY="$PROXY_URL" \
  HOME="$WORKDIR/$SESSION/.opencode-home" \
  XDG_CONFIG_HOME="$WORKDIR/$SESSION/.opencode-config" \
  XDG_CACHE_HOME="$WORKDIR/$SESSION/.opencode-cache" \
  opencode run --model "$MODEL" -- "$TASK"
} 2>&1)"
RC=$?
set -e

mkdir -p "$WORKDIR/$SESSION/.opencode-home" "$WORKDIR/$SESSION/.opencode-config" "$WORKDIR/$SESSION/.opencode-cache" || true
printf "%s\n" "$RAW" > "$RAW_LOG" 2>/dev/null || true

if [[ $RC -ne 0 ]]; then
  echo "$RAW" >&2
  exit $RC
fi

OUT="$(printf "%s\n" "$RAW" | sed '/^[[:space:]]*$/d' | tail -n 1)"
if [[ -z "${OUT//[$'\t\r\n ']}" ]]; then
  echo "opx: empty output from opencode (model=$MODEL provider=$PROVIDER)" >&2
  exit 65
fi

echo "$OUT"
