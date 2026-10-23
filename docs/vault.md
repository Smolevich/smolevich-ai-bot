# Vault — secrets access

Runtime secrets live in HashiCorp Vault, **KV v2**, as a single secret at
`secret/smolevich-ai-bot`. Fields are named like the env vars the bot expects:

| Field | Used as | Consumed by |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | `.env` + `/etc/socks-monitor/.openrouter_key` | bot, probes, benchmark |
| `GROQ_API_KEY` | `.env` + `.groq_key` | bot, probes, benchmark |
| `NVIDIA_API_KEY` | `.env` + `.nvidia_key` | bot, probes, benchmark |
| `MODEL_LEADERBOARD_TOKEN` | `.env` | benchmark `--publish` |
| `PROXY_URL` | `.env` `BOT_PROXY_URL` | bot, probes |
| `WEBHOOK_SECRET` | `.env` `BOT_WEBHOOK_SECRET` | bot (webhook auth) |
| `TELEGRAM_BOT_TOKEN` | `/etc/socks-monitor/.bot_token` | bot, health probe admin note |

`load_provider_key` reads the env var first, then the on-disk `key_file` as fallback — the deploy
writes both so the systemd bot, the benchmark cron (sources `.env`) and the health/audio/media cron
probes (read the key files directly) all stay in sync.

## Rotating the bot token

BotFather `/revoke` → `vault kv patch secret/smolevich-ai-bot TELEGRAM_BOT_TOKEN=-` (value on stdin) →
run the deploy workflow. The bot re-registers its webhook with the secret on start.

## How the deploy reads Vault

`.github/workflows/deploy.yml` runs `vault kv get -field=<FIELD> secret/smolevich-ai-bot` from inside
the SSH session on the server (Vault listens **localhost-only**), logging in via AppRole
`smolevich-ai-bot-deploy` (read-only `secret/smolevich-ai-bot`, token TTL 15m, bound to
`127.0.0.1/32`). `role_id` and `secret_id` are files owned by root in `/root/.config/smolevich-ai-bot/`
(laid out by infra, `vault_field_files`); the login body goes through stdin and the token is revoked
at the end of the step. Missing files → `exit 1`; failed login → values from the existing `.env`
with a warning (the 7-day degraded counter applies).

## How to authenticate manually (headless)

Vault listens **localhost-only on the server** (`hetzner-bot`, `127.0.0.1:8200`). Log in with the
**userpass** method as `smolevich90` (password is Stas's — not in this repo).

```bash
ssh hetzner-bot
export VAULT_ADDR=http://127.0.0.1:8200
# Flags MUST come before username=/password= positionals.
TOK=$(vault login -method=userpass -token-only -no-store username=smolevich90 password='<PW>')

# read
VAULT_TOKEN=$TOK vault kv get secret/smolevich-ai-bot
VAULT_TOKEN=$TOK vault kv get -field=HF_TOKEN secret/smolevich-ai-bot

# rotate one field (KV v2 patch preserves the others)
VAULT_TOKEN=$TOK vault kv patch secret/smolevich-ai-bot HF_TOKEN=<value>
```

After rotating in Vault, redeploy (push to `main` touching `bot/**` or run the workflow manually) so
the server `.env` and key files pick up the new value.

## Rotating the AppRole secret_id

From the infra repo: `ROTATE=yes bin/vault-approle.sh smolevich-ai-bot-deploy`, then the pyinfra run
(`fleet/pyinfra/run.sh bot_servers hetzner-bot`) lays the new `secret_id` file out on the server.

## Dead ends — do not waste time

- Public UI `https://vault.smolevich.com/ui` is behind Cloudflare Access SSO — not usable headless.
- Being **OS root** on the server does NOT grant a Vault token. Vault auth is separate.
- `vault login` flags (`-token-only -no-store`) must come **before** `username=/password=`.
