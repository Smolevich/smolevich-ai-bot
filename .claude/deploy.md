# Deploy

## Automatic (full)

Push to `main` touching `bot/**` triggers `.github/workflows/deploy.yml` (`appleboy/scp-action` + `ssh-action`). It ships every server-side artifact and runs migrations:

| Local | Server |
| --- | --- |
| `bot/vds-agent.py` | `/usr/local/bin/vds-agent` |
| `bot/migrate.py` | `/usr/local/bin/migrate_bot_db` |
| `bot/model-health-check.py` | `/usr/local/bin/model-health-check` |
| `bot/model-audio-check.py` | `/usr/local/bin/model-audio-check` |
| `bot/model-media-check.py` | `/usr/local/bin/model-media-check` |
| `bot/vds-agent.service` | `/etc/systemd/system/vds-agent.service` |
| `bot/cron.d/model-checks` | `/etc/cron.d/model-checks` |
| `bot/migrations/*.py` | `/usr/local/bin/migrations/` |

After copying, the workflow runs `systemctl daemon-reload`, `migrate_bot_db`, then `systemctl restart vds-agent` and verifies it's `active`.

Required GitHub secrets: `VDS_HOST`, `VDS_PORT`, `VDS_USER`, `VDS_SSH_KEY`, `VDS_SSH_PASSPHRASE`.

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

| Script | Log |
| --- | --- |
| `model-health-check` (text/code probes) | `/var/log/model-health-check.log` |
| `model-audio-check` (STT/TTS probes) | `/var/log/model-audio-check.log` |
| `model-media-check` (image/video discovery) | `/var/log/model-media-check.log` |

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
