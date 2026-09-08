# smolevich-ai-bot

Telegram bot using free-tier LLM providers with a Podman sandbox. Runs on a VDS as the `smolevich-ai-bot` systemd unit. Stdlib-only Python — no external runtime deps.

## Where things live

- `bot/smolevich-ai-bot.py` — Telegram polling/webhook entrypoint shim.
- `bot/agent/` — typed runtime modules (`config.py`, `text.py`, `db.py`, `entities.py`, `provider_api.py`, `telegram_api.py`, `acpx_lock.py`, `benchmark_scoring.py`).
- `bot/model-{health,audio,media}-check.py` — provider/model probes (cron).
- `bot/model-benchmark.py` — free-models benchmark, run by a Temporal schedule
  (`smolevich-bench-2x-daily`, 07:00/19:00 UTC) via `bot/temporal_jobs/`.
- `bot/migrate.py` + `bot/migrations/` — yoyo migrations against the SQLite DB.

Full file map: [docs/structure.md](docs/structure.md).

## Engine modes

- `native` — direct chat completions from LLM providers. The default for everyone, including the admin, and what a reset returns to.
- `claude` — sandboxed agentic execution inside a Podman container (`acpx-claude:latest`, built from `bot/Containerfile.acpx-claude`).
- `pi` — Private Interpreter (sandboxed Python).
- `opencode` — sandboxed shell via `bot/opx.sh`.

Rules that produced themselves the hard way (08.09.2026):

- **Every answer goes through `answer_with_fallback`.** It picks the engine, runs the sandbox, and on any failure at all answers natively with the same history. A harness never talks to a person directly.
- **A CLI's own error text never reaches the chat.** `ask_via_acpx` returns `None` on failure; the reason goes to the log and to `request_log`.
- **claude mode runs only on a model from `model_routing.CLAUDE_CLI_MODELS`,** and only while the health probe still reaches it. The board ranks models for chat completions; claude-code speaks the Anthropic protocol and cannot talk to most of them.
- **The model is passed as `--model`, never through `ANTHROPIC_DEFAULT_*_MODEL` alone.** claude-code 2.1.138 resolves a 1M-context model from the env vars to `<id>[1m]`, which no provider has.
- **The bot's system prompt goes in with `--append-system-prompt`,** or the sandbox answers a greeting with "not a software engineering task".
- **The system prompt is built per candidate from what that candidate got.** `native_answer` calls `build_system_prompt(is_admin, has_tools)` inside the fallback loop. Promising a shell to a model that was sent no tool schema is what made models type tool calls out as text and invent command output. Note `use_tools` is currently False for every text model: the health cron writes `capabilities` as `capabilities_for_category(category)`, which returns the bare string `"text"`.
- **A tool call typed out as text is not an answer.** `agent/tool_calls.py` knows the shapes; a reply that is only such a call (or a call plus a run-up under `PSEUDO_CALL_LEADIN_MAX`) is a failed attempt and the next model gets the question. The model that did it loses `model_health.supports_tools`.

The bot and the benchmark share a flock at `/var/lock/acpx.lock` so only one acpx container runs at a time.

## VDS

- SSH alias: `hetzner-bot`. Systemd unit: `smolevich-ai-bot`.
- Shared SQLite DB: `/var/lib/telegram-llm-bot.db` (tables: `model_health`, `model_health_log`, `model_benchmark_jobs`, `model_benchmark_results`, plus bot state).
- Per-session workspaces: `/var/lib/smolevich-ai-bot/sessions`.
- Env: `/opt/smolevich-ai-bot/.env` (CI-assembled from Vault) + `/etc/socks-monitor/smolevich-ai-bot.env`. Provider keys: `/etc/socks-monitor/.<provider>_key`.
- Secrets live in Vault at `secret/smolevich-ai-bot`; deploy reads them via `VAULT_DEPLOY_TOKEN`. See [docs/vault.md](docs/vault.md).

Paths, env vars and the binary mapping: [docs/config.md](docs/config.md).

## Menu

Screens, wording rules and the traps that produced them: [docs/menu.md](docs/menu.md).
Short version: the bot picks the model, the person just asks; no screen may show a model id, a provider name or a benchmark score; every submenu ends with the same «← Назад» row.

## Deploy

- Auto: push to `main` touching `bot/**` runs `.github/workflows/deploy.yml` (Tailscale → SSH → migrate → restart).
- Manual: `./deploy.sh` (bot binary only).
- Always verify with `gh run list --branch main --limit 5` — `git push` is not a deploy.

Full deploy / cron / server-prep runbook: [docs/deploy.md](docs/deploy.md).

## Benchmark

A twice-daily cron (`07:00` and `19:00` UTC) selects the top stable free models per provider, runs GSM8K in native (all three) and claude tool-use (top-1), auto-scores, and publishes the leaderboard. Scores are EWMA-weighted over a 48-hour window.

Methodology, scoring, locking, datasets and endpoints: [docs/benchmark.md](docs/benchmark.md).

## Conventions

- When sourcing env files in shell/cron scripts, wrap with `set -a` … `set +a`.
- Never write to `/tmp`; scratch files go to `.scratch/` in the repo.
- Bot server alias is `hetzner-bot` (not `vscale`).
