# Free-models benchmark

Daily auto-scored benchmark of free-tier LLMs the bot exposes. Results are published to the public leaderboard endpoint; the methodology page is published alongside.

The Russian-language version of the methodology that ships to the site lives in [`bot/benchmark-tasks.md`](../bot/benchmark-tasks.md). This document is the operational reference.

## Pipeline

Cron at **07:00 and 19:00 UTC** (`/etc/cron.d/model-checks`, twice daily) runs:

```
model-benchmark run --max-jobs 200
model-benchmark leaderboard --publish
model-benchmark leaderboard --publish-tasks
model-benchmark purge
```

1. **Selection** — for each provider, pick the three most stable text models from the last 7 days in `model_health_log` (`success_rate ≥ 0.9`, `checks ≥ 6`, mean latency ≤ 8 s).
2. **Native run** — all three are graded on the native chat task.
3. **Claude run** — the top-1 model per provider additionally runs a claude tool-use task inside the same Podman sandbox the bot uses for user chats. Sequential, gated by the global `acpx` flock (`/var/lock/acpx.lock`). If a user chat is active within the last 120 s (`/run/vds-agent-active`), the claude tick is deferred.
4. **Scoring** — auto-scorers in `bot/agent/benchmark_scoring.py`. No LLM judge.
5. **Publish** — results pushed to the leaderboard endpoint; methodology + tasks list pushed to the tasks endpoint.
6. **Purge** — jobs older than 7 days and results older than 30 days are removed.

## Tasks

Defined in [`bot/benchmark-tasks.json`](../bot/benchmark-tasks.json):

