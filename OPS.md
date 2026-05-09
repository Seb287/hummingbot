# CTB ops cheat sheet

The CTB stack on Hetzner runs Hummingbot plus four sidecar services for the
Vercel dashboard integration. All sidecars live in `docker-compose.override.yml`.

## Services

| Service | Port (host) | Purpose |
|---|---|---|
| `hummingbot` | (host network) | The bot itself |
| `hummingbot-broker` (EMQX) | 127.0.0.1:1883, 18083 | MQTT broker between bot and API |
| `hummingbot-postgres` | (internal only) | State for hummingbot-api |
| `hummingbot-api` | 127.0.0.1:8000 | FastAPI bridge for the Vercel dashboard |
| `hummingbot-dashboard` | 127.0.0.1:8501 | Streamlit ops UI (config, backtest, Optuna) |

All sidecar ports are bound to `127.0.0.1` only — not reachable from the public
internet. Reach them from your local machine via SSH tunnel.

## First-time bring-up on the Hetzner box

```bash
ssh ctb-bot
cd ~/ctb-bot
git pull
cp .env.api.example .env.api
nano .env.api          # set strong passwords; save
docker compose up -d   # pulls postgres:16, emqx:5, hummingbot/hummingbot-api, hummingbot/dashboard
docker compose ps      # verify all 5 services show "Up"
```

## SSH tunnel from your local PC (open everything you need at once)

```powershell
ssh -L 8501:localhost:8501 -L 8000:localhost:8000 -L 18083:localhost:18083 ctb-bot
```

Then in your browser:

| URL | What it is |
|---|---|
| http://localhost:8501 | Streamlit ops dashboard (Hummingbot's official) |
| http://localhost:8000/docs | hummingbot-api Swagger UI (test endpoints) |
| http://localhost:18083 | EMQX admin (default login: admin / public — change in EMQX UI) |

## Configure Hummingbot to publish to the MQTT broker

After the stack is up, attach to Hummingbot and enable the MQTT bridge so the
bot publishes its events to EMQX (where backend-api and supabase_bridge can
read them):

```bash
docker attach hummingbot
```

In the Hummingbot CLI:

```
config mqtt_bridge.mqtt_host 127.0.0.1
config mqtt_bridge.mqtt_port 1883
config mqtt_bridge.mqtt_autostart True
mqtt start
```

Detach with `Ctrl+P, Ctrl+Q`. The bot now publishes events to topic `hbot/...`
on EMQX.

## Common ops

| Task | Command |
|---|---|
| See all services | `docker compose ps` |
| Tail Hummingbot logs | `docker compose logs -f hummingbot` |
| Tail backend-api logs | `docker compose logs -f hummingbot-api` |
| Restart everything | `docker compose down && docker compose up -d` |
| Restart only the bot | `docker compose restart hummingbot` |
| Update images | `docker compose pull && docker compose up -d` |
| Stop everything | `docker compose down` (keeps volumes) |
| Nuke everything (incl. data) | `docker compose down -v` ⚠️ deletes EMQX + Postgres data |

## Troubleshooting

- **`hummingbot-api` won't start**: check `docker compose logs hummingbot-api`. Usually wrong password in `.env.api` or postgres isn't ready yet (give it 10 s and try again).
- **Streamlit dashboard shows "API connection failed"**: `BACKEND_API_USERNAME` / `BACKEND_API_PASSWORD` env vars must match `USERNAME` / `PASSWORD` in `.env.api`.
- **Hummingbot can't reach MQTT**: confirm EMQX is up (`docker compose ps`), then in Hummingbot CLI: `mqtt start`.
- **EMQX admin password forgotten**: `docker compose exec hummingbot-broker emqx_ctl admins passwd admin <new-password>`
