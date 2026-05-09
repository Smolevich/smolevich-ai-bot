# Refactor plan: split `vds-agent.py` into a typed package

## Goal

Turn `bot/vds-agent.py` (one ~1250-line file, no type hints) into a small Python package with module-level boundaries and `from __future__ import annotations` types — without breaking prod. Each phase must be a single self-contained deploy that we can ship and verify before starting the next.

## Constraints

- Stdlib only (no new runtime deps).
- Service `ExecStart` is `/usr/bin/python3 /usr/local/bin/vds-agent`. Final layout must keep that working.
- DB schema (and migrations) is shared with `bot/model-health-check.py`. Refactor must not change DB columns or behaviour.
- The `acpx-claude:latest` Podman image and the `/etc/socks-monitor/.<provider>_key` file layout on the VDS are external contracts — don't touch them.
- **All four engine modes are in production traffic.** Per `request_log` snapshot at refactor start: `native` 44 calls (23% delivered), `claude` 22 (14% delivered, mostly ACP/model-mismatch errors), `pi` 14 (0% delivered — `pi` binary missing from sandbox image, separate bug), `opencode` 0. Refactor must keep all four code paths intact even if `pi` is currently broken upstream — that's not our regression to introduce.

## Target layout

```
bot/
├── agent/
│   ├── __init__.py
│   ├── __main__.py          # entry point: `python3 -m agent`
│   ├── config.py            # env vars, paths, version stamp
│   ├── providers.py         # PROVIDERS dict, key resolution, model fetch
│   ├── db.py                # sqlite wrappers (the existing DB class)
│   ├── telegram.py          # tg_request / tg_send_text / tg_send_long_text
│   ├── tools.py             # TOOLS, TOOL_HANDLERS, tool_run_in_container
│   ├── sandbox.py           # Podman sessions, ACP wrappers (ask_via_acpx)
│   ├── llm.py               # ask_llm, compact_history, history mgmt
│   └── handlers.py          # command dispatch + callback handlers
├── migrate.py               # unchanged
├── model-health-check.py    # unchanged for now
├── migrations/
└── ...
```

`/usr/local/bin/vds-agent` becomes a 3-line shim:

```python
#!/usr/bin/env python3
import runpy
runpy.run_module("agent", run_name="__main__")
```

The package itself ships to `/usr/local/lib/vds-agent/agent/`. CI installs both, sets `PYTHONPATH=/usr/local/lib/vds-agent` in the systemd unit.

## Phases

Each phase = one PR/commit, one deploy, one verification cycle. Stop and verify before the next.

### Phase 0 — package skeleton (no logic changes)

1. Create `bot/agent/__init__.py` and `bot/agent/__main__.py`. Move *all* of `vds-agent.py` into `agent/__main__.py` verbatim. Keep `bot/vds-agent.py` as the same 3-line shim that imports from the package, so the file path on the server stays the same.
2. Update `vds-agent.service`: add `Environment=PYTHONPATH=/usr/local/lib/vds-agent`.
3. Update CI (`.github/workflows/deploy.yml`):
   - Ship `bot/agent/` → `/usr/local/lib/vds-agent/agent/`
   - Ship `bot/vds-agent.py` (now the shim) → `/usr/local/bin/vds-agent`
4. **Verify**:
   - `gh run watch` succeeds.
   - `ssh vscale 'systemctl is-active vds-agent'` → `active`.
   - `ssh vscale 'journalctl -u vds-agent -n 50'` shows the same `VDS Agent starting on port 8080…` line.
   - Send `/version` and `/status` from Telegram — both reply.
   - Wait 5 min, check `tail /var/log/model-health-check.log` is still ticking (independent canary).

### Phase 1 — extract `config` (leaf, easiest)

Move env-var lookups, paths, `__VERSION_*` stamps, and the `PROVIDERS` dict into `agent/config.py`. Add type hints there. `__main__.py` imports them.

**Verify**: same as phase 0 + grep `__VERSION_SHA__` survived stamping.

### Phase 2 — extract `telegram`

Move `tg_request`, `tg_send_text`, `tg_send_long_text`, `_split_telegram_text`, `set_bot_commands` into `agent/telegram.py`. Type-hint the public functions.

**Verify**: `/version`, `/provider` (callback flow), `/models` keyboard rendering — all work.

### Phase 3 — extract `db`

Move the `DB` class (whichever singleton/module-level state it has) into `agent/db.py`. This is the highest-risk extraction because `model-health-check.py` imports nothing from the package — it talks to the DB directly via `sqlite3.connect(DB_FILE)`. So no cross-coupling, but be careful with the global `DB = ...` instantiation timing.

**Verify**: `/stats`, `/top`, `/users`, plus health-check log keeps writing rows (`sqlite3 /var/lib/telegram-llm-bot.db 'select count(*) from request_log'` before/after).

### Phase 4 — extract `tools`, `sandbox`, `llm`

Three sibling extractions, one commit each. After each: send a real message that triggers a tool call (e.g. weather), and a real LLM round-trip with tools both on and off (`/tools on` then `/tools off`).

**Mode coverage check after Phase 4**: walk all four engine modes — `/mode native` + send message, then `/mode claude`, `/mode opencode`, `/mode pi`. Even modes that are upstream-broken (`pi` currently fails before reaching the LLM) should produce the *same* error text as before the refactor. If the error message changes, you've regressed dispatch.

### Phase 5 — extract `handlers`

The big elif-chain. Move command handlers into `agent/handlers.py`. The dispatch dict can be `{"reset": handle_reset, ...}` instead of the chain — but defer that simplification, just move the chain first.

**Verify**: walk the full command list (`/provider`, `/models`, `/stats`, `/top`, `/status`, `/mode`, `/tools`, `/model`, `/reset`, `/feedback test`, `/version`).

### Phase 6 — share `providers` with health-check

`bot/model-health-check.py` has its own copy of the `PROVIDERS` dict. Make it import from `agent.providers`. CI must ship the package to a path the cron script can `sys.path.insert` into (or update the cron to run via `python3 -m`).

**Verify**: edit the providers dict, deploy, watch the next cron tick — log should reflect the change.

## Verification checklist (run after every phase)

```
# Local
python3 -m py_compile bot/agent/__main__.py bot/agent/*.py
python3 -c "import sys; sys.path.insert(0, 'bot'); import agent"  # smoke import

# CI
gh run watch <id> --exit-status

# Server
ssh vscale 'systemctl is-active vds-agent'
ssh vscale 'sudo journalctl -u vds-agent -n 50 --since "5 minutes ago"'
ssh vscale 'tail -n 20 /var/log/model-health-check.log'
ssh vscale 'sqlite3 /var/lib/telegram-llm-bot.db ".tables"'

# Telegram (manual)
/version            # confirms shim still routes to the package
/status             # exercises DB read
/provider → pick    # exercises callback handler
/models → pick      # exercises model selection + DB write
/feedback test      # exercises admin notify path
```

If any check fails: revert the phase commit, redeploy, debug locally, retry. Don't pile fixes on top of a broken phase.

## What we're explicitly NOT doing in this pass

- No tests added (no test infra, would be its own project).
- No async rewrite, no aiohttp, no swap of long-poll → webhook (TUNNEL_URL stays).
- No `requirements.txt` (stdlib only).
- No DB schema changes.
- No restructuring of `model-health-check.py` beyond phase 6.
