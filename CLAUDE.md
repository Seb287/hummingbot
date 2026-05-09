# ctb-bot CLAUDE.md

Hummingbot fork. See workspace `CLAUDE.md` for the bigger picture.

## What this is

A fork of [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) (Apache-2.0) hosting custom Hummingbot Script classes for the user's personal crypto trading.

**Until the actual fork lands here, this directory contains only README + CLAUDE.md.** The fork happens at Phase 0; do not write Hummingbot code into this empty directory.

## Where custom code goes (when the fork is here)

- `scripts/` — Hummingbot Script classes. **All CTB-specific logic lives here**, not in upstream Hummingbot files. This keeps `git pull upstream main` clean.
- `conf/` — Hummingbot config files. Paper and live separated. Never commit live API keys.
- `dex_runner/` (or `scripts/dex_runner.py`) — separate Python process for DEX/MEV strategies (GMX v2, Aave, Curve liquidations). Hummingbot Scripts trigger it; it does its own web3.py work.

## Hard rules

- **Never edit upstream Hummingbot files** unless absolutely necessary. If you must, document why in a `PATCHES.md` and keep the diff minimal — every patch costs you on every upstream merge.
- **All Hummingbot Script classes must subclass `ScriptStrategyBase`** (or the V2 `Controller` framework). Don't reinvent.
- **State writes go to Supabase via MQTT bridge**, not directly. Single integration point = single failure point to debug.
- **Kill switch is mandatory.** Every Script polls (or subscribes via Realtime) the `kill_switches` table. Global `active=true` flattens positions and halts.
- **Personal use only.** No tenant constructs, no public APIs, no multi-user auth.

## Local dev

When the fork is here:

```bash
docker compose up    # Hummingbot + Gateway + dex_runner
# Hummingbot CLI attaches to a tty; use `docker compose exec hummingbot bash` then enter the CLI
```

Hetzner production deploy is the same `docker-compose.yml`, with `restart: always` and a remote `.env` containing real API keys.
