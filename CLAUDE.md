# smolevich-ai-bot

Telegram bot using free-tier LLM providers with a Podman sandbox. Runs on a VDS as the `vds-agent` systemd unit. Single-file codebase (`bot/vds-agent.py`), stdlib only.

Details live in `.claude/`:

- [structure.md](.claude/structure.md) — what's where
- [config.md](.claude/config.md) — env vars and VDS paths
- [deploy.md](.claude/deploy.md) — how to ship and verify
