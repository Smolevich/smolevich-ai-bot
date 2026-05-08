# smolevich-ai-bot

[![Deploy to VDS](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml/badge.svg?branch=main)](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml)

A Telegram bot that gives you one chat interface in front of several LLM providers. Today it's pointed primarily at the **free tiers** of OpenRouter, Groq, Cerebras and NVIDIA — picking which provider/model to use is a `/provider` and `/models` away. The free-only focus is a deliberate cost choice for now, not a hard design constraint; nothing in the code prevents adding paid endpoints later, and that may well happen as free quotas tighten.

## What it does

- **Multi-provider chat** — switch providers and models on the fly with `/provider` and `/models`.
- **Code sandbox** — the `execute_bash` tool runs commands inside a per-user Podman container (`acpx-claude:latest`), with persistent workspace volumes per user.
- **Health checks** — a separate cron script pings every model every 5 minutes and writes results into the bot's SQLite DB, so `/models` can show which ones are currently up.
- **Stats** — `/top` and `/status` summarise usage, success rate and per-provider/per-model delivery counts.
- **Subscription gate** — first-time users are asked to subscribe to a Telegram channel before they get access; admin can approve/deny manually.
- **Multiple engine modes** — `/mode` toggles between native (direct OpenAI-compatible API call), Claude Code via ACP, opencode, and a "pi" experimental mode.
- **Version command** — `/version` (and the bottom of `/status`) reports the deployed build, stamped by CI as `YYYY-MM-DD-<short_sha>`.

## How it runs

A single Python file (`bot/vds-agent.py`, stdlib only) on a VDS as the `vds-agent` systemd unit. Configuration is via env vars (see [`.env.example`](.env.example)); API keys are read from env or from per-provider files under `/etc/socks-monitor/`.

CI (`.github/workflows/deploy.yml`) ships the bot, migrations, the migration runner, the health-check script and the systemd unit on every push to `main` that touches `bot/**`. `deploy.sh` is a manual fallback that updates only the bot binary.

## Docs

- [CLAUDE.md](CLAUDE.md) — high-level pointers (also linked as `AGENTS.md`).
- [.claude/structure.md](.claude/structure.md) — what's in the repo.
- [.claude/config.md](.claude/config.md) — env vars, paths on the VDS, server-side binaries.
- [.claude/deploy.md](.claude/deploy.md) — automatic and manual deploy, cron jobs, verification commands.
