"""Assembles final index.html from analysis data and template."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

_CACHE_DIR    = Path(__file__).parent / ".." / ".." / "cache"
_SECTORS_JSON = Path(__file__).parent / ".." / ".." / "config" / "sectors.json"

log = logging.getLogger(__name__)


def _load_ticker_to_sector() -> dict[str, str]:
    try:
        return json.loads(_SECTORS_JSON.read_text(encoding="utf-8")).get("ticker_to_sector", {})
    except Exception:
        return {}


def _load_ticker_to_holding_type() -> dict[str, str]:
    try:
        return json.loads(_SECTORS_JSON.read_text(encoding="utf-8")).get("ticker_to_holding_type", {})
    except Exception:
        return {}


_TICKER_TO_SECTOR: dict[str, str] = _load_ticker_to_sector()
_TICKER_TO_HOLDING_TYPE: dict[str, str] = _load_ticker_to_holding_type()

# Map config holding_type values → two-class classification
# long_term/etf → long_term_core; short_term → trading; medium → let Claude decide
_HOLDING_TYPE_CLASS: dict[str, str | None] = {
    "long_term":  "long_term_core",
    "etf":        "long_term_core",
    "short_term": "trading",
    "medium":     None,  # deferred to Claude
}


def _resolve_holding_class(ticker: str, sector: str, claude_class: str | None) -> str:
    """
    Determine the two-class holding classification for a ticker.

    Priority:
    1. ETF sectors → always long_term_core
    2. config/sectors.json ticker_to_holding_type with definitive mapping
    3. Claude's classification (from analyse_holdings)
    4. Default: trading
    """
    # ETFs always core
    if sector and ("ETF" in sector.upper() or sector.upper() == "ETF"):
        return "long_term_core"
    config_type = _TICKER_TO_HOLDING_TYPE.get(ticker)
    if config_type:
        mapped = _HOLDING_TYPE_CLASS.get(config_type)
        if mapped is not None:
            return mapped
    # Fall through to Claude's classification or default
    return claude_class or "trading"

_TEMPLATE = Path(__file__).parent / "template.html"

_ACTION_COLORS = {
    "sell":       "red",
    "trim":       "red",
    "add":        "green",
    "buy":        "green",
    "danger":     "red",
    "raise_stop": "amber",
}

_SECTOR_COLORS = [
    "#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
    "#79c0ff", "#56d364", "#e3b341", "#ffa657", "#ff7b72",
    "#a5d6ff", "#7ee787", "#ffa8a8", "#c9d1d9",
]

# ATR fallback multipliers — used when no support level passes the floor check,
# and for screener/breakout candidates (which have no holding_class).
_STOP_MULT = {"long_term": 3.0, "medium": 2.5, "short_term": 1.5}
_ATR_FALLBACK_MULT = {"long_term_core": 3.0, "trading": 2.5}


def _calc_stop_loss(current_price: float, atr: float | None, holding_type: str) -> float | None:
    """ATR-multiplier stop for screener/breakout candidates (no class data available)."""
    mult = _STOP_MULT.get(holding_type)
    if mult is None or not atr or not current_price:
        return None
    return round(current_price - mult * atr, 4)


def _calc_smart_stop_loss(
    current_price: float,
    atr: float | None,
    holding_class: str,
    sma_50: float | None,
    sma_200: float | None,
    base_low_26w: float | None,
) -> float | None:
    """
    Technically-informed stop loss anchored to meaningful support levels.

    Step 1: Collect candidate support levels that are below current price.
    Step 2: Walk through preferred order for holding_class; for each level,
            place stop at level − 0.5×ATR and verify it is at least 1×ATR
            below current price (floor check).  Accept the first level that passes.
    Step 3: If all levels fail, fall back to ATR multiplier (3.0× for
            long_term_core, 2.5× for trading).
    Step 4: Return None if current_price ≤ 0 or the fallback also produces
            an invalid value.

    ETFs must be excluded by the caller (pass holding_type check first).
    """
    if not current_price or current_price <= 0:
        return None
    if not atr or atr <= 0:
        # No ATR → no valid stop; fall straight through
        return None

    def _below_price(v: float | None) -> bool:
        return bool(v and v > 0 and v < current_price)

    # Preference order per classification
    if holding_class == "long_term_core":
        ordered = [sma_200, sma_50, base_low_26w]
    else:  # trading (and any unknown class)
        ordered = [sma_50, base_low_26w]

    # Walk through levels; accept first that passes the ATR floor check.
    # "At least 1× ATR below current price" means  stop ≤ current_price − ATR,
    # i.e. candidate ≤ floor.  A candidate above the floor is too close.
    for level in ordered:
        if not _below_price(level):
            continue
        candidate = level - 0.5 * atr          # buffer below the support level
        floor     = current_price - atr         # must be at or below this
        if candidate <= floor and candidate > 0:
            return round(candidate, 4)

    # Fallback — pure ATR multiplier (backward-compatible)
    mult     = _ATR_FALLBACK_MULT.get(holding_class, 2.5)
    fallback = current_price - mult * atr
    if fallback > 0:
        return round(fallback, 4)

    log.warning("_calc_smart_stop_loss: could not produce valid stop for price=%.4f atr=%.4f class=%s",
                current_price, atr, holding_class)
    return None


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


def _build_today_actions(raw_actions: list[dict], market_data: dict) -> list[dict]:
    """Enrich raw actions with color, is_new, and current_price."""
    # Load yesterday's action IDs for is_new badge
    yesterday_ids: set[str] = set()
    last_path = _CACHE_DIR / "last_actions.json"
    if last_path.exists():
        try:
            yesterday_ids = {a["id"] for a in json.loads(last_path.read_text(encoding="utf-8")) if a.get("id")}
        except Exception:
            pass

    result = []
    for a in raw_actions:
        action_id = a.get("id", "")
        ticker    = a.get("ticker", "")
        mkt       = market_data.get(ticker, {})
        result.append({
            **a,
            "color":         _ACTION_COLORS.get(a.get("action_type", ""), "amber"),
            "is_new":        action_id not in yesterday_ids,
            "current_price": mkt.get("current_price"),
        })
    return result


def _sector_rs_signal(ticker: str, candidate_sector: str, sector_flows: dict) -> str:
    """
    Return 'leading', 'neutral', or 'lagging' for a breakout candidate based on
    the Mansfield RS of its sector's SPDR ETF proxy.

    Lookup order for sector name:
      1. config/sectors.json ticker_to_sector mapping
      2. sector field already on the candidate dict
      3. Falls back to 'Unknown'

    SECTOR_TO_ETF is imported from src.data.sector_flows — not duplicated here (R5.2).
    """
    from src.data.sector_flows import SECTOR_TO_ETF

    sector = _TICKER_TO_SECTOR.get(ticker) or candidate_sector or "Unknown"
    if not sector or sector in ("Unknown", "ETF", "?"):
        return "neutral"

    etf = SECTOR_TO_ETF.get(sector)
    if not etf:
        return "neutral"

    etf_data = sector_flows.get("etf_rs", {}).get(etf)
    if not etf_data:
        return "neutral"

    # Rotation signals → leading; negative RS → lagging; otherwise neutral
    if (etf_data.get("early_rotation")
            or etf_data.get("momentum_building")
            or etf_data.get("rotation_peaking")):
        return "leading"
    if etf_data.get("mansfield_rs", 0) < 0:
        return "lagging"
    return "neutral"


def render_dashboard(
    analysis: dict,
    portfolio: dict | None = None,
    market_data: dict | None = None,
    screener: dict | None = None,
    breakout: dict | None = None,
    macro: dict | None = None,
    sector_flows: dict | None = None,
    regret_tracker: list | None = None,
    stop_levels: dict | None = None,
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
    log.info("NETLIFY_DISMISS_URL: %s", os.environ.get("NETLIFY_DISMISS_URL", "NOT SET"))
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
    _stop_levels = stop_levels or {}

    holdings = []
    for pos in sorted(positions, key=lambda p: float(p.get("market_value", 0)), reverse=True):
        t    = pos["ticker"]
        mkt  = market_data.get(t, {})
        hdg  = analysis_map.get(t, {})
        holding_type   = pos.get("holding_type", "medium")
        current_price  = float(pos.get("currentPrice") or mkt.get("current_price") or 0)
        atr            = mkt.get("atr_14")
        sector         = pos.get("sector", "?")

        # Holding classification: config overrides → Claude → default
        claude_class  = hdg.get("holding_class")
        holding_class = _resolve_holding_class(t, sector, claude_class)

        # Stop loss — smart (support-anchored) for equities; skip for ETFs
        if holding_type == "etf":
            stop_loss = None
        else:
            stop_loss = _calc_smart_stop_loss(
                current_price, atr, holding_class,
                mkt.get("sma_50"), mkt.get("sma_200"), mkt.get("base_low_26w"),
            )

        # Stop level memory: stored level and change from previous
        sl_entry        = _stop_levels.get(t, {})
        stored_stop     = sl_entry.get("level")
        prev_stop       = sl_entry.get("prev_level")
        if stored_stop and prev_stop:
            stop_change_pct = round((stored_stop - prev_stop) / prev_stop * 100, 1)
        else:
            stop_change_pct = None

        holdings.append({
            "ticker":          t,
            "signal":          hdg.get("signal", "HOLD"),
            "analysis":        hdg.get("analysis", ""),
            "sector":          sector,
            "holding_type":    holding_type,
            "holding_class":   holding_class,
            "pie_name":        pos.get("pie_name"),
            "pnl_pct":         round(float(pos.get("pnl_pct", 0)), 1),
            "ppl":             round(float(pos.get("ppl", 0)), 0),
            "market_value":    round(float(pos.get("market_value", 0)), 0),
            "quantity":        pos.get("quantity", 0),
            "avg_price":       pos.get("averagePrice", 0),
            "current_price":   current_price,
            # Technical indicators (None when market_data not yet fetched)
            "mansfield_rs":    mkt.get("mansfield_rs"),
            "above_sma50":     mkt.get("above_sma50"),
            "macd_bullish":    mkt.get("macd_bullish"),
            "stop_loss":       stop_loss,
            "dist_52w_high":   mkt.get("dist_52w_high"),
            # Stop level memory
            "stored_stop":     stored_stop,
            "stop_change_pct": stop_change_pct,
            # Chart data for TradingView Lightweight Charts
            "ohlcv_daily":     mkt.get("ohlcv_daily"),
            "ohlcv_weekly":    mkt.get("ohlcv_weekly"),
            "mrs_daily":       mkt.get("mrs_daily"),
            "mrs_weekly":      mkt.get("mrs_weekly"),
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
    # high_conviction and composite_score are now computed in breakout_screener.py (R9/R10)
    # regime is surfaced at the result level for the regime warning banner (R8)
    breakout_regime = breakout.get("regime", "bull")
    breakout_candidates = []
    for c in breakout.get("candidates", [])[:15]:
        signals = c.get("signals", [])
        ticker  = c["ticker"]
        sector  = c.get("sector", "?")
        breakout_candidates.append({
            "ticker":           ticker,
            "company_name":     c.get("company_name") or ticker,
            "sector":           sector,
            "mansfield_rs":     round(float(c.get("mansfield_rs", 0)), 1),
            "composite_score":  c.get("composite_score", 0),
            "score_delta":      c.get("score_delta"),
            "reasoning":        c.get("reasoning", ""),
            "signals":          signals,
            "high_conviction":  bool(c.get("high_conviction", False)),
            "regime_watchlist": bool(c.get("regime_watchlist", False)),
            "earnings_soon":    bool(c.get("earnings_soon", False)),
            "earnings_date":    c.get("earnings_date"),
            "sector_rs_signal": _sector_rs_signal(ticker, sector, sector_flows),
            # R6.3: structured AI reasoning fields; setup_strength=None triggers fallback (R6.5)
            "setup_strength":   c.get("setup_strength"),
            "key_risk":         c.get("key_risk"),
            "maturity":         c.get("maturity"),
            "base_weeks":       c.get("base_weeks"),
            "base_depth_pct":   c.get("base_depth_pct"),
            "base_tightness":   c.get("base_tightness"),
            "stop_loss":        c.get("stop_loss"),
            "ohlcv_daily":      c.get("ohlcv_daily"),
            "ohlcv_weekly":     c.get("ohlcv_weekly"),
            "mrs_daily":        c.get("mrs_daily"),
            "mrs_weekly":       c.get("mrs_weekly"),
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
        "breakout_regime":      breakout_regime,
        "macro_pills":          _macro_pills(macro),
        "sector_heatmap":       _sector_heatmap(sector_flows),
        "today_actions":        _build_today_actions(analysis.get("actions", []), market_data or {}),
        "dismiss_url":          os.environ.get("NETLIFY_DISMISS_URL", ""),
    }

    data["regret_tracker"] = regret_tracker or []

    template = _TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("{{DASHBOARD_DATA}}", json.dumps(data, indent=2, ensure_ascii=False))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info(
        "Dashboard written → %s  (%d holdings, %d screener picks, %d breakout picks, %d regret entries)",
        output_path, len(holdings), len(screener_candidates), len(breakout_candidates),
        len(regret_tracker or []),
    )
