# ctb-bot

Crypto trading bot — fork of [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) (Apache-2.0) under the user's GitHub.

## Status

**Placeholder.** The actual Hummingbot fork has not been created yet. This directory exists to anchor the project structure.

## Setup (when ready)

```bash
# Fork hummingbot/hummingbot on GitHub to your account
# Then:
cd D:\ctb
git clone https://github.com/<your-user>/hummingbot ctb-bot-fork
mv ctb-bot/README.md ctb-bot-fork/README-CTB.md
mv ctb-bot-fork ctb-bot
cd ctb-bot
# Add custom scripts under scripts/
# Configure conf/ with exchange API keys (paper-trade first)
docker compose -f docker-compose.yml up -d
```

## What this repo holds (target structure)

```
ctb-bot/                    fork of hummingbot/hummingbot
├── (upstream code)         preserved; merge selectively from upstream main
├── conf/                   Hummingbot config files (paper + live)
├── scripts/                CUSTOM Hummingbot Script classes
│   ├── local_mid.py        Multi-venue COB, pre-oracle path
│   ├── oracle_directional.py  Subscribes to ctb-oracle WS
│   ├── dan_arb.py          DAN-style cross-CEX arb
│   ├── oracle_xemm.py      XEMM with oracle as fair-value reference
│   ├── supabase_bridge.py  MQTT → Supabase + kill-switch reader
│   └── dex_runner.py       Custom DEX/MEV (GMX, Aave, Curve liquidations)
├── docker-compose.yml      Hummingbot + Gateway + dex_runner
├── Dockerfile.ctb          Custom image with our extra Python deps
└── .env.example            Exchange keys, Supabase, MQTT, Telegram
```

## Why a Hummingbot fork (decision: 2026-04-29)

- Native connectors for all four target CEXes (Kraken, Coinbase, KuCoin, Gate.io).
- Built-in `cross_exchange_market_making` strategy = ~80% of `dan_arb`.
- Hummingbot Gateway covers Curve and Aerodrome out of the box.
- Apache-2.0 license, clean fork, no upstream obligations.
- 24/7 ops (Docker `restart: always`, Telegram alerts, MQTT) battle-tested.
- ~5–6 weeks to first live capital instead of ~3 months of custom Rust build.

GMX v2 perps, Aave liquidations, and Flashbots are not in Gateway and live in `scripts/dex_runner.py`.

## UI

Hummingbot's own UI is operations-only:
- CLI (`prompt_toolkit` TUI) for start/stop/configure.
- Streamlit dashboard ([hummingbot/dashboard](https://github.com/hummingbot/dashboard)) for ops monitoring.

The user-facing UI is the **Next.js dashboard on Vercel** under `D:\ctb\ctb-research\dashboard\`. Hummingbot writes state to Supabase via MQTT bridge (`scripts/supabase_bridge.py`); the dashboard renders it.

## See also

- Approved plan: `~/.claude/plans/ctb-crypto-trading-quizzical-star.md`
- Workspace context: `D:\ctb\CLAUDE.md`
