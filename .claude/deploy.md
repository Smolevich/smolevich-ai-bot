# Deploy

## Automatic (full)

Push to `main` touching `bot/**` triggers `.github/workflows/deploy.yml` (`appleboy/scp-action` + `ssh-action`). It connects via Tailscale (`tailscale/github-action@v2`, same OAuth client as voice-ai), ships every server-side artifact, and runs migrations:

| Local | Server |
| --- | --- |
| `bot/vds-agent.py` | `/usr/local/bin/vds-agent` |
| `bot/migrate.py` | `/usr/local/bin/migrate_bot_db` |
| `bot/model-health-check.py` | `/usr/local/bin/model-health-check` |
| `bot/model-audio-check.py` | `/usr/local/bin/model-audio-check` |
| `bot/model-media-check.py` | `/usr/local/bin/model-media-check` |
| `bot/model-benchmark.py` | `/usr/local/bin/model-benchmark` |
| `bot/scripts/refresh-benchmark-datasets.py` | `/usr/local/bin/refresh-benchmark-datasets` |
| `bot/benchmark-tasks.json` | `/etc/socks-monitor/benchmark-tasks.json` |
| `bot/benchmark-tasks.md` | `/etc/socks-monitor/benchmark-tasks.md` |
| `bot/benchmark-datasets/*.json` | `/etc/socks-monitor/benchmark-datasets/` |
| `bot/vds-agent.service` | `/etc/systemd/system/vds-agent.service` |
| `bot/cron.d/model-checks` | `/etc/cron.d/model-checks` |
| `bot/migrations/*.py` | `/usr/local/bin/migrations/` |
| (assembled in workflow) | `/opt/smolevich-ai-bot/.env` (0600 root) |

After copying, the workflow runs `systemctl daemon-reload`, `migrate_bot_db`, then `systemctl restart vds-agent` and verifies it's `active`.

Required GitHub secrets:
- `VDS_HOST`, `VDS_PORT`, `VDS_USER`, `VDS_SSH_KEY`, `VDS_SSH_PASSPHRASE` (SSH).
- `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` (Tailscale OAuth — same values as voice-ai, scope `tag:gha-runner`).
- `BOT_PROXY_URL`, `BOT_PROXY_DISABLED`, `HF_TOKEN`, `MODEL_LEADERBOARD_TOKEN` (env file).
- `BOT_BENCHMARK_DISABLED` (optional kill switch, leave empty to enable benchmark).

### Important: confirm deploy status in GitHub Actions

`git push` means only code upload to GitHub. Actual server deploy can still fail.
Always check the workflow result after push:

```
gh run list --branch main --limit 5
gh run view <run_id> --log-failed
```

Deploy is considered complete only when workflow `Deploy to VDS` has `completed/success`.

## Manual (bot only)

```
./deploy.sh
```

Ships `bot/vds-agent.py` only and restarts the service. Use the GitHub workflow if you also need to push migrations / cron scripts / the systemd unit. Requires your private SSH host alias in `~/.ssh/config` (examples below use `<bot-server>`).

## Cron jobs (deployed via CI)

`bot/cron.d/model-checks` is installed to `/etc/cron.d/model-checks` on every deploy.
All three scripts run every **10 minutes** with `flock` to prevent overlapping runs:

| Script | Schedule | Log |
| --- | --- | --- |
| `model-health-check` (text/code probes) | every 10 min | `/var/log/model-health-check.log` |
| `model-audio-check` (STT/TTS probes) | every 15 min | `/var/log/model-audio-check.log` |
| `model-media-check` (image/video discovery) | every 15 min | `/var/log/model-media-check.log` |
| `model-benchmark run + leaderboard + purge` | daily 04:17 UTC | `/var/log/model-benchmark.log` |

The benchmark pipeline takes the global `acpx` lock for its claude tasks, so it never collides with a live user chat in claude-mode.

## Benchmark prerequisites (one-time, manual on hetzner-bot)

Run before the first benchmark cron tick:

```sh
# 1. Enable 2 GiB swap — currently the box has zero swap and an OOM would kill vds-agent.
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 2. Log rotation for the cron logs (model-health-check.log is already ~21 MB).
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

# 3. Old user-session cleanup (currently /var/lib/vds-agent/sessions is ~750 MB).
echo '0 4 * * * root find /var/lib/vds-agent/sessions -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +' \
  | sudo tee /etc/cron.d/vds-agent-session-cleanup
```

## Benchmark endpoints

- `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models` — leaderboard payload (`source`, `updated_at`, `tasks`, `models[].task_results`).
- `PUT/GET https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/benchmark-tasks` — open methodology (`tasks`, `methodology_md`). Site repo spec: `~/pet-projects/smolevich-main-site/.claude/benchmark-integration.md`.

## Verify

```
gh run list --limit 5
ssh <bot-server> 'systemctl is-active vds-agent'
ssh <bot-server> 'journalctl -u vds-agent -n 50'
ssh <bot-server> 'tail -n 50 /var/log/model-health-check.log'
ssh <bot-server> 'tail -n 50 /var/log/model-audio-check.log'
ssh <bot-server> 'tail -n 50 /var/log/model-media-check.log'
```

## Telegram command menu refresh

`setMyCommands` is called on every bot start, but Telegram clients can cache the slash menu.

Server-side checks:

```
ssh <bot-server> 'systemctl restart vds-agent'
ssh <bot-server> 'journalctl -u vds-agent -n 80 --no-pager'
```

Client-side refresh (if `/stt`/`/tts` still not visible):

1. Type command manually once (e.g. `/stt`) — backend command is already active.
2. Reopen chat with the bot (or restart Telegram app) and wait 30-60s.
3. If needed, send `/start` to force menu reload in some clients.
