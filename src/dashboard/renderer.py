"""Assembles final index.html from analysis data and template."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).parent / "template.html"

_ACTION_COLORS = {
    "sell":   "red",
    "trim":   "red",
    "watch":  "amber",
    "add":    "green",
    "macro":  "amber",
    "sector": "amber",
}

_SECTOR_COLORS = [
    "#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
    "#79c0ff", "#56d364", "#e3b341", "#ffa657", "#ff7b72",
    "#a5d6ff", "#7ee787", "#ffa8a8", "#c9d1d9",
]

# ATR multipliers for stop loss: current_price - (multiplier × ATR14)
_STOP_MULT = {"long_term": 3.0, "medium": 2.5, "short_term": 1.5}


def _calc_stop_loss(current_price: float, atr: float | None, holding_type: str) -> float | None:
    mult = _STOP_MULT.get(holding_type)
    if mult is None or not atr or not current_price:
        return None
    return round(current_price - mult * atr, 4)


def _macro_pills(macro: dict) -> list[dict]:
    """Build colour-coded pill dicts from macro series data."""
    raw = macro.get("series", {})
    pills = []

    def pill(label: str, value: str, status: str) -> dict:
        return {"label": label, "value": value, "status": status}

    if "treasury_10y" in raw:
        v = raw["treasury_10y"]["current"]
        pills.append(pill("10yr Yield", f"{v:.2f}%",
                          "green" if v < 3.5 else ("amber" if v < 5.0 else "red")))

    if "treasury_2y" in raw:
        v = raw["treasury_2y"]["current"]
        pills.append(pill("2yr Yield", f"{v:.2f}%",
                          "green" if v < 4.0 else ("amber" if v < 5.0 else "red")))

    yc = macro.get("yield_curve", {})
    if yc:
        bps = yc.get("spread_bps", 0)
        st  = yc.get("status", "flat")
        sign = "+" if bps >= 0 else ""
        pills.append(pill("Yield Curve", f"{sign}{bps:.0f} bps",
                          "green" if st == "positive" else ("amber" if st == "flat" else "red")))

    hy_bps = macro.get("hy_spread_bps")
    if hy_bps is not None:
        pills.append(pill("HY Spread", f"{hy_bps:.0f} bps",
                          "green" if hy_bps < 300 else ("amber" if hy_bps <= 500 else "red")))

    if "fed_funds" in raw:
        v = raw["fed_funds"]["current"]
        pills.append(pill("Fed Funds", f"{v:.2f}%",
                          "green" if v < 3.0 else ("amber" if v < 5.0 else "red")))

    if "vix" in raw:
        v = raw["vix"]["current"]
        pills.append(pill("VIX", f"{v:.1f}",
                          "green" if v < 15 else ("amber" if v < 25 else "red")))

    if "cpi" in raw:
        cpi   = raw["cpi"]
        prior = cpi.get("prior_12m", 0)
        if prior > 0:
            yoy = round((cpi["current"] / prior - 1) * 100, 1)
            pills.append(pill("CPI YoY", f"{yoy:.1f}%",
                              "green" if yoy < 2.5 else ("amber" if yoy < 4.0 else "red")))

    return pills


def _sector_heatmap(sector_flows: dict) -> list[dict]:
    """Build sorted sector heatmap rows from Finviz weekly performance."""
    rows = []
    for row in sector_flows.get("finviz_performance", []):
        chg = row.get("change_1w")
        if chg is None:
            continue
        rows.append({"sector": row.get("sector", ""), "change_1w": round(float(chg), 2)})
    rows.sort(key=lambda x: x["change_1w"], reverse=True)
    return rows


def render_dashboard(
    analysis: dict,
    portfolio: dict | None = None,
    market_data: dict | None = None,
    screener: dict | None = None,
    breakout: dict | None = None,
    macro: dict | None = None,
    sector_flows: dict | None = None,
    output_path: str = "output/index.html",
) -> None:
    """
    Render the full dashboard HTML.

    Args:
        analysis:      Output of run_analysis()
        portfolio:     Output of fetch_portfolio()
        market_data:   {ticker: {mansfield_rs, stop_loss, macd_bullish, above_sma50, dist_52w_high}}
        screener:      Output of run_screener()
        breakout:      Output of run_breakout_screener()
        macro:         Output of fetch_macro_data()
        sector_flows:  Output of fetch_sector_data()
        output_path:   Destination path for index.html
    """
    portfolio    = portfolio    or {}
    market_data  = market_data  or {}
    screener     = screener     or {}
    breakout     = breakout     or {}
    macro        = macro        or {}
    sector_flows = sector_flows or {}

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
        holding_type   = pos.get("holding_type", "medium")
        current_price  = float(pos.get("currentPrice") or mkt.get("current_price") or 0)
        atr            = mkt.get("atr_14")
        stop_loss      = _calc_stop_loss(current_price, atr, holding_type)

        holdings.append({
            "ticker":        t,
            "signal":        hdg.get("signal", "HOLD"),
            "analysis":      hdg.get("analysis", ""),
            "sector":        pos.get("sector", "?"),
            "holding_type":  holding_type,
            "pie_name":      pos.get("pie_name"),
            "pnl_pct":       round(float(pos.get("pnl_pct", 0)), 1),
            "ppl":           round(float(pos.get("ppl", 0)), 0),
            "market_value":  round(float(pos.get("market_value", 0)), 0),
            "quantity":      pos.get("quantity", 0),
            "avg_price":     pos.get("averagePrice", 0),
            "current_price": current_price,
            # Technical indicators (None when market_data not yet fetched)
            "mansfield_rs":  mkt.get("mansfield_rs"),
            "above_sma50":   mkt.get("above_sma50"),
            "macd_bullish":  mkt.get("macd_bullish"),
            "stop_loss":     stop_loss,
            "dist_52w_high": mkt.get("dist_52w_high"),
            # Chart data for TradingView Lightweight Charts
            "ohlcv_daily":   mkt.get("ohlcv_daily"),
            "ohlcv_weekly":  mkt.get("ohlcv_weekly"),
            "mrs_daily":     mkt.get("mrs_daily"),
            "mrs_weekly":    mkt.get("mrs_weekly"),
        })

    # --- Screener top 10 ---
    screener_candidates = []
    for c in screener.get("candidates", [])[:10]:
        rg = c.get("revenue_growth_pct")
        price = float(c.get("price") or c.get("current_price") or 0)
        atr   = c.get("atr_14")
        screener_candidates.append({
            "ticker":             c["ticker"],
            "company_name":       c.get("company_name") or c["ticker"],
            "sector":             c.get("sector", "?"),
            "composite_score":    round(float(c.get("composite_score", 0)), 1),
            "mansfield_rs":       round(float(c.get("mansfield_rs", 0)), 1),
            "revenue_growth_pct": round(float(rg), 1) if rg is not None else None,
            "reasoning":          c.get("reasoning", ""),
            "stop_loss":          _calc_stop_loss(price, atr, "medium"),
            "ohlcv_daily":        c.get("ohlcv_daily"),
            "ohlcv_weekly":       c.get("ohlcv_weekly"),
            "mrs_daily":          c.get("mrs_daily"),
            "mrs_weekly":         c.get("mrs_weekly"),
        })

    # --- Breakout Watch List (top 15) ---
    breakout_candidates = []
    for c in breakout.get("candidates", [])[:15]:
        signals = c.get("signals", [])
        high_conviction = (
            "stage_transition" in signals
            and ("vcp" in signals or "volume_accumulation" in signals)
        )
        breakout_candidates.append({
            "ticker":          c["ticker"],
            "company_name":    c.get("company_name") or c["ticker"],
            "sector":          c.get("sector", "?"),
            "mansfield_rs":    round(float(c.get("mansfield_rs", 0)), 1),
            "composite_score": int(c.get("composite_score", 0)),
            "reasoning":       c.get("reasoning", ""),
            "signals":         signals,
            "high_conviction": high_conviction,
            "stop_loss":       c.get("stop_loss"),
            "ohlcv_daily":     c.get("ohlcv_daily"),
            "ohlcv_weekly":    c.get("ohlcv_weekly"),
            "mrs_daily":       c.get("mrs_daily"),
            "mrs_weekly":      c.get("mrs_weekly"),
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
        "breakout_candidates":  breakout_candidates,
        "macro_pills":          _macro_pills(macro),
        "sector_heatmap":       _sector_heatmap(sector_flows),
        "today_actions":        [
            {**a, "color": _ACTION_COLORS.get(a.get("action_type", ""), "amber")}
            for a in analysis.get("actions", [])
        ],
    }

    template = _TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("{{DASHBOARD_DATA}}", json.dumps(data, indent=2, ensure_ascii=False))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("Dashboard written → %s  (%d holdings, %d screener picks, %d breakout picks)",
             output_path, len(holdings), len(screener_candidates), len(breakout_candidates))
