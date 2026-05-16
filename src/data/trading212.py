"""
Trading 212 API client — read-only.

Authentication uses a single API key in the Authorization header:
  Authorization: <api_key>

The key is generated in the Trading 212 app:
  Settings → API Beta → Create API Key

IMPORTANT: This client issues GET requests only.
POST / PUT / DELETE are never called under any circumstances.
"""

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

# Read-only permitted endpoints — order endpoints intentionally absent.
_EP_SUMMARY  = "/equity/account/summary"
_EP_CASH     = "/equity/account/cash"
_EP_POSITIONS = "/equity/portfolio/positions"
_EP_ORDERS   = "/equity/history/orders"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class T212AuthError(Exception):
    """Raised when the API returns 401 — credentials need to be regenerated."""


class T212Error(Exception):
    """General Trading 212 API error."""


# ---------------------------------------------------------------------------
# Ticker normalisation: T212 format → yfinance / market-data ticker
# ---------------------------------------------------------------------------

# Manual overrides for tickers that don't normalise cleanly
_TICKER_OVERRIDES = {
    "BRK_B_US_EQ": "BRK-B",
    "BRK_A_US_EQ": "BRK-A",
    "BF_B_US_EQ":  "BF-B",
}


def normalise_ticker(t212_ticker: str) -> str:
    """
    Convert a T212 instrument identifier to a standard ticker symbol.

    T212 format examples:
      AAPL_US_EQ   → AAPL
      VWRP_EQ      → VWRP
      BRK_B_US_EQ  → BRK-B
      TSLA_US_EQ   → TSLA
    """
    if t212_ticker in _TICKER_OVERRIDES:
        return _TICKER_OVERRIDES[t212_ticker]

    ticker = t212_ticker
    # Strip trailing exchange + type suffixes: _US_EQ, _UK_EQ, _EQ, _US, etc.
    ticker = re.sub(
        r"_(?:US|UK|EU|DE|FR|IT|ES|NL|CH|AU|CA|HK|JP|SG)(?:_EQ)?$",
        "", ticker, flags=re.IGNORECASE
    )
    ticker = re.sub(r"_EQ$", "", ticker, flags=re.IGNORECASE)
    # Single trailing letter after underscore → class share (BRK_B → BRK-B)
    ticker = re.sub(r"_([A-Z])$", r"-\1", ticker)
    return ticker


# ---------------------------------------------------------------------------
# Sectors config loader
# ---------------------------------------------------------------------------

def load_sectors_config() -> dict:
    """Load and return the parsed sectors.json config."""
    try:
        with open(SECTORS_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to load sectors config: %s", exc)
        return {
            "ticker_to_sector": {},
            "ticker_to_holding_type": {},
            "pie_labels": {},
            "watchlist": [],
        }


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class Trading212Client:
    """
    Read-only Trading 212 API client.
    Only GET requests are issued — no order placement ever.
    """

    def __init__(self, use_live: bool = False):
        self._api_key = os.environ.get("T212_API_KEY", "")
        self._base_url = BASE_LIVE if use_live else BASE_DEMO
        self._headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        log.info("T212 client: %s", "LIVE" if use_live else "DEMO")

    def _get(self, endpoint: str) -> dict | list:
        """
        Issue a GET request.  Handles rate limiting and raises typed exceptions.
        POST / PUT / DELETE are never called from this module.
        """
        url = f"{self._base_url}{endpoint}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=30)
        except requests.RequestException as exc:
            raise T212Error(f"Network error calling {endpoint}: {exc}") from exc

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
                "T212 API returned 401 Unauthorized.\n"
                "To fix: open Trading 212 app → Settings → API Beta → "
                "generate a new key and update T212_API_KEY in .env and GitHub Secrets.\n"
                "Note: demo and live accounts use separate API keys."
            )

        if not resp.ok:
            raise T212Error(f"T212 API {resp.status_code} on {endpoint}: {resp.text[:300]}")

        return resp.json()

    def _normalise_list(self, raw: dict | list) -> list:
        """Some API versions wrap lists in an 'items' key — normalise to list."""
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
        return self._normalise_list(self._get(_EP_POSITIONS))

    def get_order_history(self) -> list:
        return self._normalise_list(self._get(_EP_ORDERS))


# ---------------------------------------------------------------------------
# Position enrichment
# ---------------------------------------------------------------------------

