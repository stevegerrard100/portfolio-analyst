"""Entry point — orchestrates the full analysis pipeline.

Step sequence:
  1. fetch_portfolio        Trading 212 (pies + direct positions)
  2. fetch_market_data      yfinance OHLCV + Mansfield RS for all tickers
  3. fetch_sector_data      Finviz sector perf + ETF rotation signals
  4. fetch_macro_data       FRED rates, spreads, yield curve
  5. fetch_all_fundamentals Finnhub + yfinance per holding
  6. run_screener           S&P 500 growth screen (8h cached)
  7. run_analysis           Claude 5-prompt pipeline
  8. render_dashboard       Static HTML → output/index.html
"""

import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "output")
_OUTPUT_HTML = os.path.join(_OUTPUT_DIR, "index.html")


def _step(n: int, total: int, label: str) -> None:
    log.info("━━━ Step %d/%d — %s", n, total, label)


def main() -> None:
    t0 = time.time()
    log.info("━" * 60)
    log.info("  Portfolio Analyst — full pipeline starting")
    log.info("━" * 60)

    os.makedirs(_OUTPUT_DIR, exist_ok=True)

    # ── 1. Portfolio ──────────────────────────────────────────────────────────
    _step(1, 8, "Trading 212 portfolio")
    from src.data.trading212 import fetch_portfolio
    portfolio = fetch_portfolio()
    positions = portfolio.get("positions", [])
    portfolio_tickers = [p["ticker"] for p in positions]
    log.info("Portfolio: %d positions (env=%s)", len(positions), portfolio.get("environment", "?"))

    # ── 2. Market data ────────────────────────────────────────────────────────
    _step(2, 8, "Market data (yfinance + Mansfield RS)")
    from src.data.market_data import fetch_market_data, SPDR_ETFS, COMMODITY_PROXIES
    # Always include SPDR ETFs and commodity proxies for sector rotation signals
    extra = [t for t in (SPDR_ETFS + COMMODITY_PROXIES) if t not in portfolio_tickers]
    all_tickers = portfolio_tickers + extra
    market_data = fetch_market_data(all_tickers)
    log.info("Market data: %d/%d tickers processed", len(market_data), len(all_tickers))

    # ── 3. Sector flows ───────────────────────────────────────────────────────
    _step(3, 8, "Sector rotation (Finviz + ETF RS)")
    from src.data.sector_flows import fetch_sector_data
    sector_flows = fetch_sector_data(market_data, positions)
    n_signals = len(sector_flows.get("rotation_signals", []))
    log.info("Sector data: %d rotation signals, alignment=%.0f%%",
             n_signals, sector_flows.get("alignment", {}).get("alignment_pct", 0))

    # ── 4. Macro ──────────────────────────────────────────────────────────────
    _step(4, 8, "FRED macro data")
    from src.data.macro import fetch_macro_data
    macro = fetch_macro_data()
    if macro:
        yc = macro.get("yield_curve", {})
        log.info("Macro: HY regime=%s, yield curve=%s (%s bps), rates=%s",
                 macro.get("hy_regime", "?"),
                 yc.get("status", "?"),
                 yc.get("spread_bps", "?"),
                 macro.get("rate_trajectory", "?"))

    # ── 5. Fundamentals ───────────────────────────────────────────────────────
    _step(5, 8, "Fundamentals (Finnhub + yfinance)")
    from src.data.fundamentals import fetch_all_fundamentals
    fundamentals = fetch_all_fundamentals(portfolio_tickers)
    log.info("Fundamentals: %d/%d tickers", len(fundamentals), len(portfolio_tickers))

    # ── 6. Screener ───────────────────────────────────────────────────────────
    _step(6, 8, "S&P 500 growth screener (8h cached)")
    from src.data.screener import run_screener
    screener = run_screener(exclude_tickers=portfolio_tickers)
    log.info("Screener: %d candidates (universe=%d)",
             len(screener.get("candidates", [])),
             screener.get("universe_size", 0))

    # ── 7. Claude analysis ────────────────────────────────────────────────────
    _step(7, 8, "Claude analysis (5-prompt pipeline)")
    from src.analysis.claude_analyst import run_analysis
    analysis = run_analysis(
        portfolio=portfolio,
        market_data=market_data,
        fundamentals=fundamentals,
        macro=macro,
        sector_flows=sector_flows,
        screener=screener,
    )
    log.info("Analysis complete: %d holdings analysed",
             len(analysis.get("holdings_analysis", [])))

    # ── 8. Render ─────────────────────────────────────────────────────────────
    _step(8, 8, "Rendering dashboard → output/index.html")
    from src.dashboard.renderer import render_dashboard
    render_dashboard(
        analysis=analysis,
        portfolio=portfolio,
        market_data=market_data,
        screener=screener,
        output_path=_OUTPUT_HTML,
    )

    elapsed = time.time() - t0
    log.info("━" * 60)
    log.info("  Done in %.1f min — %s", elapsed / 60, _OUTPUT_HTML)
    log.info("━" * 60)


if __name__ == "__main__":
    main()
