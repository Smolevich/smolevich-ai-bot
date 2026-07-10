# Configuration

All bot settings come from env vars (see `.env.example`). API keys are resolved in this order: env var → on-disk file.

Runtime secrets (provider keys, `HF_TOKEN`, `MODEL_LEADERBOARD_TOKEN`, proxy URL) are stored in Vault at `secret/smolevich-ai-bot` and written by the deploy into both `/opt/smolevich-ai-bot/.env` and the `/etc/socks-monitor/.<provider>_key` files. See [vault.md](vault.md).

## VDS paths

- Config: `/etc/socks-monitor/config.json`
- Admin ID: `/etc/socks-monitor/.admin_id`
- Provider keys: `/etc/socks-monitor/.<provider>_key` (openrouter, groq, cerebras, nvidia, hf)
- DB: `/var/lib/telegram-llm-bot.db` — shared between the bot, all health-check crons, and the benchmark.
- Sandbox sessions: `/var/lib/smolevich-ai-bot/sessions`
- Migrations dir on server: `/usr/local/bin/migrations/`
- Health-check log: `/var/log/model-health-check.log`
- Audio-check log: `/var/log/model-audio-check.log`
- Media-check log: `/var/log/model-media-check.log`
- Benchmark log: `/var/log/model-benchmark.log`

Each bot path can be overridden via the matching `BOT_*` env var.

## Environment files (on VDS)

- `/opt/smolevich-ai-bot/.env` — central env file managed by CI/CD.
- `/etc/socks-monitor/smolevich-ai-bot.env` — node-specific server configuration.

When sourcing these in shell or cron scripts, use `set -a` before and `set +a` after sourcing so all variables are exported to subprocesses.

## Env variable directory

| Env Variable | Purpose | Default |
| --- | --- | --- |
| `BOT_CONFIG` | Path to JSON config | `/etc/socks-monitor/config.json` |
| `BOT_ADMIN_FILE` | Authorized Admin Telegram IDs | `/etc/socks-monitor/.admin_id` |
| `BOT_DB_FILE` | Bot SQLite database file | `/var/lib/telegram-llm-bot.db` |
| `BOT_SESSIONS_ROOT` | Podman agent workspace directory | `/var/lib/smolevich-ai-bot/sessions` |
| `BOT_TUNNEL_URL` | Public webhook URL | `https://ai.smolevich.com` |
| `BOT_REQUIRED_CHANNEL` | Required channel for subscription gate | `@naturalists_notes_st` |
| `BOT_PROXY_URL` | Outbound HTTP/SOCKS proxy URL | `""` |
| `BOT_PROXY_DISABLED` | Global proxy override (non-empty value disables) | `""` |
| `BOT_ACPX_LOCK_PATH` | Shared acpx flock file (bot ↔ benchmark) | `/var/lock/acpx.lock` |
| `BOT_ACPX_LOCK_WAIT` | Seconds the chat waits for the lock before responding "agent busy" | `30` |
| `BOT_ACPX_ACTIVE_PATH` | File touched while a user chat is active | `/run/smolevich-ai-bot-active` |

Benchmark-specific variables are listed in [benchmark.md](benchmark.md).

## Server-side binaries

| Path | Source in repo |
| --- | --- |
| `/usr/local/bin/smolevich-ai-bot` | `bot/smolevich-ai-bot.py` |
| `/usr/local/bin/migrate_bot_db` | `bot/migrate.py` |
| `/usr/local/bin/model-health-check` | `bot/model-health-check.py` |
| `/usr/local/bin/model-audio-check` | `bot/model-audio-check.py` |
| `/usr/local/bin/model-media-check` | `bot/model-media-check.py` |
| `/usr/local/bin/model-benchmark` | `bot/model-benchmark.py` |
| `/usr/local/bin/refresh-benchmark-datasets` | `bot/scripts/refresh-benchmark-datasets.py` |
| `/etc/socks-monitor/benchmark-tasks.json` | `bot/benchmark-tasks.json` |
| `/etc/socks-monitor/benchmark-tasks.md` | `bot/benchmark-tasks.md` |
| `/etc/socks-monitor/benchmark-datasets/*.json` | `bot/benchmark-datasets/*.json` |
| `/etc/systemd/system/smolevich-ai-bot.service` | `bot/smolevich-ai-bot.service` |
| `/etc/cron.d/model-checks` | `bot/cron.d/model-checks` |
| `/opt/smolevich-ai-bot/.env` | assembled in `deploy.yml` |
| `/usr/local/bin/migrations/*.py` | `bot/migrations/*.py` |
| `/usr/local/bin/agent/*.py` | `bot/agent/*.py` |
| `/usr/local/bin/socks-notify` | — (separate repo) |
