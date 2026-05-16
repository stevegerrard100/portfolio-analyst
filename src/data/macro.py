"""FRED API macro indicators: rates, spreads, yield curve."""

import logging
import os

import pandas as pd
from fredapi import Fred

log = logging.getLogger(__name__)

FRED_SERIES = {
    "hy_spread":    "BAMLH0A0HYM2",  # HY Credit Spread (ICE BofA)
    "treasury_10y": "DGS10",          # 10-year Treasury yield
    "treasury_2y":  "DGS2",           # 2-year Treasury yield
    "yield_spread": "T10Y2Y",         # 10yr minus 2yr
    "vix":          "VIXCLS",         # VIX closing level
    "fed_funds":    "DFF",            # Fed Funds Effective Rate
    "cpi":          "CPIAUCSL",       # CPI (monthly)
}


def _series_snapshot(fred: Fred, name: str, series_id: str) -> dict | None:
    """Fetch a FRED series and return current + prior-period values."""
    try:
        series = fred.get_series(series_id, observation_start="2022-01-01")
        series = series.dropna()
        if series.empty:
            log.warning("Empty series for %s (%s)", name, series_id)
            return None

        current = float(series.iloc[-1])
        last_date = series.index[-1]
        last_updated = last_date.strftime("%Y-%m-%d")

        def _prior_by_date(months_ago: int) -> float:
            cutoff = last_date - pd.DateOffset(months=months_ago)
            past = series[series.index <= cutoff]
            return float(past.iloc[-1]) if not past.empty else current

        prior_3m = _prior_by_date(3)
        prior_12m = _prior_by_date(12)

        return {
            "current": round(current, 4),
            "prior_3m": round(prior_3m, 4),
            "prior_12m": round(prior_12m, 4),
            "change_3m": round(current - prior_3m, 4),
            "change_12m": round(current - prior_12m, 4),
            "last_updated": last_updated,
            "series_id": series_id,
        }
    except Exception as exc:
        log.error("FRED fetch failed for %s (%s): %s", name, series_id, exc)
        return None


def _yield_curve_status(spread_bps: float) -> str:
    if spread_bps > 50:
        return "positive"
    if spread_bps >= 0:
        return "flat"
    return "inverted"


def _hy_regime(spread_bps: float) -> str:
    if spread_bps < 300:
        return "tight"
    if spread_bps <= 500:
        return "normal"
    return "stress"


def _rate_trajectory(fed_funds: dict) -> str:
    change = fed_funds["change_12m"]
    if change < -0.25:
        return "easing"
    if change > 0.25:
        return "tightening"
    return "on hold"


def fetch_macro_data() -> dict:
    """
    Fetch all FRED series and compute derived macro indicators.
    Returns a unified macro context dict consumed by Claude and the renderer.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        log.error("FRED_API_KEY not set")
        return {}

    fred = Fred(api_key=api_key)
    raw: dict[str, dict] = {}

    for name, series_id in FRED_SERIES.items():
        log.info("Fetching FRED series %s (%s)", name, series_id)
        snap = _series_snapshot(fred, name, series_id)
        if snap:
            raw[name] = snap

    # --- Derived indicators ---
    result: dict = {"series": raw}

    if "treasury_10y" in raw and "treasury_2y" in raw:
        spread_pct = raw["treasury_10y"]["current"] - raw["treasury_2y"]["current"]
        spread_bps = round(spread_pct * 100, 1)
        result["yield_curve"] = {
            "spread_bps": spread_bps,
            "status": _yield_curve_status(spread_bps),
        }

    if "hy_spread" in raw:
        result["hy_regime"] = _hy_regime(raw["hy_spread"]["current"] * 100)
        # BAMLH0A0HYM2 is in percentage points (e.g. 3.5 = 350 bps)
        result["hy_spread_bps"] = round(raw["hy_spread"]["current"] * 100, 0)

    if "fed_funds" in raw:
        result["rate_trajectory"] = _rate_trajectory(raw["fed_funds"])

    # Stress flag — triggers opportunity signal downgrade per spec
    hy_bps = result.get("hy_spread_bps", 0)
    result["credit_stress"] = bool(hy_bps > 500)

    log.info(
        "Macro: 10yr=%.2f%%, yield curve=%s, HY=%s bps (%s), rates=%s",
        raw.get("treasury_10y", {}).get("current", 0),
        result.get("yield_curve", {}).get("status", "unknown"),
        hy_bps,
        result.get("hy_regime", "unknown"),
        result.get("rate_trajectory", "unknown"),
    )

    return result
