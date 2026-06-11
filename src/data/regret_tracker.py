"""
Regret Tracker — identifies fully exited positions since 1 Jan 2026 and
compares each sell price against the current market price.

No API calls here: current prices come from market_data (which already
fetched them in the main pipeline because get_exited_tickers() returns
the list so main.py can add them to the yfinance batch).
"""

import logging
import math

from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES
from src.data.trading212 import normalise_ticker

log = logging.getLogger(__name__)

_CUTOFF = "2026-01-01"


def _resolve(ticker: str) -> str:
    """Apply merger/rename overrides to a ticker (e.g. IIVI → COHR)."""
    return _MERGER_OVERRIDES.get(ticker, ticker)


def get_exited_tickers(order_history: list[dict], current_tickers: set[str]) -> list[str]:
    """
    Return tickers that have at least one SELL order from _CUTOFF onwards
    and are no longer present in the live portfolio.

    Merger overrides are applied to both sold tickers and current_tickers
    before comparison so that e.g. a SELL of IIVI is correctly excluded
    when the portfolio currently holds its successor COHR.

    Called before fetch_market_data so these tickers get their current
    prices included in the main yfinance batch.
    """
    # Resolve current holdings through merger map so successor tickers match.
    resolved_current = {_resolve(t) for t in current_tickers}

    sold: set[str] = set()
    for order in order_history:
        if order.get("side", "").upper() != "SELL":
            continue
        filled_at = order.get("filled_at", "")
        if not filled_at or filled_at[:10] < _CUTOFF:
            continue
        raw = order.get("ticker", "")
        ticker = _resolve(normalise_ticker(raw)) if raw else ""
        if ticker:
            sold.add(ticker)

    return [t for t in sold if t not in resolved_current]


def build_regret_tracker(
    order_history: list[dict],
    current_tickers: set[str],
    market_data: dict,
) -> list[dict]:
    """
    Build the regret tracker table from parsed order history + market_data.

    Logic:
    - Only SELL orders from _CUTOFF onwards.
    - Only tickers not currently held (fully exited).
    - Merger overrides applied to both sold tickers and current_tickers so
      that e.g. IIVI (sold) is correctly excluded when COHR (successor) is
      held.  This makes the function correct regardless of whether callers
      have already normalised tickers.
    - Where a ticker was sold multiple times, use the most recent sell.
    - Sort by pct_diff descending (biggest regret — current > sell — first).

    Returns a list of dicts:
        ticker, company_name, sell_date, sell_price, current_price, pct_diff
    """
    print("REGRET TRACKER CALLED")

    # Resolve current holdings through merger map.
    resolved_current = {_resolve(t) for t in current_tickers}
    # Log the portfolio set being used for exclusion, noting any that were
    # remapped (helps diagnose why a ticker does or doesn't appear in results).
    remapped = {t: _resolve(t) for t in current_tickers if _resolve(t) != t}
    if remapped:
        log.info(
            "Regret tracker: current_tickers (%d) — resolved set (%d): %s "
            "[merger remaps: %s]",
            len(current_tickers), len(resolved_current),
            sorted(resolved_current), remapped,
        )
    else:
        log.info(
            "Regret tracker: current_tickers (%d): %s",
            len(resolved_current), sorted(resolved_current),
        )

    # Count all SELL orders from cutoff to produce the diagnostic log line.
    all_sell_count = 0
    sells_all_tickers: set[str] = set()
    sells: dict[str, dict] = {}

    for order in order_history:
        if order.get("side", "").upper() != "SELL":
            continue
        filled_at = order.get("filled_at", "")
        if not filled_at or filled_at[:10] < _CUTOFF:
            continue
        raw_ticker = order.get("ticker", "")
        if not raw_ticker:
            continue

        ticker = _resolve(normalise_ticker(raw_ticker))
        all_sell_count += 1
        sells_all_tickers.add(ticker)

        if ticker in resolved_current:
            continue  # still held (or held as successor) — skip

        # Keep most recent sell for each exited ticker
        if ticker not in sells or filled_at > sells[ticker]["filled_at"]:
            sells[ticker] = {
                "ticker":       ticker,
                "company_name": order.get("name") or ticker,
                "sell_date":    filled_at[:10],
                "sell_price":   float(order.get("fill_price", 0) or 0),
                "filled_at":    filled_at,
            }

    log.info(
        "Regret tracker: %d SELL order(s) since %s | %d unique ticker(s) sold | "
        "%d fully exited (not in current portfolio)",
        all_sell_count, _CUTOFF, len(sells_all_tickers), len(sells),
    )

    if not sells:
        return []

    result = []
    for ticker, sell in sells.items():
        mkt = market_data.get(ticker, {})
        current_price = mkt.get("current_price")
        if (
            current_price is None
            or math.isnan(float(current_price))
            or float(current_price) <= 0
        ):
            log.warning(
                "Regret tracker: invalid current_price for %s (raw=%r) — skipping",
                ticker, current_price,
            )
            continue
        if sell["sell_price"] <= 0:
            log.warning("Regret tracker: zero sell price for %s — skipping", ticker)
            continue

        pct_diff = (float(current_price) / sell["sell_price"] - 1) * 100
        result.append({
            "ticker":        ticker,
            "company_name":  sell["company_name"],
            "sell_date":     sell["sell_date"],
            "sell_price":    round(sell["sell_price"], 4),
            "current_price": round(float(current_price), 4),
            "pct_diff":      round(pct_diff, 1),
        })

    result.sort(key=lambda x: x["pct_diff"], reverse=True)
    log.info("Regret tracker: %d entries", len(result))
    return result
