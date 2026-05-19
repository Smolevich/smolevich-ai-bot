# smolevich-ai-bot

[![Deploy to VDS](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml/badge.svg?branch=main)](https://github.com/Smolevich/smolevich-ai-bot/actions/workflows/deploy.yml)

A Telegram bot that gives you one chat interface in front of several LLM providers. Today it's pointed primarily at the **free tiers** of OpenRouter, Groq, Cerebras and NVIDIA — picking which provider/model to use is a `/provider` and `/models` away. The free-only focus is a deliberate cost choice for now, not a hard design constraint; nothing in the code prevents adding paid endpoints later, and that may well happen as free quotas tighten.

## What it does

- **Multi-provider chat** — switch providers and models on the fly with `/provider` and `/models`.
- **Code sandbox** — the `execute_bash` tool runs commands inside a per-user Podman container (`acpx-claude:latest`), with persistent workspace volumes per user.
- **Health checks** — text/code model checks run via `model-health-check` cron; audio checks (STT/TTS) run via separate `model-audio-check` cron; image/video discovery runs via separate `model-media-check` cron.
- **Daily free-models benchmark** — `model-benchmark` runs at 04:17 UTC, picks the three most stable text models per provider for native chat tasks and the top-1 model for a claude tool-use task, scores them automatically and publishes the leaderboard and methodology to the site.
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

Daily cron (`/etc/cron.d/model-checks`, 04:17 UTC) runs the full pipeline:
`model-benchmark run → leaderboard --publish → leaderboard --publish-tasks → purge`.

### Methodology

- Three most stable text models per provider (last 7 days, `success_rate ≥ 0.9`, `checks ≥ 6`, latency ≤ 8 s) are graded on a **native** chat task. The top-1 of those three additionally runs a **claude** tool-use task inside the same podman sandbox the bot uses for user chats.
- Tasks live in [`bot/benchmark-tasks.json`](bot/benchmark-tasks.json), human-readable methodology in [`bot/benchmark-tasks.md`](bot/benchmark-tasks.md).
- Auto-scoring only (no LLM judge): `bot/agent/benchmark_scoring.py` extracts numeric answers via regex chain (`####`, `\boxed{}`, `ANSWER:`, last number) and compares with `abs(diff) < 1e-3`. Claude tool-use scoring gives 0.5 for a non-empty `scratch.md` (proof of tool use) + 0.5 for a correct `answer.txt`.
- One acpx/podman container runs on the host at a time — a global flock (`bot/agent/acpx_lock.py`) is shared between the bot and the benchmark, so the benchmark never collides with a live user chat.
- Cron `BOT_BENCHMARK_DISABLED=1` env var is a kill switch; results retention is 30 days, jobs 7 days, purged daily.

### Datasets

Sample fixtures live in `bot/benchmark-datasets/`:

| File | Source | License | Sample size |
|------|--------|---------|-------------|
| `gsm8k.json` | [openai/gsm8k](https://huggingface.co/datasets/openai/gsm8k) (config `main`, split `test`) | MIT | 15 |

Schema of each file:

```json
{
  "dataset": "openai/gsm8k",
  "config": "main",
  "split": "test",
  "license": "MIT",
  "source_url": "https://huggingface.co/datasets/openai/gsm8k",
  "fetched_at": "2026-05-19T11:00:00Z",
  "seed": 20260519,
  "samples": [
    {
      "id": "gsm8k_test_42",
      "source": "openai/gsm8k:test",
      "question": "...",
      "ground_truth": 42,
      "raw_answer": "step-by-step ... #### 42"
    }
  ]
}
```

Refresh manually:

```sh
python3 bot/scripts/refresh-benchmark-datasets.py --out bot/benchmark-datasets
```

Default seed is the current UTC date (`YYYYMMDD`) — re-running on the same day reproduces the same items. Use `--seed N` for a custom seed.

### Endpoints

- `GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models` — leaderboard payload (with optional `tasks` and per-model `task_results`).
- `GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/benchmark-tasks` — open methodology (`tasks` + `methodology_md`).

GPQA-Diamond, MMLU-Pro and other heavier sets can be added later without DB
migrations — extend `benchmark-tasks.json` and drop a new dataset JSON into
`bot/benchmark-datasets/`.

## Docs

- [CLAUDE.md](CLAUDE.md) — high-level pointers (also linked as `AGENTS.md`).
- [.claude/structure.md](.claude/structure.md) — what's in the repo.
- [.claude/config.md](.claude/config.md) — env vars, paths on the VDS, server-side binaries.
- [.claude/deploy.md](.claude/deploy.md) — automatic and manual deploy, cron jobs, verification commands, benchmark prerequisites.
