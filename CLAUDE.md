# smolevich-ai-bot

Telegram bot using free-tier LLM providers with a Podman sandbox. Runs on a VDS as the `vds-agent` systemd unit. Stdlib-only Python — no external runtime deps.

## Where things live

- `bot/vds-agent.py` — Telegram polling/webhook entrypoint shim.
- `bot/agent/` — typed runtime modules (`config.py`, `text.py`, `db.py`, `entities.py`, `provider_api.py`, `telegram_api.py`, `acpx_lock.py`, `benchmark_scoring.py`).
- `bot/model-{health,audio,media}-check.py` — provider/model probes (cron).
- `bot/model-benchmark.py` — daily free-models benchmark (cron, 04:17 UTC).
- `bot/migrate.py` + `bot/migrations/` — yoyo migrations against the SQLite DB.

Full file map: [docs/structure.md](docs/structure.md).

## Engine modes

- `native` — direct chat completions from LLM providers.
- `claude` — sandboxed agentic execution inside a Podman container (`acpx-claude:latest`, built from `bot/Containerfile.acpx-claude`).
- `pi` — Private Interpreter (sandboxed Python).
- `opencode` — sandboxed shell via `bot/opx.sh`.

The bot and the benchmark share a flock at `/var/lock/acpx.lock` so only one acpx container runs at a time.

## VDS

- SSH alias: `hetzner-bot`. Systemd unit: `vds-agent`.
- Shared SQLite DB: `/var/lib/telegram-llm-bot.db` (tables: `model_health`, `model_health_log`, `model_benchmark_jobs`, `model_benchmark_results`, plus bot state).
- Per-session workspaces: `/var/lib/vds-agent/sessions`.
- Env: `/opt/smolevich-ai-bot/.env` (CI-assembled) + `/etc/socks-monitor/vds-agent.env`. Provider keys: `/etc/socks-monitor/.<provider>_key`.

Paths, env vars and the binary mapping: [docs/config.md](docs/config.md).

## Deploy

- Auto: push to `main` touching `bot/**` runs `.github/workflows/deploy.yml` (Tailscale → SSH → migrate → restart).
- Manual: `./deploy.sh` (bot binary only).
- Always verify with `gh run list --branch main --limit 5` — `git push` is not a deploy.

Full deploy / cron / server-prep runbook: [docs/deploy.md](docs/deploy.md).

## Benchmark

Daily cron (`04:17 UTC`) selects the top stable free models per provider, runs GSM8K in native (all three) and claude tool-use (top-1), auto-scores, and publishes the leaderboard. Scores are EWMA-weighted over a 48-hour window.

Methodology, scoring, locking, datasets and endpoints: [docs/benchmark.md](docs/benchmark.md).

## Conventions

- When sourcing env files in shell/cron scripts, wrap with `set -a` … `set +a`.
- Never write to `/tmp`; scratch files go to `.scratch/` in the repo.
- Bot server alias is `hetzner-bot` (not `vscale`).
