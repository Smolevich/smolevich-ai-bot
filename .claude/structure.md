# Structure

Everything bot-related lives in `bot/`:

- `bot/vds-agent.py` — the bot itself (~1250+ lines): Telegram long-poll/webhook, SQLite, providers (OpenRouter / Groq / Cerebras / NVIDIA / Hugging Face), Podman sessions, STT/TTS handlers. Holds the global acpx lock for user sessions.
- `bot/agent/` — shared typed modules used by runtime: `config.py`, `text.py`, `db.py`, `entities.py`, `provider_api.py`, `telegram_api.py`, plus `acpx_lock.py` (cross-process flock between bot and benchmark) and `benchmark_scoring.py` (auto-scorers for GSM8K).
- `bot/vds-agent.service` — systemd unit, two EnvironmentFiles (`/etc/socks-monitor/vds-agent.env` + `/opt/smolevich-ai-bot/.env`).
- `bot/migrate.py` — yoyo migration runner (deploys to server as `migrate_bot_db`).
- `bot/model-health-check.py` — text/code model health cron job, writes provider/model availability into the bot DB.
- `bot/model-audio-check.py` — separate audio (STT/TTS) health cron job.
- `bot/model-media-check.py` — image/video model discovery cron job.
- `bot/model-benchmark.py` — daily free-models benchmark queue (subcommands `enqueue`, `work`, `run`, `leaderboard`, `tasks`, `refresh-datasets`, `purge`); writes to `model_benchmark_jobs` / `model_benchmark_results` in the bot DB.
- `bot/benchmark-tasks.json` — open methodology (task list with kind / dataset / license / source URL).
- `bot/benchmark-tasks.md` — human-readable methodology, published to the site.
- `bot/benchmark-datasets/` — fixed JSON samples from public HF datasets (currently GSM8K test, 15 random rows).
- `bot/scripts/refresh-benchmark-datasets.py` — pulls fresh samples from `datasets-server.huggingface.co` (stdlib only).
- `bot/cron.d/model-checks` — cron entries deployed to `/etc/cron.d/model-checks`. Includes the daily benchmark pipeline at 04:17 UTC.
- `bot/migrations/` — yoyo migrations against SQLite (`/var/lib/telegram-llm-bot.db`).
- `bot/Containerfile.acpx-claude` — Podman image (`acpx-claude:latest`) used by both the user sandbox and the benchmark; built on the host when its hash changes.
- `bot/opx.sh` — opencode wrapper used by the bot's `/run` command (dev tool).
- `bot/test-claude-openrouter.sh` — local smoke test for Claude via OpenRouter.
- `bot/tasks/harness-test-prompts.md` — test prompts for the sandbox.

Top-level:

- `deploy.sh` — manual bot-only deploy via your private SSH host alias.
- `.github/workflows/deploy.yml` — auto-deploy on push to `main`.
- `.env.example` — every env var.

No external Python dependencies — stdlib only (yoyo is installed on the server).
