# Deploy

## Automatic (full)

Push to `main` touching `bot/**` triggers `.github/workflows/deploy.yml` (`appleboy/scp-action` + `ssh-action`). It ships every server-side artifact and runs migrations:

| Local | Server |
| --- | --- |
| `bot/vds-agent.py` | `/usr/local/bin/vds-agent` |
| `bot/migrate.py` | `/usr/local/bin/migrate_bot_db` |
| `bot/model-health-check.py` | `/usr/local/bin/model-health-check` |
| `bot/model-audio-check.py` | `/usr/local/bin/model-audio-check` |
| `bot/vds-agent.service` | `/etc/systemd/system/vds-agent.service` |
| `bot/migrations/*.py` | `/usr/local/bin/migrations/` |

After copying, the workflow runs `systemctl daemon-reload`, `migrate_bot_db`, then `systemctl restart vds-agent` and verifies it's `active`.

Required GitHub secrets: `VDS_HOST`, `VDS_PORT`, `VDS_USER`, `VDS_SSH_KEY`, `VDS_SSH_PASSPHRASE`.

## Manual (bot only)

```
./deploy.sh
```

Ships `bot/vds-agent.py` only and restarts the service. Use the GitHub workflow if you also need to push migrations / cron scripts / the systemd unit. Requires the `vscale` SSH host alias in `~/.ssh/config`.

## Cron jobs (provisioned manually on the server)

- `/etc/cron.d/model-health-check` — `*/5 * * * *` runs `/usr/local/bin/model-health-check`, logs to `/var/log/model-health-check.log`.
- `/etc/cron.d/model-audio-check` — recommended separate schedule for `/usr/local/bin/model-audio-check` (for STT/TTS probes), logs to `/var/log/model-audio-check.log`.
- `/etc/cron.d/socks-notify` — `0 * * * *` runs `/usr/local/bin/socks-notify` (separate proxy monitor, not in this repo).

The cron files themselves are not in CI — set them up once per host.

## Verify

```
gh run list --limit 5
ssh vscale 'systemctl is-active vds-agent'
ssh vscale 'journalctl -u vds-agent -n 50'
ssh vscale 'tail -n 50 /var/log/model-health-check.log'
ssh vscale 'tail -n 50 /var/log/model-audio-check.log'
```

## Telegram command menu refresh

`setMyCommands` is called on every bot start, but Telegram clients can cache the slash menu.

Server-side checks:

```
ssh vscale 'systemctl restart vds-agent'
ssh vscale 'journalctl -u vds-agent -n 80 --no-pager'
```

Client-side refresh (if `/stt`/`/tts` still not visible):

1. Type command manually once (e.g. `/stt`) — backend command is already active.
2. Reopen chat with the bot (or restart Telegram app) and wait 30-60s.
3. If needed, send `/start` to force menu reload in some clients.
