# Structure

Everything bot-related lives in `bot/`:

- `bot/vds-agent.py` — the bot itself (~1250+ lines): Telegram long-poll/webhook, SQLite, providers (OpenRouter / Groq / Cerebras / NVIDIA), Podman sessions, STT/TTS handlers.
- `bot/agent/` — shared typed modules used by runtime (`config.py`, `text.py`).
- `bot/vds-agent.service` — systemd unit, runs `/usr/bin/python3 /usr/local/bin/vds-agent` as root.
- `bot/migrate.py` — yoyo migration runner (deploys to server as `migrate_bot_db`).
- `bot/model-health-check.py` — text/code model health cron job, writes provider/model availability into the bot DB.
- `bot/model-audio-check.py` — separate audio (STT/TTS) health cron job.
- `bot/migrations/` — yoyo migrations against SQLite (`/var/lib/telegram-llm-bot.db`).
- `bot/Containerfile.acpx-claude` — Podman image (`acpx-claude:latest`) used by the sandbox; built manually on the host.
- `bot/opx.sh` — opencode wrapper used by the bot's `/run` command (dev tool).
- `bot/test-claude-openrouter.sh` — local smoke test for Claude via OpenRouter.
- `bot/tasks/harness-test-prompts.md` — test prompts for the sandbox.

Top-level:

- `deploy.sh` — manual bot-only deploy via your private SSH host alias.
- `.github/workflows/deploy.yml` — auto-deploy on push to `main`.
- `.env.example` — every env var.

No external Python dependencies — stdlib only (yoyo is installed on the server).
