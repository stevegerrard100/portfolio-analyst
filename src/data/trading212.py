"""
Trading 212 API client — read-only.

Authentication: HTTP Basic Auth with KEY:SECRET base64-encoded.
  Authorization: Basic base64(T212_API_KEY:T212_API_SECRET)

API keys are generated in the Trading 212 app:
  Settings → API Beta → Create API Key
Note: live and demo accounts use SEPARATE keys.

IMPORTANT: This module issues GET requests only.
POST / PUT / DELETE are never called under any circumstances.
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE_DEMO = "https://demo.trading212.com/api/v0"
BASE_LIVE = "https://live.trading212.com/api/v0"

CACHE_DIR = Path("cache")
CACHE_FILE = CACHE_DIR / "last_portfolio.json"
SECTORS_FILE = Path("config/sectors.json")

# Read-only endpoints — order-placement endpoints intentionally absent.
_EP_SUMMARY   = "/equity/account/summary"
_EP_CASH      = "/equity/account/cash"
_EP_POSITIONS = "/equity/positions"
_EP_ORDERS    = "/equity/history/orders"
_EP_PIES      = "/equity/pies"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class T212AuthError(Exception):
    """401 from T212 — credentials need to be regenerated."""


class T212Error(Exception):
    """General Trading 212 API error."""


# ---------------------------------------------------------------------------
# Ticker normalisation: T212 format → yfinance / market-data ticker
# ---------------------------------------------------------------------------

# Hard overrides for tickers that can't be derived by the regex rules.
_TICKER_OVERRIDES: dict[str, str] = {
    "BRK_B_US_EQ":  "BRK-B",
    "BRK_A_US_EQ":  "BRK-A",
    "BF_B_US_EQ":   "BF-B",
    "FB_US_EQ":     "META",      # Facebook → Meta Platforms rebrand (June 2022)
    "TWTR_US_EQ":   "X",         # Twitter → X Corp
    "AVAV__US_EQ":  "AVAV",      # Double-underscore T212 quirk for AeroVironment
    "FPp_EQ":       "FP.PA",     # TotalEnergies (Euronext Paris)
}

# T212 uses a lowercase suffix letter to encode the listing exchange for
# non-US equities, replacing the standard yfinance dot-suffix convention.
_EXCHANGE_SUFFIX: dict[str, str] = {
    "l": ".L",   # London Stock Exchange
    "d": ".DE",  # XETRA (Germany)
    "p": ".PA",  # Euronext Paris
}


def normalise_ticker(t212_ticker: str) -> str:
    """
    Convert a T212 instrument identifier to a standard ticker symbol.

    T212 examples:
      AAPL_US_EQ   → AAPL
      VWRP_EQ      → VWRP.L    (LSE ETF)
      SEMIl_EQ     → SEMI.L    (lowercase-l = London)
      COPGl_EQ     → COPG.L
      BRK_B_US_EQ  → BRK-B    (override)
      FB_US_EQ     → META      (override)
    """
    if t212_ticker in _TICKER_OVERRIDES:
        return _TICKER_OVERRIDES[t212_ticker]

    ticker = t212_ticker
    # Strip trailing exchange + asset-type suffixes
    ticker = re.sub(
        r"_(?:US|UK|EU|DE|FR|IT|ES|NL|CH|AU|CA|HK|JP|SG)(?:_EQ)?$",
        "", ticker, flags=re.IGNORECASE,
    )
    ticker = re.sub(r"_EQ$", "", ticker, flags=re.IGNORECASE)
    ticker = ticker.rstrip("_")  # Fix double-underscore artefacts (e.g. AVAV__)

    # Lowercase suffix = exchange indicator (e.g. SEMIl → SEMI.L)
    m = re.match(r"^(.+?)([a-z])$", ticker)
    if m:
        base, suffix = m.group(1), m.group(2)
        exchange = _EXCHANGE_SUFFIX.get(suffix)
        if exchange:
            return base + exchange

    # Single trailing uppercase letter → class-share separator (BRK_B → BRK-B)
    ticker = re.sub(r"_([A-Z])$", r"-\1", ticker)
    return ticker


# ---------------------------------------------------------------------------
# Sectors config + auto-resolution
# ---------------------------------------------------------------------------

def load_sectors_config() -> dict:
    try:
        with open(SECTORS_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.error("Could not load sectors config: %s", exc)
        return {"ticker_to_sector": {}, "ticker_to_holding_type": {}, "pie_labels": {}, "watchlist": []}


def _infer_holding_type(info: dict) -> str:
    """
    Infer holding_type from yfinance info.
    long_term: mega-cap ($100B+) with moderate volatility (beta < 1.2)
    short_term: high beta (>2.0) or micro-cap (<$300M)
    medium: everything else
    """
    market_cap = float(info.get("marketCap", 0) or 0)
    beta = float(info.get("beta", 1.0) or 1.0)
    if market_cap > 100e9 and beta < 1.2:
        return "long_term"
    if beta > 2.0 or (0 < market_cap < 300e6):
        return "short_term"
    return "medium"


def auto_resolve_sectors(tickers: list[str], sectors: dict) -> None:
    """
    For tickers not in sectors config, look up yfinance and fill in sector
    and holding_type.  ETFs are detected via quoteType.  Resolved entries are
    written back to sectors.json on disk immediately.  Unresolvable tickers
    are logged as warnings.
    """
    missing = [t for t in tickers if t not in sectors["ticker_to_sector"]]
    if not missing:
        return

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not available — cannot auto-resolve: %s", missing)
        return

    log.info("Auto-resolving %d unknown ticker(s) via yfinance: %s", len(missing), missing)
    resolved: list[str] = []

    for ticker in missing:
        try:
            info = yf.Ticker(ticker).info or {}
            quote_type = info.get("quoteType", "")

            if not quote_type or quote_type.upper() == "NONE":
                log.warning("No yfinance data for %-10s — leaving unresolved", ticker)
                continue

            if quote_type in ("ETF", "MUTUALFUND"):
                category = info.get("category", "")
                sector = f"{category} ETF" if category else "ETF"
                holding_type = "etf"
            else:
                sector = info.get("sector", "")
                if not sector:
                    log.warning("No sector from yfinance for %-10s (quoteType=%s)", ticker, quote_type)
                    continue
                holding_type = _infer_holding_type(info)

            sectors["ticker_to_sector"][ticker] = sector
            sectors["ticker_to_holding_type"][ticker] = holding_type
            log.info("Auto-resolved %-10s  sector=%-35s  type=%s", ticker, sector, holding_type)
            resolved.append(ticker)

        except Exception as exc:
            log.warning("yfinance lookup failed for %-10s: %s", ticker, exc)

    still_missing = [t for t in missing if t not in sectors["ticker_to_sector"]]
    if still_missing:
        log.warning("Could not resolve %d ticker(s): %s", len(still_missing), still_missing)

    if resolved:
        try:
            with open(SECTORS_FILE, "w") as f:
                json.dump(sectors, f, indent=2)
            log.info("sectors.json updated with %d auto-resolved ticker(s): %s", len(resolved), resolved)
        except Exception as exc:
            log.error("Failed to write sectors.json: %s", exc)


# ---------------------------------------------------------------------------
# API client (GET only)
# ---------------------------------------------------------------------------

class Trading212Client:
    """
    Read-only Trading 212 API client.
    Uses HTTP Basic Auth: base64(T212_API_KEY:T212_API_SECRET).
    Only GET requests are ever issued.
    """

    def __init__(self, use_live: bool = True):
        key    = os.environ["T212_API_KEY"]
        secret = os.environ["T212_API_SECRET"]
        token  = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self._base_url = BASE_LIVE if use_live else BASE_DEMO
        log.info("T212 client: %s", "LIVE" if use_live else "DEMO")

    def _get(self, endpoint: str, allow_404_empty: bool = False) -> dict | list:
        """
        Issue a GET request. Handles rate-limiting and raises typed exceptions.
        If allow_404_empty=True, a 404 response returns [] rather than raising.
        POST / PUT / DELETE are never called.
        """
        url = f"{self._base_url}{endpoint}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=30)
        except requests.RequestException as exc:
            raise T212Error(f"Network error on {endpoint}: {exc}") from exc

        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                if int(remaining) < 5:
                    log.warning("T212 rate limit low (%s remaining) — pausing 2s", remaining)
                    time.sleep(2)
            except ValueError:
                pass

        if resp.status_code == 401:
            raise T212AuthError(
                "T212 returned 401 Unauthorized.\n"
                "Regenerate: Trading 212 app → Settings → API Beta → Create new key.\n"
                "Note: live and demo accounts use SEPARATE keys."
            )

        if resp.status_code == 404 and allow_404_empty:
            log.info("T212 %s returned 404 — treating as empty", endpoint)
            return []

        if not resp.ok:
            raise T212Error(f"T212 {resp.status_code} on {endpoint}: {resp.text[:300]}")

        return resp.json()

    def _to_list(self, raw: dict | list) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("items", raw.get("positions", []))
        return []

    def get_account_summary(self) -> dict:
        return self._get(_EP_SUMMARY)

    def get_cash(self) -> dict:
        return self._get(_EP_CASH)

    def get_positions(self) -> list:
        # 404 means no open positions — not an error
        raw = self._get(_EP_POSITIONS, allow_404_empty=True)
        return self._to_list(raw)

    def get_order_history(self) -> list:
        return self._to_list(self._get(_EP_ORDERS))

    def get_pies(self) -> list:
        """Return list of pie summaries (id, cash, result).  404 → []."""
        raw = self._get(_EP_PIES, allow_404_empty=True)
        return raw if isinstance(raw, list) else []

    def get_pie_detail(self, pie_id: int) -> dict:
        """Return {settings, instruments} for a single pie."""
        return self._get(f"{_EP_PIES}/{pie_id}")

    def get_pie_positions(self) -> list[dict]:
        """
        Fetch all pies and aggregate their instruments into raw T212 position
        format compatible with _enrich_position.  Sleeps 32s between pie-detail
        requests (hard rate limit: 1 req/30s on pies endpoints).
        """
        pies = self.get_pies()
        if not pies:
            return []

        log.info("T212: %d pies — fetching details (1 req/30s rate limit)", len(pies))
        agg: dict[str, dict] = {}

        for pie_summary in pies:
            pie_id = pie_summary["id"]
            time.sleep(32)

            detail: dict = {}
            for attempt in range(3):
                try:
                    detail = self.get_pie_detail(pie_id)
                    # Rate-limit "errors" are returned as HTTP 200 with a BusinessException body
                    if isinstance(detail, dict) and detail.get("code") == "BusinessException":
                        log.warning("Pie %d rate limited on attempt %d, waiting 32s", pie_id, attempt + 1)
                        time.sleep(32)
                        detail = {}
                        continue
                    break
                except T212Error as exc:
                    log.warning("Pie %d attempt %d failed: %s", pie_id, attempt + 1, exc)
                    if attempt < 2:
                        time.sleep(32)

            if not detail:
                log.warning("Skipping pie %d after failed fetches", pie_id)
                continue

            pie_name = detail.get("settings", {}).get("name", str(pie_id))
            instruments = detail.get("instruments", [])
            log.info("T212 pie '%s' (%d): %d instruments", pie_name, pie_id, len(instruments))

            for instr in instruments:
                t = instr.get("ticker", "")
                if not t:
                    continue
                res = instr.get("result", {})
                qty    = float(instr.get("ownedQuantity", 0))
                cost   = float(res.get("priceAvgInvestedValue", 0))
                mktval = float(res.get("priceAvgValue", 0))
                ppl    = float(res.get("priceAvgResult", 0))

                if t in agg:
                    agg[t]["_qty"]    += qty
                    agg[t]["_cost"]   += cost
                    agg[t]["_mktval"] += mktval
                    agg[t]["_ppl"]    += ppl
                else:
                    agg[t] = {"_qty": qty, "_cost": cost, "_mktval": mktval, "_ppl": ppl}

        result: list[dict] = []
        for t, d in agg.items():
            qty = d["_qty"]
            result.append({
                "ticker":       t,
                "quantity":     qty,
                "averagePrice": round(d["_cost"] / qty, 4) if qty > 0 else 0.0,
                "currentPrice": round(d["_mktval"] / qty, 4) if qty > 0 else 0.0,
                "ppl":          d["_ppl"],
            })
        return result


# ---------------------------------------------------------------------------
# Position merging (direct holdings + pie holdings)
# ---------------------------------------------------------------------------

def _merge_raw_positions(direct: list[dict], pie: list[dict]) -> list[dict]:
    """
    Combine direct and pie raw positions, aggregating by T212 ticker.
    If the same ticker appears in both (unusual but possible), quantities and
    P&L are summed and per-share prices are recomputed from combined totals.
    """
    agg: dict[str, dict] = {}
    for pos in direct + pie:
        t = pos.get("ticker")
        if not t:
            log.warning("_merge_raw_positions: position missing 'ticker' key — skipping: %s", pos)
            continue
        qty = float(pos.get("quantity", 0))
        avg = float(pos.get("averagePrice", 0))
        cur = float(pos.get("currentPrice", 0))
        ppl = float(pos.get("ppl", 0))
        if t not in agg:
            agg[t] = {"ticker": t, "_qty": qty, "_cost": qty * avg, "_mktval": qty * cur, "_ppl": ppl}
        else:
            agg[t]["_qty"]    += qty
            agg[t]["_cost"]   += qty * avg
            agg[t]["_mktval"] += qty * cur
            agg[t]["_ppl"]    += ppl

    result: list[dict] = []
    for t, d in agg.items():
        qty = d["_qty"]
        result.append({
            "ticker":       t,
            "quantity":     qty,
            "averagePrice": round(d["_cost"] / qty, 4) if qty > 0 else 0.0,
            "currentPrice": round(d["_mktval"] / qty, 4) if qty > 0 else 0.0,
            "ppl":          d["_ppl"],
        })
    return result


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def _parse_account(summary: dict, cash: dict) -> dict:
    """
    Build a unified account dict from the two T212 account endpoints.

    summary shape: {id, currency, totalValue, cash: {availableToTrade, …},
                    investments: {currentValue, totalCost, realizedProfitLoss, …}}
    cash shape:    {free, total, ppl, result, invested, pieCash, blocked}
    """
    inv = summary.get("investments", {})
    c   = summary.get("cash", {})
    return {
        "currency":       summary.get("currency", "GBP"),
        "total":          float(summary.get("totalValue", 0) or cash.get("total", 0)),
        "cash_free":      float(c.get("availableToTrade", 0) or cash.get("free", 0)),
        "cash_reserved":  float(c.get("reservedForOrders", 0) or cash.get("blocked", 0)),
        "cash_in_pies":   float(c.get("inPies", 0) or cash.get("pieCash", 0)),
        "invested":       float(inv.get("currentValue", 0) or cash.get("invested", 0)),
        "total_cost":     float(inv.get("totalCost", 0)),
        "realized_ppl":   float(inv.get("realizedProfitLoss", 0) or cash.get("result", 0)),
        "unrealized_ppl": float(inv.get("unrealizedProfitLoss", 0) or cash.get("ppl", 0)),
    }


def _enrich_position(pos: dict, sectors: dict) -> dict:
    """Add sector / holding-type / pie-label metadata to a raw T212 position."""
    t212_ticker  = pos.get("ticker", "")
    ticker       = normalise_ticker(t212_ticker)
    sector       = sectors["ticker_to_sector"].get(ticker, "Unknown")
    holding_type = sectors["ticker_to_holding_type"].get(ticker, "medium")
    pie_label    = sectors["pie_labels"].get(sector, sector)

    quantity      = float(pos.get("quantity", 0))
    avg_price     = float(pos.get("averagePrice", 0))
    current_price = float(pos.get("currentPrice", 0))
    ppl           = float(pos.get("ppl", 0))

    cost_basis   = quantity * avg_price
    market_value = quantity * current_price
    pnl_pct      = ((current_price / avg_price) - 1) * 100 if avg_price > 0 else 0.0

    return {
        "t212_ticker":   t212_ticker,
        "ticker":        ticker,
        "sector":        sector,
        "holding_type":  holding_type,
        "pie_label":     pie_label,
        "quantity":      round(quantity, 6),
        "avg_price":     round(avg_price, 4),
        "current_price": round(current_price, 4),
        "ppl":           round(ppl, 2),
        "pnl_pct":       round(pnl_pct, 2),
        "cost_basis":    round(cost_basis, 2),
        "market_value":  round(market_value, 2),
    }


def _parse_order_history(raw_items: list) -> list:
    """Flatten the nested {order: {…}, fill: {…}} structure from history/orders."""
    result = []
    for item in raw_items:
        order  = item.get("order", {})
        fill   = item.get("fill", {})
        impact = fill.get("walletImpact", {})
        ticker_t212 = order.get("ticker", "")
        result.append({
            "ticker":            normalise_ticker(ticker_t212),
            "t212_ticker":       ticker_t212,
            "name":              order.get("instrument", {}).get("name", ""),
            "side":              order.get("side", ""),
            "quantity":          abs(float(order.get("filledQuantity", 0))),
            "fill_price":        float(fill.get("price", 0)),
            "net_value_gbp":     float(impact.get("netValue", 0)),
            "realized_ppl_gbp":  float(impact.get("realisedProfitLoss", 0)),
            "fx_rate":           float(impact.get("fxRate", 1)),
            "filled_at":         fill.get("filledAt", ""),
            "status":            order.get("status", ""),
        })
    return result


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _save_cache(portfolio: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(portfolio, f, indent=2)
    except Exception as exc:
        log.warning("Portfolio cache write failed: %s", exc)


def load_last_known_portfolio() -> dict | None:
    """Return last successfully fetched portfolio from cache, or None."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        log.warning("Using cached portfolio (fetched %s)", data.get("fetched_at", "?"))
        return data
    except Exception as exc:
        log.error("Cache read failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Mock portfolio for local dev / CI without live credentials
# ---------------------------------------------------------------------------

def _mock_portfolio() -> dict:
    sectors = load_sectors_config()
    raw = [
        {"ticker": "AAPL_US_EQ", "quantity": 15,  "averagePrice": 172.50, "currentPrice": 300.23, "ppl": 1916.0},
        {"ticker": "MSFT_US_EQ", "quantity": 8,   "averagePrice": 310.00, "currentPrice": 430.50, "ppl":  964.0},
        {"ticker": "NVDA_US_EQ", "quantity": 20,  "averagePrice":  85.00, "currentPrice": 225.32, "ppl": 2806.4},
        {"ticker": "VWRP_EQ",    "quantity": 50,  "averagePrice":  88.00, "currentPrice": 110.20, "ppl": 1110.0},
        {"ticker": "GLD_US_EQ",  "quantity": 10,  "averagePrice": 175.00, "currentPrice": 310.50, "ppl": 1355.0},
        {"ticker": "GDX_US_EQ",  "quantity": 30,  "averagePrice":  28.50, "currentPrice":  48.20, "ppl":  591.0},
    ]
    positions = [_enrich_position(p, sectors) for p in raw]
    mv = sum(p["market_value"] for p in positions)
    ppl = sum(p["ppl"] for p in positions)
    return {
        "positions":   positions,
        "order_history": [],
        "account": {
            "currency": "GBP", "total": round(mv + 2500, 2),
            "cash_free": 2500.0, "cash_reserved": 0.0, "cash_in_pies": 0.0,
            "invested": round(mv, 2), "total_cost": 0.0,
            "realized_ppl": 1857.04, "unrealized_ppl": round(ppl, 2),
        },
        "is_mock": True,
        "fetched_at": datetime.now().isoformat(),
        "environment": "MOCK",
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def fetch_portfolio(use_live: bool = True, allow_mock: bool = False) -> dict:
    """
    Fetch the full portfolio state from Trading 212 and enrich with sector metadata.

    Returns:
      positions:     list of enriched position dicts
      order_history: list of flattened historical order dicts
      account:       unified account financials
      environment:   'LIVE' | 'DEMO' | 'MOCK' | 'CACHED'
      is_mock:       True when using mock or cached data
      fetched_at:    ISO timestamp

    On API failure falls back to cache/last_portfolio.json, then to mock
    if allow_mock=True.
    """
    sectors = load_sectors_config()

    try:
        client = Trading212Client(use_live=use_live)

        log.info("T212: fetching positions...")
        raw_positions = client.get_positions()

        log.info("T212: fetching pie positions...")
        raw_pie_positions = [p for p in client.get_pie_positions() if float(p.get("quantity", 0)) > 0]
        if raw_pie_positions:
            raw_positions = _merge_raw_positions(raw_positions, raw_pie_positions)
            log.info("T212: merged — %d total positions from pies", len(raw_positions))

        log.info("T212: fetching account summary...")
        summary = client.get_account_summary()

        log.info("T212: fetching cash...")
        cash_data = client.get_cash()

        log.info("T212: fetching order history...")
        raw_orders = client.get_order_history()

    except T212AuthError as exc:
        log.error("T212 auth failed: %s", exc)
        cached = load_last_known_portfolio()
        if cached:
            cached["environment"] = "CACHED"
            cached["is_mock"] = True
            return cached
        if allow_mock:
            log.warning("No cache available — using mock portfolio")
            return _mock_portfolio()
        raise

    except T212Error as exc:
        log.error("T212 API error: %s", exc)
        cached = load_last_known_portfolio()
        if cached:
            cached["environment"] = "CACHED"
            cached["is_mock"] = True
            return cached
        if allow_mock:
            return _mock_portfolio()
        raise

    # Auto-resolve any tickers not yet in sectors config, then enrich
    all_tickers = list({normalise_ticker(p["ticker"]) for p in raw_positions})
    auto_resolve_sectors(all_tickers, sectors)

    positions     = [_enrich_position(p, sectors) for p in raw_positions]
    order_history = _parse_order_history(raw_orders)
    account       = _parse_account(summary, cash_data)

    environment = "LIVE" if use_live else "DEMO"
    portfolio = {
        "positions":     positions,
        "order_history": order_history,
        "account":       account,
        "is_mock":       False,
        "fetched_at":    datetime.now().isoformat(),
        "environment":   environment,
    }

    _save_cache(portfolio)
    log.info(
        "T212 %s: %d positions | total=%.2f %s | realized_ppl=%.2f",
        environment, len(positions),
        account["total"], account["currency"], account["realized_ppl"],
    )
    return portfolio
