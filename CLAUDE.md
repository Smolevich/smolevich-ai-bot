# smolevich-ai-bot

Telegram bot using free-tier LLM providers with a Podman sandbox. Runs on a VDS as the `vds-agent` systemd unit. Main runtime is structured into a entry shim (`bot/vds-agent.py`) and typed submodules (`bot/agent/`). Zero external runtime Python dependencies — uses only the Python standard library.

---

## 🏗️ Architecture & Core Concepts

### 1. Engine Modes
The bot supports four interaction/engine modes:
* **`native`**: Standard chat completions directly from LLM providers.
* **`claude`**: Sandboxed agentic execution inside a Podman container.
* **`pi`**: Private Interpreter mode (sandboxed Python code execution).
* **`opencode`**: Sandbox executing open-source / shell commands via `bot/opx.sh`.

### 2. Sandbox (acpx-claude)
Agentic runs (Claude mode) are executed inside a Podman container built from `bot/Containerfile.acpx-claude` (`acpx-claude:latest`).
* **Session Directory:** Each session gets a workspace directory inside `/var/lib/vds-agent/sessions`.
* **Lock Management:** To prevent multiple resource-heavy agent instances from running simultaneously, the system uses a cross-process lock (`/var/lock/acpx.lock`). This lock is shared between the user-facing bot agent and the background model-benchmark script.

### 3. Model Probes & Discovery
The bot monitors provider model availability using three cron-based checks:
* **`model-health-check`**: Probes text/code LLMs sequentially or in parallel.
* **`model-audio-check`**: Validates STT (Speech-to-Text) and TTS (Text-to-Speech) pipelines.
* **`model-media-check`**: Validates image/video generation capabilities.
Results are saved to a shared SQLite database (`/var/lib/telegram-llm-bot.db`) in `model_health` and `model_health_log` tables.

### 4. Leaderboard Benchmark Suite
A daily cron job (04:17 UTC) runs the benchmark suite (`bot/model-benchmark.py`):
1. **Selection:** Filters top stable models from the database.
2. **Execution:** Runs evaluation tasks (e.g., GSM8K math dataset) in `native` mode (parallel workers) and `claude` mode (sequential execution using the global `acpx` lock).
3. **Scoring:** Calculates model accuracy/scores using rule-based and LLM-assisted auto-scorers.
4. **Publishing:** Pushes the results payload to the leaderboard endpoint and the tasks methodology to the tasks endpoint.

---

## 📂 Codebase Layout

Everything bot-related lives in `bot/`:

* **Entrypoint & Runtime:**
  * `bot/vds-agent.py` — Telegram polling/webhook entrypoint, SQLite coordination, STT/TTS routing, and session lock handlers.
  * `bot/agent/` — Typed packages utilized by the runtime:
    * `config.py` — Path and provider defaults (OpenRouter, Groq, Cerebras, NVIDIA, HF).
    * `text.py` — Localization and user-facing copy.
    * `db.py` — Thread-safe SQLite connections (WAL mode, busy timeout).
    * `entities.py` — Structured data models (dataclasses/typed-dicts).
    * `provider_api.py` — Outbound LLM API calls and streaming formats.
    * `telegram_api.py` — Inbound webhook handling and outbound message/audio/document delivery.
    * `acpx_lock.py` — Cross-process locking wrapper (`flock`).
    * `benchmark_scoring.py` — Scorers for model benchmark runs.
* **Database & Migrations:**
  * `bot/migrate.py` — Yoyo migration runner (deployed as `/usr/local/bin/migrate_bot_db`).
  * `bot/migrations/` — SQLite migrations schema.
* **Health Checking & Cron:**
  * `bot/model-health-check.py` — Text/code LLM checker.
  * `bot/model-audio-check.py` — Audio pipeline checker.
  * `bot/model-media-check.py` — Image/video capability checker.
  * `bot/cron.d/model-checks` — Sourced configurations for `/etc/cron.d/model-checks`.
* **Benchmark & Sandbox Resources:**
  * `bot/model-benchmark.py` — Benchmark runner.
  * `bot/benchmark-tasks.json` — Tasks registry (GSM8K datasets, licenses, rules).
  * `bot/benchmark-tasks.md` — Methodology documentation published to the main site.
  * `bot/benchmark-datasets/` — JSON datasets containing evaluation prompts.
  * `bot/scripts/refresh-benchmark-datasets.py` — Utility script to update evaluation samples.
  * `bot/Containerfile.acpx-claude` — Container definition for sandboxed agent executions.
  * `bot/opx.sh` — OpenCode wrapper for terminal commands.

