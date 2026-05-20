# Deploy

## SSH host

- **Alias:** `hetzner-bot` (configured locally in `~/.ssh/config`).
- **Systemd unit:** `vds-agent` (`vds-agent.service`).

## Automatic (full)

Push to `main` touching `bot/**` triggers `.github/workflows/deploy.yml` (`appleboy/scp-action` + `ssh-action`). It connects via Tailscale (`tailscale/github-action@v2`, scope `tag:gha-runner`), ships every server-side artifact, and runs migrations:

| Local | Server | Perm |
| --- | --- | --- |
| `bot/vds-agent.py` | `/usr/local/bin/vds-agent` | `0755` |
| `bot/migrate.py` | `/usr/local/bin/migrate_bot_db` | `0755` |
| `bot/model-health-check.py` | `/usr/local/bin/model-health-check` | `0755` |
| `bot/model-audio-check.py` | `/usr/local/bin/model-audio-check` | `0755` |
| `bot/model-media-check.py` | `/usr/local/bin/model-media-check` | `0755` |
| `bot/model-benchmark.py` | `/usr/local/bin/model-benchmark` | `0755` |
| `bot/scripts/refresh-benchmark-datasets.py` | `/usr/local/bin/refresh-benchmark-datasets` | `0755` |
| `bot/benchmark-tasks.json` | `/etc/socks-monitor/benchmark-tasks.json` | `0644` |
| `bot/benchmark-tasks.md` | `/etc/socks-monitor/benchmark-tasks.md` | `0644` |
| `bot/benchmark-datasets/*.json` | `/etc/socks-monitor/benchmark-datasets/` | `0644` |
| `bot/vds-agent.service` | `/etc/systemd/system/vds-agent.service` | `0644` |
| `bot/cron.d/model-checks` | `/etc/cron.d/model-checks` | `0644` |
| `bot/migrations/*.py` | `/usr/local/bin/migrations/` | `0644` |
| `bot/agent/*.py` | `/usr/local/bin/agent/` | `0644` |
| (assembled in workflow) | `/opt/smolevich-ai-bot/.env` (0600 root) | — |

After copying, the workflow runs `systemctl daemon-reload`, `migrate_bot_db`, rebuilds the Podman image if `Containerfile.acpx-claude` changed, then `systemctl restart vds-agent` and verifies it is `active`.

Required GitHub secrets:
- `VDS_HOST`, `VDS_PORT`, `VDS_USER`, `VDS_SSH_KEY`, `VDS_SSH_PASSPHRASE` (SSH).
- `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` (Tailscale OAuth, scope `tag:gha-runner`).
- `BOT_PROXY_URL`, `BOT_PROXY_DISABLED`, `HF_TOKEN`, `MODEL_LEADERBOARD_TOKEN` (env file).
- `BOT_BENCHMARK_DISABLED` (optional kill switch — leave empty to enable benchmark).

### Confirm deploy in GitHub Actions

`git push` only uploads to GitHub. The actual server deploy can still fail. Always check the workflow:

```sh
gh run list --branch main --limit 5
gh run view <run_id> --log-failed
```

Deploy is complete only when workflow `Deploy to VDS` shows `completed/success`.

## Manual (bot only)

```sh
./deploy.sh
```

Ships `bot/vds-agent.py` only and restarts the systemd unit. Use the GitHub workflow if you also need to push migrations / cron scripts / the systemd unit.

## Cron jobs (deployed via CI)

`bot/cron.d/model-checks` is installed to `/etc/cron.d/model-checks` on every deploy. All scripts use `flock` to prevent overlapping runs:

| Script | Schedule | Log |
| --- | --- | --- |
| `model-health-check` (text/code probes) | every 10 min | `/var/log/model-health-check.log` |
| `model-audio-check` (STT/TTS probes) | every 15 min | `/var/log/model-audio-check.log` |
| `model-media-check` (image/video discovery) | every 15 min | `/var/log/model-media-check.log` |
| `model-benchmark run + leaderboard + purge` | daily 04:17 UTC | `/var/log/model-benchmark.log` |

The benchmark pipeline takes the global `acpx` lock for its claude task, so it never collides with a live user chat in claude-mode.

## Manually trigger probes & benchmark

```sh
# Health probes
ssh hetzner-bot 'sudo /usr/local/bin/model-health-check'
ssh hetzner-bot 'sudo /usr/local/bin/model-audio-check'
ssh hetzner-bot 'sudo /usr/local/bin/model-media-check'

# Daily leaderboard benchmark (native + claude)
ssh hetzner-bot 'sudo nohup /bin/sh -c "set -a; . /opt/smolevich-ai-bot/.env 2>/dev/null || true; . /etc/socks-monitor/vds-agent.env 2>/dev/null || true; set +a; [ \"\$BOT_BENCHMARK_DISABLED\" = \"1\" ] && exit 0; /usr/local/bin/model-benchmark run --max-jobs 200; if [ -n \"\$MODEL_LEADERBOARD_TOKEN\" ]; then /usr/local/bin/model-benchmark leaderboard --publish; /usr/local/bin/model-benchmark leaderboard --publish-tasks; fi; /usr/local/bin/model-benchmark purge" >> /var/log/model-benchmark.log 2>&1 &'
```

## Server one-time prerequisites

Run before the first benchmark cron tick on a fresh box:

```sh
# 1. Enable 2 GiB swap — without it an OOM kills vds-agent.
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 2. Log rotation for cron logs (model-health-check.log grows fast).
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

# 3. Old session cleanup (runs daily at 04:00).
echo '0 4 * * * root find /var/lib/vds-agent/sessions -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +' \
  | sudo tee /etc/cron.d/vds-agent-session-cleanup
```

## Verify

```sh
gh run list --limit 5
ssh hetzner-bot 'systemctl is-active vds-agent'
ssh hetzner-bot 'sudo journalctl -u vds-agent -n 100 --no-pager'
ssh hetzner-bot 'tail -n 50 /var/log/model-health-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-audio-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-media-check.log'
ssh hetzner-bot 'tail -n 50 /var/log/model-benchmark.log'
```

## Telegram command menu refresh

`setMyCommands` is called on every bot start, but Telegram clients can cache the slash menu.

Server-side checks:

```sh
ssh hetzner-bot 'systemctl restart vds-agent'
ssh hetzner-bot 'sudo journalctl -u vds-agent -n 80 --no-pager'
```

Client-side refresh (if `/stt`/`/tts` still not visible):

1. Type the command manually once (e.g. `/stt`) — the backend command is already active.
2. Reopen the chat with the bot (or restart Telegram) and wait 30–60 s.
3. If needed, send `/start` to force the menu reload.
