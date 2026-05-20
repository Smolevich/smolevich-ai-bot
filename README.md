# smolevich-ai-bot

[![Deploy to VDS](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml/badge.svg?branch=main)](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml)

A Telegram bot that gives you one chat interface in front of several LLM providers. Today it's pointed primarily at the **free tiers** of OpenRouter, Groq, Cerebras and NVIDIA — picking which provider/model to use is a `/provider` and `/models` away. The free-only focus is a deliberate cost choice for now, not a hard design constraint; nothing in the code prevents adding paid endpoints later, and that may well happen as free quotas tighten.

## What it does

- **Multi-provider chat** — switch providers and models on the fly with `/provider` and `/models`.
- **Code sandbox** — the `execute_bash` tool runs commands inside a per-user Podman container (`acpx-claude:latest`), with persistent workspace volumes per user.
- **Health checks** — text/code model checks run via `model-health-check` cron; audio checks (STT/TTS) run via separate `model-audio-check` cron; image/video discovery runs via separate `model-media-check` cron.
- **Free-models benchmark** — `model-benchmark` runs twice daily (07:00 and 19:00 UTC), picks the three most stable text models per provider for native chat tasks and the top-1 model for a claude tool-use task, scores them automatically and publishes the leaderboard and methodology to the site.
- **Stats** — `/top` and `/status` summarise usage, success rate and per-provider/per-model delivery counts.
- **Subscription gate** — first-time users are asked to subscribe to a Telegram channel before they get access; admin can approve/deny manually.
- **Multiple engine modes** — `/mode` toggles between native (direct OpenAI-compatible API call), Claude Code via ACP, opencode, and a "pi" experimental mode.
- **Version command** — `/version` (and the bottom of `/status`) reports the deployed build, stamped by CI as `YYYY-MM-DD-<short_sha>`.

## How it runs

A single Python file (`bot/vds-agent.py`, stdlib only) on a VDS as the `vds-agent` systemd unit. Configuration is via env vars (see [`.env.example`](.env.example)); API keys are read from env or from per-provider files under `/etc/socks-monitor/`.

CI (`.github/workflows/deploy.yml`) ships the bot, migrations, migration runner, health-check scripts, and the systemd unit on every push to `main` that touches `bot/**`. `deploy.sh` is a manual fallback that updates only the bot binary.

## Stored data

The bot stores only essential operational data needed for support and provider-compliance diagnostics:

- Telegram `user_id` and username
- user request/response history and selected provider/model
- technical request logs (timestamps, token usage, latency, error status)

This is used to identify which user request triggered a provider-side Terms/abuse block and to troubleshoot incidents.

Provider API keys used by the bot are the bot owner’s personal keys. Stored operational data is not sold and is not shared with third parties.

## Free-models benchmark

A twice-daily cron (07:00 and 19:00 UTC) picks the most stable free models per provider, runs GSM8K in `native` and `claude` tool-use modes, auto-scores the results, and publishes the leaderboard + methodology to the site. Scores are EWMA-weighted over a 48-hour window.

Full methodology, scoring formula, datasets and endpoints: [docs/benchmark.md](docs/benchmark.md).

## Docs

- [CLAUDE.md](CLAUDE.md) — high-level pointers (also linked as `AGENTS.md`).
- [docs/structure.md](docs/structure.md) — what's in the repo.
- [docs/config.md](docs/config.md) — env vars, paths on the VDS, server-side binaries.
- [docs/deploy.md](docs/deploy.md) — automatic and manual deploy, cron jobs, verification commands, server prerequisites.
- [docs/benchmark.md](docs/benchmark.md) — free-models benchmark methodology, scoring, locking, datasets and endpoints.
