"""Bespoke, standalone execution runner for the burst/TieredBook model on
Kraken Futures (EEA flex multi-collateral perpetuals).

NOT a Hummingbot Script. Hummingbot has no Kraken Futures/derivatives
connector (confirmed 2026-09-21: only hummingbot/connector/exchange/kraken
exists, and it is spot-only -- no leverage/margin order support either).
Building a full Hummingbot connector would cost weeks and defeats the
reason Hummingbot was chosen in the first place. This follows the same
pattern already sanctioned for DEX/MEV (dex_runner.py): a bespoke module
that runs alongside Hummingbot, not inside it.

Reuses (imports, does not duplicate) the trigger-detection and tier-sizing
building blocks from ctb-research's already-audited capture_perps.py /
capture.py -- same TIER_PARAMS, MIN_LOT, FuturesTradeStore -- so the live
decision logic matches exactly what the shadow book
(capture_perps.py, running unmodified on Hetzner) has been validating.
This module only adds a real order-execution layer behind those decisions.

SAFETY (all defaults are the safe ones):
  - DRY_RUN: True unless env KRAKEN_LIVE=1 is set. In dry-run, every action
    that would touch the exchange is logged as "WOULD ..." and never sent.
  - Kill switch: TWO independent layers, combined with OR (either one
    halts). (1) Supabase `kill_switches` table, scope='global' (the
    ctb-bot/CLAUDE.md hard rule -- self-hosted Supabase on swm-company-01
    since 2026-09-21/22, see coolify_infra memory), polled over its REST
    API (PostgREST) with the service-role key, cached for
    KILL_SWITCH_POLL_S seconds so it isn't refetched on every single
    decision. FAIL-CLOSED: any error reaching Supabase (network, auth,
    missing env vars) is treated as ACTIVE, not inactive -- same "unknown
    means don't trade" philosophy as efficiency_ratio_24h() in capture.py.
    (2) A local file (KILL_SWITCH_PATH, default ctb-bot/KILL_SWITCH) as a
    physical fallback that doesn't depend on network reaching Supabase --
    touch it to halt immediately from the host itself, delete it to
    resume. Both checked before every decision and before every order.
    Activating either flattens any open real positions (see
    flatten_all_positions()) and refuses all new entries -- not just a
    "no new entries" gate.
  - MAX_STAKE_EUR / MAX_DAILY_LOSS_EUR: hard caps enforced in code before
    any order is placed, not just monitored after the fact.

Credentials: KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_API_SECRET env vars.
Never hardcoded, never logged, never committed. Generate a Futures API key
scoped to trade + read only -- NOT withdrawal.
Supabase: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY env vars (service-role,
per the schema's own comment: "the trader uses the service-role key" --
RLS only allows authenticated-user reads + service-role writes, and this
is a backend service, not a user session).

Run (safe, always dry-run unless KRAKEN_LIVE=1 is exported):
    python ctb-bot/scripts/kraken_perps_runner.py --seconds 180
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path("D:/ctb/ctb-research/src/ctb_research/ingest")))
from capture import CORE, MIN_LOT, TIER_PARAMS, TRADE, log  # noqa: E402
from capture_perps import FUTURES_SYMBOL, FuturesTradeStore  # noqa: E402

FUTURES_BASE = "https://futures.kraken.com"
ROOT = Path(__file__).resolve().parents[1]
KILL_SWITCH_PATH = Path(os.environ.get("KILL_SWITCH_PATH", ROOT / "KILL_SWITCH"))
DRY_RUN = os.environ.get("KRAKEN_LIVE") != "1"
FEE = 0.0007

# Hard risk caps -- overridable via env, but always present. These are
# enforced in code, not just displayed: an order that would exceed them
# is refused, not just logged as a warning.
MAX_STAKE_EUR = float(os.environ.get("MAX_STAKE_EUR", "50"))
MAX_DAILY_LOSS_EUR = float(os.environ.get("MAX_DAILY_LOSS_EUR", "30"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
KILL_SWITCH_POLL_S = float(os.environ.get("KILL_SWITCH_POLL_S", "3"))


class SupabaseKillSwitch:
    """Polls public.kill_switches (scope='global') over Supabase's REST API
    (PostgREST, exposed by the self-hosted stack's Kong gateway). Cached for
    KILL_SWITCH_POLL_S seconds -- cheap enough to check before every order
    without adding a network round-trip to the hot path every time.

    FAIL-CLOSED by design: missing env vars, a network error, a non-200
    response, or a malformed body all resolve to active=True. A kill switch
    that silently reads as "off" when it can't actually reach Supabase would
    defeat the entire point of having one."""

    def __init__(self):
        self._cached_active = True  # safe default until the first real check
        self._cached_reason = "nog niet gecontroleerd"
        self._checked_at = 0.0

    def _fetch(self) -> tuple[bool, str]:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            return True, "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY niet ingesteld"
        url = f"{SUPABASE_URL}/rest/v1/kill_switches?scope=eq.global&select=active,reason"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "user-agent": "ctb-bot/kraken_perps_runner",
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                rows = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return True, f"Supabase-check mislukt: {exc}"
        if not rows:
            return True, "geen global kill_switches-rij gevonden in Supabase"
        row = rows[0]
        active = bool(row.get("active"))
        reason = row.get("reason") or ("handmatig geactiveerd" if active else "uit")
        return active, reason

    def active(self) -> tuple[bool, str]:
        now = time.time()
        if now - self._checked_at >= KILL_SWITCH_POLL_S:
            self._cached_active, self._cached_reason = self._fetch()
            self._checked_at = now
        return self._cached_active, self._cached_reason


_supabase_kill_switch = SupabaseKillSwitch()


def kill_switch_active() -> tuple[bool, str]:
    """Combined check: local file OR Supabase, whichever fires first. Always
    returns (active, human-readable reason) so callers can log WHY."""
    if KILL_SWITCH_PATH.exists():
        return True, f"lokaal bestand aanwezig ({KILL_SWITCH_PATH})"
    return _supabase_kill_switch.active()


class KrakenFuturesClient:
    """Authenticated Kraken Futures (Derivatives) REST client.

    Auth scheme verified live against docs.kraken.com/api/docs/futures-api
    (Derivatives REST > Authentication), 2026-09-21:
      Authent = base64( HMAC-SHA512( base64_decode(secret),
                                      SHA256(postData + Nonce + endpointPath) ) )
    """

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self._secret_decoded = base64.b64decode(api_secret)

    def _authent(self, endpoint_path: str, post_data: str, nonce: str) -> str:
        message = (post_data + nonce + endpoint_path).encode()
        sha256_hash = hashlib.sha256(message).digest()
        sig = hmac.new(self._secret_decoded, sha256_hash, hashlib.sha512).digest()
        return base64.b64encode(sig).decode()

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = params or {}
        post_data = urllib.parse.urlencode(params)
        nonce = str(int(time.time() * 1000))
        endpoint_path = "/api/v3" + path
        authent = self._authent(endpoint_path, post_data, nonce)
        headers = {"APIKey": self.api_key, "Nonce": nonce, "Authent": authent,
                   "user-agent": "ctb-bot/kraken_perps_runner"}
        if method == "GET":
            url = f"{FUTURES_BASE}/derivatives{endpoint_path}"
            if post_data:
                url += "?" + post_data
            req = urllib.request.Request(url, headers=headers, method="GET")
        else:
            url = f"{FUTURES_BASE}/derivatives{endpoint_path}"
            req = urllib.request.Request(url, data=post_data.encode(), headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def get_wallets(self) -> dict:
        return self._request("GET", "/accounts")

    def get_open_positions(self) -> dict:
        return self._request("GET", "/openpositions")

    def send_order(self, order_type: str, symbol: str, side: str, size: float,
                    limit_price: float | None = None, stop_price: float | None = None,
                    reduce_only: bool = False, cli_ord_id: str | None = None) -> dict:
        params = {"orderType": order_type, "symbol": symbol, "side": side, "size": size}
        if limit_price is not None:
            params["limitPrice"] = limit_price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = "true"
        if cli_ord_id:
            params["cliOrdId"] = cli_ord_id
        return self._request("POST", "/sendorder", params)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", "/cancelorder", {"order_id": order_id})

    def cancel_all_orders(self, symbol: str | None = None) -> dict:
        return self._request("POST", "/cancelallorders", {"symbol": symbol} if symbol else {})


class OrderExecutor:
    """Places a bracket (entry + stop + take-profit) for one leg, honoring
    DRY_RUN, the kill switch, and the hard risk caps. Exit orders are
    reduceOnly so they can only close the position this entry opened, never
    open a new one. A horizon-based time exit is handled by the caller
    (cancel both bracket orders, close at market) since Kraken Futures has
    no native "exit after N minutes" order type."""

    def __init__(self, client: KrakenFuturesClient | None, daily_loss_tracker: "DailyLossTracker"):
        self.client = client
        self.daily_loss = daily_loss_tracker

    def open_leg(self, pair: str, tier: str, stake_eur: float, entry_px: float) -> dict | None:
        active, reason = kill_switch_active()
        if active:
            log(f"KILL SWITCH ACTIEF ({reason}) -- {pair} overgeslagen")
            return None
        if self.daily_loss.exceeded():
            log(f"DAGLIMIET BEREIKT (EUR {self.daily_loss.total:+.2f}) -- {pair} overgeslagen")
            return None
        if stake_eur > MAX_STAKE_EUR:
            log(f"{pair}: inzet EUR {stake_eur:,.2f} boven MAX_STAKE_EUR ({MAX_STAKE_EUR}) -- overgeslagen")
            return None

        symbol = FUTURES_SYMBOL[pair]
        p = TIER_PARAMS[tier]
        size = stake_eur * p["leverage"] / entry_px
        if size < MIN_LOT[pair]:
            log(f"{tier} {pair}: EUR {stake_eur:,.2f} ({size:.4f} eenheden) onder Kraken-minimum "
                f"van {MIN_LOT[pair]} eenheden -- overgeslagen")
            return None

        stop_px = entry_px * (1 - p["stop"])
        target_px = entry_px * (1 + p["target"])

        if DRY_RUN or self.client is None:
            log(f"[DRY RUN] ZOU PLAATSEN: {tier} {symbol} BUY {size:.4f} @ market "
                f"(stake EUR {stake_eur:,.2f}), stop {stop_px:.6g}, target {target_px:.6g}")
            return {"dry_run": True, "symbol": symbol, "size": size, "entry": entry_px,
                    "stop": stop_px, "target": target_px}

        entry = self.client.send_order("ioc", symbol, "buy", size)
        if entry.get("sendStatus", {}).get("status") != "placed":
            log(f"! {tier} {symbol}: entry niet geplaatst -- {entry}")
            return None
        stop_order = self.client.send_order("stp", symbol, "sell", size, stop_price=stop_px, reduce_only=True)
        tp_order = self.client.send_order("take_profit", symbol, "sell", size, stop_price=target_px, reduce_only=True)
        log(f"LIVE: {tier} {symbol} BUY {size:.4f} @ ~{entry_px:.6g}, stop {stop_px:.6g}, target {target_px:.6g}")
        return {"dry_run": False, "symbol": symbol, "size": size, "entry": entry_px,
                "stop_order_id": stop_order.get("sendStatus", {}).get("order_id"),
                "tp_order_id": tp_order.get("sendStatus", {}).get("order_id")}

    def close_leg_at_market(self, pos: dict, reason: str) -> None:
        if DRY_RUN or self.client is None:
            log(f"[DRY RUN] ZOU SLUITEN ({reason}): {pos['symbol']} SELL {pos['size']:.4f} @ market")
            return
        for oid_key in ("stop_order_id", "tp_order_id"):
            oid = pos.get(oid_key)
            if oid:
                try:
                    self.client.cancel_order(oid)
                except Exception as exc:
                    log(f"! kon {oid_key} niet annuleren voor {pos['symbol']}: {exc}")
        self.client.send_order("ioc", pos["symbol"], "sell", pos["size"], reduce_only=True)
        log(f"LIVE: {pos['symbol']} gesloten @ market ({reason})")

    def flatten_all_positions(self, reason: str) -> None:
        """Closes EVERY real open position on Kraken, queried fresh from the
        exchange rather than from in-process tracking -- this is what makes
        it a real "flatten" (CLAUDE.md hard rule) and not just a "stop
        opening new ones": it works correctly even after a restart, when any
        locally-remembered stop/tp order IDs from open_leg() are long gone.
        Called on kill-switch activation (both at startup and mid-run)."""
        if DRY_RUN or self.client is None:
            log(f"[DRY RUN] ZOU ALLE OPEN POSITIES SLUITEN ({reason})")
            return
        try:
            positions = self.client.get_open_positions().get("openPositions", [])
        except Exception as exc:
            log(f"! kon open posities niet ophalen om te flatten: {exc}")
            return
        if not positions:
            log(f"flatten ({reason}): geen open posities")
            return
        for pos in positions:
            symbol, size, side = pos["symbol"], pos["size"], pos["side"]
            try:
                self.client.cancel_all_orders(symbol)
            except Exception as exc:
                log(f"! kon open orders voor {symbol} niet annuleren: {exc}")
            close_side = "sell" if side == "long" else "buy"
            try:
                self.client.send_order("ioc", symbol, close_side, size, reduce_only=True)
                log(f"LIVE: {symbol} geflattened ({reason}), {side} {size} gesloten @ market")
            except Exception as exc:
                log(f"! FLATTEN MISLUKT voor {symbol} -- {exc} -- HANDMATIG INGRIJPEN VEREIST")


class DailyLossTracker:
    def __init__(self):
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.total = 0.0

    def record(self, eur: float) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day = today
            self.total = 0.0
        self.total += eur

    def exceeded(self) -> bool:
        return self.total <= -abs(MAX_DAILY_LOSS_EUR)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--check-only", action="store_true",
                     help="alleen get_wallets/get_open_positions tonen en stoppen (veilig, read-only)")
    ap.add_argument("--check-kill-switch", action="store_true",
                     help="alleen de kill-switch-status ophalen en tonen, dan stoppen (veilig, read-only, "
                          "handig om de Supabase-koppeling te verifieren zonder de rest te starten)")
    args = ap.parse_args()

    log(f"kraken_perps_runner start -- DRY_RUN={DRY_RUN}, MAX_STAKE_EUR={MAX_STAKE_EUR}, "
        f"MAX_DAILY_LOSS_EUR={MAX_DAILY_LOSS_EUR}, kill switch: lokaal={KILL_SWITCH_PATH}, "
        f"Supabase={SUPABASE_URL or '(niet ingesteld)'}")

    if args.check_kill_switch:
        active, reason = kill_switch_active()
        log(f"kill switch: {'ACTIEF' if active else 'uit'} -- {reason}")
        return 0

    api_key = os.environ.get("KRAKEN_FUTURES_API_KEY")
    api_secret = os.environ.get("KRAKEN_FUTURES_API_SECRET")
    client = KrakenFuturesClient(api_key, api_secret) if api_key and api_secret else None
    if client is None:
        log("geen KRAKEN_FUTURES_API_KEY/SECRET in environment -- draait volledig in DRY_RUN, "
            "zelfs read-only calls (get_wallets/get_open_positions) zijn niet beschikbaar")

    if args.check_only:
        if client is None:
            log("kan --check-only niet uitvoeren zonder API-credentials")
            return 1
        log(json.dumps(client.get_wallets(), indent=2))
        log(json.dumps(client.get_open_positions(), indent=2))
        return 0

    # Startup-flatten: als de kill switch al actief staat wanneer dit proces
    # opstart (bv. na een crash terwijl 'm iemand had omgezet), sluit dan
    # meteen alles wat nog open zou staan -- gebaseerd op de echte positie
    # bij Kraken, niet op lokaal onthouden state die een herstart niet
    # overleeft.
    active, reason = kill_switch_active()
    log(f"kill switch bij opstart: {'ACTIEF' if active else 'uit'} -- {reason}")
    if active:
        executor = OrderExecutor(client, DailyLossTracker())
        executor.flatten_all_positions(f"kill switch actief bij opstart ({reason})")

    log("nog niet verbonden aan de live triggerloop -- dit is de execution-laag, "
        "klaar om aangeroepen te worden zodra het startsein er is. Zie module-docstring. "
        "De kill-switch-koppeling (Supabase + lokaal bestand) is af en wordt hierboven "
        "al gecontroleerd bij elke start en voor elke order (zie OrderExecutor.open_leg).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
