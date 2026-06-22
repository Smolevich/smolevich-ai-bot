# Vault — secrets access

Runtime secrets live in HashiCorp Vault, **KV v2**, as a single secret at
`secret/smolevich-ai-bot`. Fields are named like the env vars the bot expects:

| Field | Used as | Consumed by |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | `.env` + `/etc/socks-monitor/.openrouter_key` | bot, probes, benchmark |
| `GROQ_API_KEY` | `.env` + `.groq_key` | bot, probes, benchmark |
| `CEREBRAS_API_KEY` | `.env` + `.cerebras_key` | bot, probes, benchmark |
| `NVIDIA_API_KEY` | `.env` + `.nvidia_key` | bot, probes, benchmark |
| `HF_TOKEN` | `.env` + `.hf_key` | bot, probes, benchmark |
| `MODEL_LEADERBOARD_TOKEN` | `.env` | benchmark `--publish` |
| `PROXY_URL` | `.env` `BOT_PROXY_URL` | bot, probes |

`load_provider_key` reads the env var first, then the on-disk `key_file` as fallback — the deploy
writes both so the systemd bot, the benchmark cron (sources `.env`) and the health/audio/media cron
probes (read the key files directly) all stay in sync.

## How the deploy reads Vault

`.github/workflows/deploy.yml` runs `vault kv get -field=<FIELD> secret/smolevich-ai-bot` from inside
the SSH session on the server (Vault listens **localhost-only**), authenticating with the
`VAULT_DEPLOY_TOKEN` GitHub secret. That token is scoped to the read-only `smolevich-ai-bot-read`
policy. The deploy fails fast (`exit 1`) if the token is missing or any required field is empty.

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

## Rotating the CI deploy token

`VAULT_DEPLOY_TOKEN` is a periodic (1y) orphan token under the `smolevich-ai-bot-read` policy. To
re-mint:

```bash
ssh hetzner-bot
export VAULT_ADDR=http://127.0.0.1:8200
TOK=$(vault login -method=userpass -token-only -no-store username=smolevich90 password='<PW>')
VAULT_TOKEN=$TOK vault token create -policy=smolevich-ai-bot-read -period=8760h -orphan \
  -display-name=smolevich-deploy -field=token
```

Then `gh secret set VAULT_DEPLOY_TOKEN` in this repo with the printed value.

## Dead ends — do not waste time

- Public UI `https://vault.smolevich.com/ui` is behind Cloudflare Access SSO — not usable headless.
- Being **OS root** on the server does NOT grant a Vault token. Vault auth is separate.
- `vault login` flags (`-token-only -no-store`) must come **before** `username=/password=`.
