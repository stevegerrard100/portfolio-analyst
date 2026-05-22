#!/usr/bin/env python3
"""
Standalone regret tracker smoke test.

Runs only:
  1. T212 order history fetch  (get_order_history + _parse_order_history)
  2. Minimal yfinance price fetch for exited tickers only
  3. build_regret_tracker()

No Claude API, no full portfolio fetch, no screeners, no yfinance batch.
Typical runtime: 15-45 seconds (dominated by T212 rate-limit pauses).

Usage (run from project root):
    python scripts/test_regret.py
"""

import logging
import os
import sys
from pathlib import Path

# ── 0. Path and .env setup (must happen before src imports) ──────────────────

# Ensure project root is on sys.path so `from src...` imports work when the
# script is run directly rather than as a module.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Minimal .env parser — handles KEY=value, KEY="value", KEY='value', comments.
_ENV_FILE = _ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _val = _val.strip().strip('"').strip("'")
        os.environ.setdefault(_key.strip(), _val)
    print(f"Loaded environment from {_ENV_FILE}")
else:
    print(f"Warning: {_ENV_FILE} not found — relying on existing environment variables")

# ── 1. Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── 2. Imports (after path is set) ───────────────────────────────────────────

import pandas as pd
import yfinance as yf

from src.data.trading212 import Trading212Client, _parse_order_history
from src.data.regret_tracker import get_exited_tickers, build_regret_tracker

_CUTOFF = "2026-01-01"


# ── 3. Main ───────────────────────────────────────────────────────────────────

def main() -> None:

    # ── Step 1: Fetch order history ───────────────────────────────────────────
    log.info("Connecting to T212 (LIVE)...")
    client = Trading212Client(use_live=True)

    log.info("Fetching order history (pagination follows nextPagePath)...")
    raw_orders = client.get_order_history()
    log.info("Raw response: %d item(s)", len(raw_orders))

    # ── Step 2: Parse ─────────────────────────────────────────────────────────
    parsed = _parse_order_history(raw_orders)

    all_sells   = [o for o in parsed if o.get("side", "").upper() == "SELL"]
    with_price  = [o for o in parsed if float(o.get("fill_price") or 0) > 0]
    recent_sells = [
        o for o in all_sells
        if (o.get("filled_at") or "")[:10] >= _CUTOFF
    ]

    log.info(
        "Parsed: %d order(s) total | %d SELL | %d with fill_price > 0",
        len(parsed), len(all_sells), len(with_price),
    )

    if all_sells:
        dates = sorted(
            o["filled_at"][:10] for o in all_sells if o.get("filled_at")
        )
        log.info("SELL date range in response: %s → %s", dates[0], dates[-1])

    log.info("SELL orders on or after %s: %d", _CUTOFF, len(recent_sells))

    if not recent_sells:
        print("\nNo SELL orders found since the cutoff — nothing to track.")
        print("Possible causes:")
        print("  • Order history only returned pre-cutoff pages (check pagination logs above)")
        print("  • T212 returned an error body — check for 'error body' warnings above")
        return

    # ── Step 3: Identify exited tickers ──────────────────────────────────────
    # Pass empty current_tickers — this script has no portfolio fetch, so all
    # sold tickers are treated as exited.  Re-bought positions will appear; a
    # full pipeline run filters those out correctly.
    current_tickers: set[str] = set()
    exited = get_exited_tickers(parsed, current_tickers)
    log.info("Tickers to price: %s", exited if exited else "(none)")

    if not exited:
        print("\nget_exited_tickers() returned an empty list.")
        print("All sold tickers may have side != 'SELL' or filled_at pre-dates the cutoff.")
        return

    # ── Step 4: Fetch current prices for exited tickers only ─────────────────
    log.info(
        "Fetching current prices for %d ticker(s) via yfinance...", len(exited)
    )
    market_data: dict = {}
    try:
        if len(exited) == 1:
            # yf.download with a single ticker returns a plain DataFrame
            raw = yf.download(exited[0], period="5d", auto_adjust=True, progress=False)
            close_series = raw["Close"].dropna() if "Close" in raw.columns else pd.Series([], dtype=float)
            if not close_series.empty:
                market_data[exited[0]] = {"current_price": round(float(close_series.iloc[-1]), 4)}
        else:
            raw = yf.download(
                exited,
                period="5d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            close_df = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
            for t in exited:
                if t in close_df.columns:
                    series = close_df[t].dropna()
                    if not series.empty:
                        market_data[t] = {"current_price": round(float(series.iloc[-1]), 4)}

        for t in exited:
            if t in market_data:
                log.info("  %-12s current price: %.4f", t, market_data[t]["current_price"])
            else:
                log.warning("  %-12s — no price data returned by yfinance", t)

    except Exception as exc:
        log.error("yfinance fetch failed: %s", exc)

    # ── Step 5: Build regret tracker ──────────────────────────────────────────
    log.info("Running build_regret_tracker()...")
    entries = build_regret_tracker(parsed, current_tickers, market_data)

    # ── Step 6: Print results table ───────────────────────────────────────────
    W = 74
    print()
    print("=" * W)
    print("  REGRET TRACKER RESULTS")
    print("=" * W)

    if not entries:
        print("  No entries.")
        print()
        print("  Diagnostic checklist:")
        print(f"  • Are there SELL orders since {_CUTOFF}?  (see 'recent_sells' count above)")
        print("  • Did get_exited_tickers() return any tickers?  (see log above)")
        print("  • Did yfinance return prices for those tickers?  (see log above)")
        print("  • build_regret_tracker() skips entries with sell_price <= 0")
        print("    or missing current_price — check for 'skipping' warnings above")
    else:
        hdr = f"  {'Ticker':<10}  {'Company':<28}  {'Sold':<10}  {'Sell £':>8}  {'Now £':>8}  {'%Δ':>7}"
        print(hdr)
        print("  " + "-" * (W - 2))
        for e in entries:
            pct  = e["pct_diff"]
            sign = "+" if pct >= 0 else ""
            flag = "  ← regret" if pct > 10 else ("  ← good exit" if pct < -10 else "")
            print(
                f"  {e['ticker']:<10}  {e['company_name'][:27]:<28}  "
                f"{e['sell_date']:<10}  {e['sell_price']:>8.4f}  "
                f"{e['current_price']:>8.4f}  {sign}{pct:>6.1f}%{flag}"
            )

    print("=" * W)
    print(f"  {len(entries)} entry/entries  |  "
          f"{len(recent_sells)} SELL orders since {_CUTOFF}  |  "
          f"{len(exited)} ticker(s) priced")
    print("=" * W)
    print()


if __name__ == "__main__":
    main()
