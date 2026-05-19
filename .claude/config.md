# Configuration

All bot settings come from env vars (see `.env.example`). API keys are resolved in this order: env var → on-disk file.

## VDS paths

- Config: `/etc/socks-monitor/config.json`
- Admin ID: `/etc/socks-monitor/.admin_id`
- Provider keys: `/etc/socks-monitor/.<provider>_key` (openrouter, groq, cerebras, nvidia)
- DB: `/var/lib/telegram-llm-bot.db` — shared between the bot, text health-check cron, and audio health-check cron
- Sandbox sessions: `/var/lib/vds-agent/sessions`
- Migrations dir on server: `/usr/local/bin/migrations/`
- Health-check log: `/var/log/model-health-check.log`
- Audio-check log (recommended): `/var/log/model-audio-check.log`

Each bot path can be overridden via the matching `BOT_*` env var.

## Other

- `BOT_TUNNEL_URL` — public webhook URL (`https://ai.smolevich.com`).
- `BOT_REQUIRED_CHANNEL` — Telegram channel used for the subscription gate.
- `BOT_PROXY_URL` — optional SOCKS proxy override for outbound provider traffic; on current Hetzner setup it is usually not needed and can stay empty.
- `BOT_PROXY_DISABLED` — kill switch: set to any non-empty value (e.g. `1`) to ignore `BOT_PROXY_URL` everywhere (bot, model-check scripts, `opx.sh`). Pipes through deploy.yml as a GH secret.

## Benchmark

The daily free-models benchmark (`bot/model-benchmark.py`, cron 04:17 UTC) uses these env vars (all written to `/opt/smolevich-ai-bot/.env` by `deploy.yml`):

- `MODEL_LEADERBOARD_TOKEN` — bearer token for `notes-share` worker; if empty, the publish steps are skipped.
- `BOT_BENCHMARK_DISABLED=1` — kill switch for enqueue/work/purge.
- `BOT_ACPX_LOCK_PATH` — path to the shared acpx flock file (default `/var/lock/acpx.lock`); guards against two acpx containers running at once.
- `BOT_ACPX_LOCK_WAIT` — seconds the user-facing chat will wait for the lock before responding "agent busy" (default 30).
- `BOT_ACPX_ACTIVE_PATH` — touched by `ask_via_acpx` so the benchmark can defer its claude tick (default `/run/vds-agent-active`).
- `BOT_BENCHMARK_TASKS` — path to `benchmark-tasks.json` (default `/etc/socks-monitor/benchmark-tasks.json`).
- `BOT_BENCHMARK_METHODOLOGY` — path to `benchmark-tasks.md` (default `/etc/socks-monitor/benchmark-tasks.md`).
- `BOT_BENCHMARK_DATASETS` — directory with dataset samples (default `/etc/socks-monitor/benchmark-datasets`).
- `BOT_BENCHMARK_ROOT` — workspaces for claude benchmark runs (default `/var/lib/vds-agent/sessions/benchmarks`); auto-cleaned on success.
- `BOT_BENCHMARK_KEEP_FAILED=1` — keep failed claude workspaces around for debugging.

## Server-side binaries

| Path | Source in repo | Owner |
| --- | --- | --- |
| `/usr/local/bin/vds-agent` | `bot/vds-agent.py` | shipped by CI |
| `/usr/local/bin/migrate_bot_db` | `bot/migrate.py` | shipped by CI |
| `/usr/local/bin/model-health-check` | `bot/model-health-check.py` | shipped by CI |
| `/usr/local/bin/model-audio-check` | `bot/model-audio-check.py` | shipped by CI |
| `/usr/local/bin/model-media-check` | `bot/model-media-check.py` | shipped by CI |
| `/usr/local/bin/model-benchmark` | `bot/model-benchmark.py` | shipped by CI |
| `/usr/local/bin/refresh-benchmark-datasets` | `bot/scripts/refresh-benchmark-datasets.py` | shipped by CI |
| `/etc/socks-monitor/benchmark-tasks.json` | `bot/benchmark-tasks.json` | shipped by CI |
| `/etc/socks-monitor/benchmark-tasks.md` | `bot/benchmark-tasks.md` | shipped by CI |
| `/etc/socks-monitor/benchmark-datasets/*.json` | `bot/benchmark-datasets/*.json` | shipped by CI |
| `/etc/systemd/system/vds-agent.service` | `bot/vds-agent.service` | shipped by CI |
| `/etc/cron.d/model-checks` | `bot/cron.d/model-checks` | shipped by CI |
| `/opt/smolevich-ai-bot/.env` | (assembled in deploy.yml) | shipped by CI |
| `/usr/local/bin/migrations/*.py` | `bot/migrations/*.py` | shipped by CI |
| `/usr/local/bin/socks-notify` | — | separate, not in this repo |
