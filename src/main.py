"""Entry point — orchestrates the full analysis pipeline.

Step sequence:
  1. fetch_portfolio        Trading 212 (pies + direct positions)
  2. fetch_market_data      yfinance OHLCV + Mansfield RS for all tickers
  3. fetch_sector_data      Finviz sector perf + ETF rotation signals
  4. fetch_macro_data       FRED rates, spreads, yield curve
  5. fetch_all_fundamentals Finnhub + yfinance per holding
  6. run_screener           S&P 500 growth screen (8h cached)
  7. run_breakout_screener  S&P 500 breakout/accumulation screen (8h cached)
  8. run_analysis           Claude 5-prompt pipeline
  9. render_dashboard       Static HTML → output/index.html
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "output")
_OUTPUT_HTML = os.path.join(_OUTPUT_DIR, "index.html")
_CACHE_DIR   = Path(__file__).parent / ".." / "cache"


def _step(n: int, total: int, label: str) -> None:
    log.info("━━━ Step %d/%d — %s", n, total, label)


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Analyst pipeline")
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Skip live fetches (steps 1-7) and load data from cache files. "
            "For local development only — never use in CI."
        ),
    )
    args = parser.parse_args()
    fast = args.fast

    t0 = time.time()

    if fast:
        print(
            "\n⚡ FAST MODE — using cached data, skipping live fetches. "
            "This is for local development only and should never be used in CI.\n",
            file=sys.stderr,
        )
        log.info("━" * 60)
        log.info("  Portfolio Analyst — FAST MODE (steps 1-7 skipped)")
        log.info("━" * 60)
    else:
        log.info("━" * 60)
        log.info("  Portfolio Analyst — full pipeline starting")
        log.info("━" * 60)

    os.makedirs(_OUTPUT_DIR, exist_ok=True)

    from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES

    if fast:
        # ── Fast mode: load steps 1-7 data from cache ────────────────────────
        _required = {
            "portfolio":  _CACHE_DIR / "last_portfolio.json",
            "screener":   _CACHE_DIR / "screener.json",
            "breakout":   _CACHE_DIR / "breakout_screener.json",
        }
        missing = [str(p) for name, p in _required.items() if not p.exists()]
        if missing:
            log.error(
                "FAST MODE: missing cache file(s): %s — run without --fast first",
                missing,
            )
            sys.exit(1)

        log.info("Fast mode: loading portfolio from %s", _required["portfolio"])
        portfolio = json.loads(_required["portfolio"].read_text(encoding="utf-8"))

        log.info("Fast mode: loading screener from %s", _required["screener"])
        screener = json.loads(_required["screener"].read_text(encoding="utf-8"))

        log.info("Fast mode: loading breakout from %s", _required["breakout"])
        breakout = json.loads(_required["breakout"].read_text(encoding="utf-8"))

        market_data:  dict = {}
        sector_flows: dict = {}
        macro:        dict = {}
        fundamentals: dict = {}
        regret_tracker:     list = []

        positions        = portfolio.get("positions", [])
        portfolio_tickers = [_MERGER_OVERRIDES.get(p["ticker"], p["ticker"]) for p in positions]
        log.info(
            "Fast mode: %d positions, %d screener candidates, %d breakout candidates",
            len(positions),
            len(screener.get("candidates", [])),
            len(breakout.get("candidates", [])),
        )

    else:
        # ── 1. Portfolio ──────────────────────────────────────────────────────
        _step(1, 9, "Trading 212 portfolio")
        from src.data.trading212 import fetch_portfolio
        portfolio = fetch_portfolio()
        positions = portfolio.get("positions", [])
        # Apply merger overrides explicitly — ensures resolved tickers (IONQ, QBTS…) reach
        # all downstream steps even if _enrich_position's override path failed silently.
        portfolio_tickers = [_MERGER_OVERRIDES.get(p["ticker"], p["ticker"]) for p in positions]
        log.info("Portfolio: %d positions (env=%s)", len(positions), portfolio.get("environment", "?"))

        # Persist portfolio for --fast dev runs
        _portfolio_cache = _CACHE_DIR / "last_portfolio.json"
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _portfolio_cache.write_text(
                json.dumps(portfolio, indent=2, default=str), encoding="utf-8"
            )
            log.info("Portfolio cached → %s", _portfolio_cache)
        except Exception as exc:
            log.warning("Could not cache portfolio: %s", exc)

        # Pre-compute exited tickers so their current prices are included in the
        # main yfinance batch (step 2) — avoids a separate fetch in the renderer.
        from src.data.regret_tracker import get_exited_tickers as _get_exited
        _exited_tickers = _get_exited(portfolio.get("order_history", []), set(portfolio_tickers))
        if _exited_tickers:
            log.info("Regret tracker: %d exited ticker(s) to pre-fetch: %s", len(_exited_tickers), _exited_tickers)

        # ── 2. Market data ────────────────────────────────────────────────────
        _step(2, 9, "Market data (yfinance + Mansfield RS)")
        from src.data.market_data import fetch_market_data, SPDR_ETFS, COMMODITY_PROXIES
        # Always include SPDR ETFs, commodity proxies, and exited tickers (for Regret Tracker)
        extra = [t for t in (SPDR_ETFS + COMMODITY_PROXIES + _exited_tickers) if t not in portfolio_tickers]
        all_tickers = portfolio_tickers + extra
        market_data = fetch_market_data(all_tickers)
        log.info("Market data: %d/%d tickers processed", len(market_data), len(all_tickers))

        # ── 3. Sector flows ───────────────────────────────────────────────────
        _step(3, 9, "Sector rotation (Finviz + ETF RS)")
        from src.data.sector_flows import fetch_sector_data
        sector_flows = fetch_sector_data(market_data, positions)
        n_signals = len(sector_flows.get("rotation_signals", []))
        log.info("Sector data: %d rotation signals, alignment=%.0f%%",
                 n_signals, sector_flows.get("alignment", {}).get("alignment_pct", 0))

        # ── 4. Macro ──────────────────────────────────────────────────────────
        _step(4, 9, "FRED macro data")
        from src.data.macro import fetch_macro_data
        macro = fetch_macro_data()
        if macro:
            yc = macro.get("yield_curve", {})
            log.info("Macro: HY regime=%s, yield curve=%s (%s bps), rates=%s",
                     macro.get("hy_regime", "?"),
                     yc.get("status", "?"),
                     yc.get("spread_bps", "?"),
                     macro.get("rate_trajectory", "?"))

        # ── 5. Fundamentals ───────────────────────────────────────────────────
        _step(5, 9, "Fundamentals (Finnhub + yfinance)")
        from src.data.fundamentals import fetch_all_fundamentals
        fundamentals = fetch_all_fundamentals(portfolio_tickers)
        log.info("Fundamentals: %d/%d tickers", len(fundamentals), len(portfolio_tickers))

        # ── 6. Screener ───────────────────────────────────────────────────────
        _step(6, 9, "S&P 500 growth screener (8h cached)")
        from src.data.screener import run_screener
        screener = run_screener(exclude_tickers=portfolio_tickers)
        log.info("Screener: %d candidates (universe=%d)",
                 len(screener.get("candidates", [])),
                 screener.get("universe_size", 0))

        # ── 7. Breakout Watch List ────────────────────────────────────────────
        _step(7, 9, "Breakout Watch List screener (8h cached)")
        from src.data.breakout_screener import run_breakout_screener
        breakout = run_breakout_screener(exclude_tickers=portfolio_tickers)
        log.info("Breakout screener: %d candidates (universe=%d)",
                 len(breakout.get("candidates", [])),
                 breakout.get("universe_size", 0))

        # ── Regret Tracker (uses already-fetched market data) ─────────────────
        from src.data.regret_tracker import build_regret_tracker
        regret_tracker = build_regret_tracker(
            portfolio.get("order_history", []),
            set(portfolio_tickers),
            market_data,
        )

    # ── 8. Claude analysis ────────────────────────────────────────────────────
    _step(8, 9, "Claude analysis (6-prompt pipeline)")

    # Read active dismissals so todays_actions() can filter them
    dismissed_entries: list[dict] = []
    _dismissed_path = _CACHE_DIR / "dismissed_actions.json"
    if _dismissed_path.exists():
        try:
            _all = json.loads(_dismissed_path.read_text(encoding="utf-8"))
            _today = date.today().isoformat()
            dismissed_entries = [e for e in _all if e.get("snoozed_until", "") >= _today]
            log.info("Dismissals: %d active (of %d total) — IDs: %s",
                     len(dismissed_entries), len(_all),
                     [e["id"] for e in dismissed_entries] or "none")
        except Exception as exc:
            log.warning("Could not read dismissed_actions.json: %s", exc)
    else:
        log.info("Dismissals: cache/dismissed_actions.json not found — no active dismissals")

    from src.analysis.claude_analyst import run_analysis
    analysis = run_analysis(
        portfolio=portfolio,
        market_data=market_data,
        fundamentals=fundamentals,
        macro=macro,
        sector_flows=sector_flows,
        screener=screener,
        breakout=breakout,
        dismissed_entries=dismissed_entries,
    )
    log.info("Analysis complete: %d holdings analysed",
             len(analysis.get("holdings_analysis", [])))

    # ── 9. Render ─────────────────────────────────────────────────────────────
    _step(9, 9, "Rendering dashboard → output/index.html")
    from src.dashboard.renderer import render_dashboard
    render_dashboard(
        analysis=analysis,
        portfolio=portfolio,
        market_data=market_data,
        screener=screener,
        breakout=breakout,
        macro=macro,
        sector_flows=sector_flows,
        regret_tracker=regret_tracker,
        output_path=_OUTPUT_HTML,
    )

    # Persist today's actions so tomorrow's run can compute is_new badges
    _last_path = _CACHE_DIR / "last_actions.json"
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _last_actions = [
            {"id": a["id"], "action_type": a["action_type"],
             "priority": a["priority"], "text": a["text"]}
            for a in analysis.get("actions", [])
            if a.get("id")
        ]
        _last_path.write_text(json.dumps(_last_actions, indent=2), encoding="utf-8")
        log.info("Persisted %d actions → %s", len(_last_actions), _last_path)
    except Exception as exc:
        log.warning("Could not write last_actions.json: %s", exc)

    elapsed = time.time() - t0
    log.info("━" * 60)
    log.info("  Done in %.1f min — %s", elapsed / 60, _OUTPUT_HTML)
    log.info("━" * 60)


if __name__ == "__main__":
    main()
