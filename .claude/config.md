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

## Server-side binaries

| Path | Source in repo | Owner |
| --- | --- | --- |
| `/usr/local/bin/vds-agent` | `bot/vds-agent.py` | shipped by CI |
| `/usr/local/bin/migrate_bot_db` | `bot/migrate.py` | shipped by CI |
| `/usr/local/bin/model-health-check` | `bot/model-health-check.py` | shipped by CI |
| `/usr/local/bin/model-audio-check` | `bot/model-audio-check.py` | shipped by CI |
| `/etc/systemd/system/vds-agent.service` | `bot/vds-agent.service` | shipped by CI |
| `/usr/local/bin/migrations/*.py` | `bot/migrations/*.py` | shipped by CI |
| `/usr/local/bin/socks-notify` | — | separate, not in this repo |