---

## ⚙️ Configuration & Environment Variables

All configuration is sourced from system environment variables, falling back to on-disk credential keys.

### 1. Environment Files (on VDS)
* `/opt/smolevich-ai-bot/.env` — Central env configuration managed by CI/CD.
* `/etc/socks-monitor/vds-agent.env` — Node-specific server configuration.
* *Note: When sourcing environment files in shell or cron scripts, you MUST use `set -a` before and `set +a` after sourcing to ensure all variables are correctly exported to subprocesses.*

### 2. Provider Keys (Fallback Paths)
On VDS, provider keys are stored in separate files:
* `/etc/socks-monitor/.openrouter_key`
* `/etc/socks-monitor/.groq_key`
* `/etc/socks-monitor/.cerebras_key`
* `/etc/socks-monitor/.nvidia_key`
* `/etc/socks-monitor/.hf_key`

### 3. Environment Variable Directory

| Env Variable | Purpose | Default |
| --- | --- | --- |
| `BOT_CONFIG` | Path to JSON config | `/etc/socks-monitor/config.json` |
| `BOT_ADMIN_FILE` | Authorized Admin Telegram IDs | `/etc/socks-monitor/.admin_id` |
| `BOT_DB_FILE` | Bot SQLite database file | `/var/lib/telegram-llm-bot.db` |
| `BOT_SESSIONS_ROOT` | Podman agent workspace directory | `/var/lib/vds-agent/sessions` |
| `BOT_TUNNEL_URL` | Webhook domain path | `https://ai.smolevich.com` |
| `BOT_REQUIRED_CHANNEL` | Required channel for subscription gate | `@naturalists_notes_st` |
| `BOT_PROXY_URL` | Outbound HTTP/SOCKS proxy URL | `""` |
| `BOT_PROXY_DISABLED` | Global proxy override (non-empty value disables) | `""` |
| `MODEL_LEADERBOARD_TOKEN` | Bearer token to publish benchmark scores | `""` |
| `BOT_BENCHMARK_DISABLED` | Benchmark runner kill switch | `""` |
| `BOT_ACPX_LOCK_PATH` | Shared acpx lock file | `/var/lock/acpx.lock` |
| `BOT_ACPX_ACTIVE_PATH` | File touched to indicate active user chat | `/run/vds-agent-active` |
| `BOT_BENCHMARK_ROOT` | Workspace path for benchmark runs | `/var/lib/vds-agent/sessions/benchmarks` |

---

## 🚀 Deployment & Operations Playbook

### 1. SSH Server Connection
* **VDS Host Alias:** `hetzner-bot` (configured locally in `~/.ssh/config`).
* **Systemd Service:** `vds-agent` (`vds-agent.service`).

### 2. Automated CI/CD Deployment
Any push to `main` affecting files in `bot/**` triggers `.github/workflows/deploy.yml`:
1. Preflight syntax check using `py_compile`.
2. Connects to server via Tailscale (scope `tag:gha-runner`).
3. Ships files to `/tmp/deploy-bot` and installs them to their respective system paths (see mapping below).
4. Runs database migrations (`migrate_bot_db`).
5. Rebuilds the Podman sandbox image if `Containerfile.acpx-claude` changed.
6. Restarts `vds-agent` service.

**Verify deployment status:**
```bash
gh run list --branch main --limit 5
gh run view <run_id> --log-failed
```

### 3. Server Binaries Mapping

| Local Repository Path | Server Installation Path | Permissions |
| --- | --- | --- |
| `bot/vds-agent.py` | `/usr/local/bin/vds-agent` | `0755` |
| `bot/migrate.py` | `/usr/local/bin/migrate_bot_db` | `0755` |
| `bot/model-health-check.py` | `/usr/local/bin/model-health-check` | `0755` |
| `bot/model-audio-check.py` | `/usr/local/bin/model-audio-check` | `0755` |
| `bot/model-media-check.py` | `/usr/local/bin/model-media-check` | `0755` |
| `bot/model-benchmark.py` | `/usr/local/bin/model-benchmark` | `0755` |
| `bot/scripts/refresh-benchmark-datasets.py` | `/usr/local/bin/refresh-benchmark-datasets` | `0755` |
| `bot/vds-agent.service` | `/etc/systemd/system/vds-agent.service` | `0644` |
| `bot/cron.d/model-checks` | `/etc/cron.d/model-checks` | `0644` |
| `bot/benchmark-tasks.json` | `/etc/socks-monitor/benchmark-tasks.json` | `0644` |
| `bot/benchmark-tasks.md` | `/etc/socks-monitor/benchmark-tasks.md` | `0644` |
| `bot/benchmark-datasets/*.json` | `/etc/socks-monitor/benchmark-datasets/` | `0644` |
| `bot/migrations/*.py` | `/usr/local/bin/migrations/` | `0644` |
| `bot/agent/*.py` | `/usr/local/bin/agent/` | `0644` |