| Task | Mode | Dataset | License |
| --- | --- | --- | --- |
| `gsm8k_native` | native | [GSM8K test](https://huggingface.co/datasets/openai/gsm8k) (15 sampled rows in repo) | MIT |
| `gsm8k_tooluse` | claude | same GSM8K samples | MIT |

GSM8K (grade-school math, numeric answer) discriminates well in the free-tier band: 8B-class ≈ 50–60 % pass, 70B-class ≈ 85–93 %, weak models < 40 %.

## Scoring

### `gsm8k_native`

Prompt asks the model to end with `ANSWER: <number>`. Answer extraction tries, in order:

1. `####\s*(-?[\d.]+)` (GSM8K canonical format)
2. `\boxed{...}` (LaTeX)
3. `ANSWER:\s*(-?[\d.]+)` (what we asked for)
4. Last number in the response.

Compared to ground truth with `abs(diff) < 1e-3` → `score=1.0`, else `0.0`.

### `gsm8k_tooluse`

Same problem, but the model runs through `acpx` in a Podman sandbox and must:
- write reasoning into `scratch.md`,
- write the final number into `answer.txt`,
- reply with `DONE`.

Hard limits: wall-clock ≤ 120 s, ≤ 3 tool calls (anti tool-loop guard).

Score:
- `0.5` for non-empty `scratch.md` (proof of actual tool use)
- `0.5` for a correct number in `answer.txt`
- `ok=True` only at full `1.0`

## Overall ranking

For each model:

```
overall       = 0.45 * health_rate + 0.45 * bench_score + latency_bonus
bench_score   = 0.65 * native_score + 0.35 * claude_score   (claude_score = 0 if not run)
latency_bonus = max(0, min(0.1, (6000 - latency_ms) / 60000))
```

- All component scores are **EWMA-weighted** over a 48-hour window (half-life 12 h for both bench and health samples — `HALF_LIFE_BENCH_SEC`, `HALF_LIFE_HEALTH_SEC` in `bot/model-benchmark.py`).
- `health_rate` is the EWMA-weighted availability rate from `model_health_log` over the same window.
- `latency_bonus` is capped at ±0.1 — purely cosmetic; reliability and quality dominate.
- Models receive `status: "unstable"` if `health_rate < 0.75` or `bench_score < 0.6`.

## Datasets

Samples live in [`bot/benchmark-datasets/`](../bot/benchmark-datasets/).

| File | Source | License | Sample size |
| --- | --- | --- | --- |
| `gsm8k.json` | [openai/gsm8k](https://huggingface.co/datasets/openai/gsm8k) (config `main`, split `test`) | MIT | 15 |

Schema:

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

Refresh manually (stdlib only):

```sh
python3 bot/scripts/refresh-benchmark-datasets.py --out bot/benchmark-datasets
```

Default seed is the current UTC date (`YYYYMMDD`) — re-running on the same day reproduces the same items. Use `--seed N` for a custom seed.

GPQA-Diamond, MMLU-Pro and other heavier sets can be added later without DB migrations — extend `benchmark-tasks.json` and drop a new dataset JSON into `bot/benchmark-datasets/`.

## Concurrency & locking

- **`/var/lock/acpx.lock`** — shared flock between the bot's claude chat handler and the benchmark's claude task. Only one acpx/podman container runs at a time. `BOT_ACPX_LOCK_WAIT` controls how long a user chat waits before responding "agent busy" (default 30 s).
- **`/run/vds-agent-active`** — touched by `ask_via_acpx` whenever a user chat is active. The benchmark skips its claude tick if this file was touched within `ACTIVE_SKIP_WINDOW_SEC` (120 s).

## Endpoints

- `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models` — leaderboard payload (`source`, `updated_at`, `tasks`, `models[].task_results`).
- `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/benchmark-tasks` — open methodology (`tasks`, `methodology_md`).

Integration spec for the consuming site: `~/pet-projects/smolevich-main-site/.claude/benchmark-integration.md`.

## Env variables

Written to `/opt/smolevich-ai-bot/.env` by `deploy.yml`:

- `MODEL_LEADERBOARD_TOKEN` — bearer token for the `notes-share` worker; if empty, publish steps are skipped.
- `BOT_BENCHMARK_DISABLED=1` — kill switch for enqueue/work/purge.
- `BOT_BENCHMARK_TASKS` — path to `benchmark-tasks.json` (default `/etc/socks-monitor/benchmark-tasks.json`).
- `BOT_BENCHMARK_METHODOLOGY` — path to `benchmark-tasks.md` (default `/etc/socks-monitor/benchmark-tasks.md`).
- `BOT_BENCHMARK_DATASETS` — directory with dataset samples (default `/etc/socks-monitor/benchmark-datasets`).
- `BOT_BENCHMARK_ROOT` — workspaces for claude benchmark runs (default `/var/lib/vds-agent/sessions/benchmarks`); auto-cleaned on success.
- `BOT_BENCHMARK_KEEP_FAILED=1` — keep failed claude workspaces around for debugging.

## DB tables

- `model_benchmark_jobs` — work queue (provider, model, mode, task, sample, batch_id, attempts).
- `model_benchmark_results` — finished runs (provider, model, mode, task, sample, ok, score, latency_ms, prompt/completion/total tokens, batch_id, ts).

The leaderboard aggregator reads both tables plus `model_health_log` to compute the EWMA-weighted scores described above.

## Manual run

```sh
ssh hetzner-bot 'sudo nohup /bin/sh -c "set -a; . /opt/smolevich-ai-bot/.env 2>/dev/null || true; . /etc/socks-monitor/vds-agent.env 2>/dev/null || true; set +a; [ \"\$BOT_BENCHMARK_DISABLED\" = \"1\" ] && exit 0; /usr/local/bin/model-benchmark run --max-jobs 200; if [ -n \"\$MODEL_LEADERBOARD_TOKEN\" ]; then /usr/local/bin/model-benchmark leaderboard --publish; /usr/local/bin/model-benchmark leaderboard --publish-tasks; fi; /usr/local/bin/model-benchmark purge" >> /var/log/model-benchmark.log 2>&1 &'
```

Log: `/var/log/model-benchmark.log`.
