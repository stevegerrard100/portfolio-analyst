"""Assembles final index.html from analysis data and template."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).parent / "template.html"

_SECTOR_COLORS = [
    "#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
    "#79c0ff", "#56d364", "#e3b341", "#ffa657", "#ff7b72",
    "#a5d6ff", "#7ee787", "#ffa8a8", "#c9d1d9",
]


def render_dashboard(
    analysis: dict,
    portfolio: dict | None = None,
    market_data: dict | None = None,
    screener: dict | None = None,
    output_path: str = "output/index.html",
) -> None:
    """
    Render the full dashboard HTML.

    Args:
        analysis:     Output of run_analysis()
        portfolio:    Output of fetch_portfolio()
        market_data:  {ticker: {mansfield_rs, stop_loss, macd_bullish, above_sma50, dist_52w_high}}
        screener:     Output of run_screener()
        output_path:  Destination path for index.html
    """
    portfolio   = portfolio   or {}
    market_data = market_data or {}
    screener    = screener    or {}

    account   = portfolio.get("account", {})
    positions = portfolio.get("positions", [])

    # --- Account totals ---
    total      = float(account.get("total", 0))
    unrealised = float(account.get("unrealized_ppl", 0))
    realised   = float(account.get("realized_ppl", 0))
    cost_basis = total - unrealised
    pct_gain   = (unrealised / cost_basis * 100) if cost_basis > 0 else 0.0

    # --- Sector allocation for donut chart ---
    sector_totals: dict[str, float] = {}
    for p in positions:
        s = p.get("sector", "Other")
        sector_totals[s] = sector_totals.get(s, 0.0) + float(p.get("market_value", 0))

    sectors_sorted = sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)
    sector_data = [
        {
            "label": label,
            "value": round(value, 2),
            "color": _SECTOR_COLORS[i % len(_SECTOR_COLORS)],
        }
        for i, (label, value) in enumerate(sectors_sorted)
    ]

    # --- Holdings: merge positions + analysis + market_data ---
    analysis_map = {h["ticker"]: h for h in analysis.get("holdings_analysis", [])}

    holdings = []
    for pos in sorted(positions, key=lambda p: float(p.get("market_value", 0)), reverse=True):
        t    = pos["ticker"]
        mkt  = market_data.get(t, {})
        hdg  = analysis_map.get(t, {})
        holdings.append({
            "ticker":        t,
            "signal":        hdg.get("signal", "HOLD"),
            "analysis":      hdg.get("analysis", ""),
            "sector":        pos.get("sector", "?"),
            "holding_type":  pos.get("holding_type", "medium"),
            "pnl_pct":       round(float(pos.get("pnl_pct", 0)), 1),
            "ppl":           round(float(pos.get("ppl", 0)), 0),
            "market_value":  round(float(pos.get("market_value", 0)), 0),
            "quantity":      pos.get("quantity", 0),
            "avg_price":     pos.get("averagePrice", 0),
            "current_price": pos.get("currentPrice", 0),
            # Optional technical fields — None when market_data not yet fetched
            "mansfield_rs":  mkt.get("mansfield_rs"),
            "above_sma50":   mkt.get("above_sma50"),
            "macd_bullish":  mkt.get("macd_bullish"),
            "stop_loss":     mkt.get("stop_loss"),
            "dist_52w_high": mkt.get("dist_52w_high"),
        })

    # --- Screener top 10 ---
    screener_candidates = []
    for c in screener.get("candidates", [])[:10]:
        rg = c.get("revenue_growth_pct")
        screener_candidates.append({
            "ticker":             c["ticker"],
            "company_name":       c.get("company_name") or c["ticker"],
            "sector":             c.get("sector", "?"),
            "composite_score":    round(float(c.get("composite_score", 0)), 1),
            "mansfield_rs":       round(float(c.get("mansfield_rs", 0)), 1),
            "revenue_growth_pct": round(float(rg), 1) if rg is not None else None,
        })

    # --- Meta ---
    gen_at = analysis.get("generated_at", datetime.now().isoformat())
    try:
        now = datetime.fromisoformat(gen_at)
    except ValueError:
        now = datetime.now()

    data = {
        "meta": {
            "generated_at": gen_at,
            "date_str":     now.strftime(f"%A, {now.day} %B %Y"),
            "time_str":     now.strftime("%H:%M"),
            "model":        analysis.get("model", "claude-sonnet-4-6"),
        },
        "verdict":          analysis.get("verdict", ""),
        "macro_narrative":  analysis.get("macro_narrative", ""),
        "sector_narrative": analysis.get("sector_narrative", ""),
        "opportunities":    analysis.get("opportunities", ""),
        "account": {
            "total":          round(total, 2),
            "unrealized_ppl": round(unrealised, 2),
            "realized_ppl":   round(realised, 2),
            "currency":       account.get("currency", "GBP"),
            "pct_gain":       round(pct_gain, 1),
            "n_positions":    len(positions),
        },
        "sectors":              sector_data,
        "holdings":             holdings,
        "screener_candidates":  screener_candidates,
    }

    template = _TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("{{DASHBOARD_DATA}}", json.dumps(data, indent=2, ensure_ascii=False))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("Dashboard written → %s  (%d holdings, %d screener picks)",
             output_path, len(holdings), len(screener_candidates))