def _enrich_position(pos: dict, sectors: dict) -> dict:
    """
    Add sector, holding_type, and pie_label metadata to a raw T212 position.
    Normalises the T212 ticker format to a standard ticker symbol.
    """
    t212_ticker = pos.get("ticker", "")
    ticker = normalise_ticker(t212_ticker)

    sector = sectors["ticker_to_sector"].get(ticker, "Unknown")
    holding_type = sectors["ticker_to_holding_type"].get(ticker, "medium")
    pie_label = sectors["pie_labels"].get(sector, sector)

    quantity = float(pos.get("quantity", 0))
    avg_price = float(pos.get("averagePrice", 0))
    current_price = float(pos.get("currentPrice", 0))
    ppl = float(pos.get("ppl", 0))  # profit/loss in account currency

    cost_basis = quantity * avg_price
    market_value = quantity * current_price
    pnl_pct = ((current_price / avg_price) - 1) * 100 if avg_price > 0 else 0.0

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
    """Return the last successfully fetched portfolio from cache, or None."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        log.warning("Using cached portfolio from %s", data.get("fetched_at", "unknown time"))
        return data
    except Exception as exc:
        log.error("Cache read failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Mock portfolio (used when live/demo credentials are unavailable locally)
# ---------------------------------------------------------------------------

def _mock_portfolio() -> dict:
    """
    Realistic sample portfolio for local development and CI without T212 credentials.
    Tickers match the sectors.json defaults so the full pipeline can be exercised.
    """
    sectors = load_sectors_config()
    raw_positions = [
        {"ticker": "AAPL_US_EQ",  "quantity": 15,    "averagePrice": 172.50, "currentPrice": 300.23, "ppl": 1916.0},
        {"ticker": "MSFT_US_EQ",  "quantity": 8,     "averagePrice": 310.00, "currentPrice": 430.50, "ppl": 964.0},
        {"ticker": "NVDA_US_EQ",  "quantity": 20,    "averagePrice": 85.00,  "currentPrice": 225.32, "ppl": 2806.4},
        {"ticker": "AMZN_US_EQ",  "quantity": 5,     "averagePrice": 140.00, "currentPrice": 205.80, "ppl": 329.0},
        {"ticker": "META_US_EQ",  "quantity": 6,     "averagePrice": 290.00, "currentPrice": 620.40, "ppl": 1982.4},
        {"ticker": "VWRP_EQ",     "quantity": 50,    "averagePrice": 88.00,  "currentPrice": 110.20, "ppl": 1110.0},
        {"ticker": "GLD_US_EQ",   "quantity": 10,    "averagePrice": 175.00, "currentPrice": 310.50, "ppl": 1355.0},
        {"ticker": "GDX_US_EQ",   "quantity": 30,    "averagePrice": 28.50,  "currentPrice": 48.20,  "ppl": 591.0},
    ]
    positions = [_enrich_position(p, sectors) for p in raw_positions]
    total_market_value = sum(p["market_value"] for p in positions)
    total_ppl = sum(p["ppl"] for p in positions)

    return {
        "positions": positions,
        "account": {
            "total": round(total_market_value + 2500.0, 2),
            "invested": round(total_market_value, 2),
            "cash_free": 2500.00,
            "cash_total": 2500.00,
            "total_ppl": round(total_ppl, 2),
        },
        "is_mock": True,
        "fetched_at": datetime.now().isoformat(),
        "environment": "MOCK",
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def fetch_portfolio(use_live: bool = False, allow_mock: bool = False) -> dict:
    """
    Fetch the full portfolio from Trading 212 and enrich with sectors metadata.

    Returns a dict with:
      positions: list of enriched position dicts
      account:   summary financials (total value, cash, P&L)
      fetched_at: ISO timestamp
      environment: 'LIVE' | 'DEMO' | 'MOCK' | 'CACHED'
      is_mock: True if using mock or cached data

    Falls back to cached portfolio if the API call fails.
    Raises T212Error if no data is available at all.
    """
    sectors = load_sectors_config()

    try:
        client = Trading212Client(use_live=use_live)

        log.info("Fetching T212 positions...")
        raw_positions = client.get_positions()

        log.info("Fetching T212 account summary...")
        summary = client.get_account_summary()

        log.info("Fetching T212 cash...")
        cash_data = client.get_cash()

    except T212AuthError as exc:
        log.error("T212 authentication failed:\n%s", exc)
        if allow_mock:
            log.warning("Falling back to MOCK portfolio data.")
            return _mock_portfolio()
        cached = load_last_known_portfolio()
        if cached:
            cached["environment"] = "CACHED"
            cached["is_mock"] = True
            return cached
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

    # Enrich positions with sector/holding metadata
    positions = [_enrich_position(p, sectors) for p in raw_positions]
    total_market_value = sum(p["market_value"] for p in positions)

    # Parse account summary — handle both response shapes
    def _cash_val(key: str) -> float:
        for obj in (summary, cash_data):
            if isinstance(obj, dict):
                if key in obj:
                    return float(obj[key])
                for v in obj.values():
                    if isinstance(v, dict) and key in v:
                        return float(v[key])
        return 0.0

    account = {
        "total":      _cash_val("total") or (total_market_value + _cash_val("free")),
        "invested":   _cash_val("invested") or total_market_value,
        "cash_free":  _cash_val("free"),
        "cash_total": _cash_val("free") + _cash_val("blocked"),
        "total_ppl":  _cash_val("ppl") or sum(p["ppl"] for p in positions),
    }

    environment = "LIVE" if use_live else "DEMO"
    portfolio = {
        "positions":   positions,
        "account":     account,
        "is_mock":     False,
        "fetched_at":  datetime.now().isoformat(),
        "environment": environment,
    }

    _save_cache(portfolio)
    log.info(
        "T212 portfolio fetched: %d positions  total=%.2f  P&L=%.2f",
        len(positions), account["total"], account["total_ppl"],
    )
    return portfolio