### 4. Manual Deployment (Bot Code Only)
```bash
./deploy.sh
```
Copies `bot/vds-agent.py` directly to the server and restarts the systemd unit.

### 5. Verification Commands
```bash
# Check service status
ssh hetzner-bot 'systemctl status vds-agent'
ssh hetzner-bot 'sudo journalctl -u vds-agent -n 100 --no-pager'

# View logs of model health checks
ssh hetzner-bot 'tail -n 50 /var/log/model-health-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-audio-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-media-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-benchmark.log'
```

---

## 📊 Cron Jobs & Manual Execution Runbook

### 1. Cron Job Schedule
Configured in `/etc/cron.d/model-checks`:
* **Model Health (Text):** Runs every 10 min. Logs to `/var/log/model-health-check.log`.
* **Model Audio (STT/TTS):** Runs every 15 min. Logs to `/var/log/model-audio-check.log`.
* **Model Media (Image/Video):** Runs every 15 min. Logs to `/var/log/model-media-check.log`.
* **Leaderboard Benchmark:** Runs daily at 04:17 UTC. Logs to `/var/log/model-benchmark.log`.

### 2. Manually Trigger Probes & Benchmark
Run these via SSH to run checks or benchmark outside the normal cron schedule:
```bash
# Health probes
ssh hetzner-bot 'sudo /usr/local/bin/model-health-check'
ssh hetzner-bot 'sudo /usr/local/bin/model-audio-check'
ssh hetzner-bot 'sudo /usr/local/bin/model-media-check'

# Daily Leaderboard Benchmark (runs both native and claude modes)
ssh hetzner-bot 'sudo nohup /bin/sh -c "set -a; . /opt/smolevich-ai-bot/.env 2>/dev/null || true; . /etc/socks-monitor/vds-agent.env 2>/dev/null || true; set +a; [ \"\$BOT_BENCHMARK_DISABLED\" = \"1\" ] && exit 0; /usr/local/bin/model-benchmark run --max-jobs 200; if [ -n \"\$MODEL_LEADERBOARD_TOKEN\" ]; then /usr/local/bin/model-benchmark leaderboard --publish; /usr/local/bin/model-benchmark leaderboard --publish-tasks; fi; /usr/local/bin/model-benchmark purge" >> /var/log/model-benchmark.log 2>&1 &'
```

### 3. Server Configuration Prerequisites (Swap & Logs)
Must be configured manually on a new server instance:
```bash
# 1. Enable 2 GiB Swap
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 2. Setup Cron Log Rotation
sudo tee /etc/logrotate.d/model-checks <<'EOF'
/var/log/model-*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# 3. Old Session Workspace Cleanup (Runs at 04:00 daily)
echo '0 4 * * * root find /var/lib/vds-agent/sessions -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +' \
  | sudo tee /etc/cron.d/vds-agent-session-cleanup
```

---

## 🔗 Main Website & Publish Integration

* **Leaderboard Publish Endpoint:** `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models`
* **Tasks Publish Endpoint:** `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/benchmark-tasks`
* **Integration Spec (Main Site Repo):** `~/pet-projects/smolevich-main-site/.claude/benchmark-integration.md`

---

## 💬 Telegram Slash Commands Menu Troubleshooting
Slash commands (`/stt`, `/tts`, etc.) are registered on bot startup but can be cached by clients.
* **Server-side check:** Restart the bot and verify commands setup logs:
  ```bash
  ssh hetzner-bot 'systemctl restart vds-agent'
  ssh hetzner-bot 'sudo journalctl -u vds-agent -n 80 --no-pager'
  ```
* **Client-side reload:**
  1. Manually send a command (e.g. `/stt`) to activate it.
  2. Close and reopen the chat (or restart the Telegram app) and wait ~1 minute.
  3. Send `/start` to force client menu updates.
