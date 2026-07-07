"""Sector performance and rotation: Finviz + SPDR ETF Mansfield RS."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
SECTOR_LEADERS_CACHE = CACHE_DIR / "sector_leaders.json"
SECTOR_LEADERS_CACHE_HOURS = 24

# Screener filter for sector leaders: Finviz's cumulative "Large" market cap
# bucket ($10B+, not restricted to S&P 500 constituents), US-listed, common
# stock only ("Stocks only (ex-Funds)" excludes ETFs/closed-end funds —
# without it, leveraged/derivative ETFs still show up tagged with a regular
# GICS sector), and above-average relative volume so the 1-week movers below
# are volume-confirmed rather than thin/illiquid spikes.
FINVIZ_SECTOR_LEADER_FILTER = {
    "Market Cap.": "+Large (over $10bln)",
    "Country": "USA",
    "Industry": "Stocks only (ex-Funds)",
    "Relative Volume": "Over 1",
}

# Pacing between per-sector Finviz calls, and backoff/retry on a 429 response.
# Finviz rate-limits aggressively; back-to-back calls across 11 sectors were
# seeing most sectors fail with 429 in production.
_SECTOR_LEADER_SLEEP_SECONDS = 1.5
_SECTOR_LEADER_429_BACKOFF_SECONDS = 5
_SECTOR_LEADER_MIN_SUCCESSFUL_SECTORS = 6  # of 11 — below this, don't cache

# The 11 Finviz sector filter values (finvizfinance.constants filter_dict["Sector"]).
# These are also the exact strings Finviz's group Performance() screener returns
# in its "sector" column, so they double as the keys used by _sector_heatmap()
# in renderer.py and the sector_leaders dict below.
FINVIZ_SECTORS = [
    "Basic Materials", "Communication Services", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Financial", "Healthcare", "Industrials",
    "Real Estate", "Technology", "Utilities",
]

# Cap-size buckets mirror the ranges used in high_growth_screener.py's universe
# fetch (Small $300M-$2B, Mid $2B-$10B); anything above that is "large".
_SECTOR_LEADER_SMALL_CAP_MAX = 2_000_000_000
_SECTOR_LEADER_MID_CAP_MAX = 10_000_000_000

SPDR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB", "XLC"]
COMMODITY_PROXIES = ["GLD", "GDX", "SLV", "XME", "USO"]

# Maps our sector label strings to their SPDR ETF proxy
SECTOR_TO_ETF = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Communication Services": "XLC",
    "Commodities": "GLD",
}


# ---------------------------------------------------------------------------
# Finviz sector performance
# ---------------------------------------------------------------------------

def _parse_perf_col(series: pd.Series) -> pd.Series:
    """
    Finviz mixes formats: 'Perf Week' → '-3.50%' string (already pct),
    other perf cols → -0.0363 float (decimal fraction needing * 100).
    Detect by whether the raw value contains '%'.
    """
    def _convert(val):
        s = str(val).strip()
        if "%" in s:
            return round(pd.to_numeric(s.replace("%", "").strip(), errors="coerce"), 2)
        num = pd.to_numeric(s, errors="coerce")
        if pd.isna(num):
            return None
        return round(num * 100, 2)
    return series.apply(_convert)


def fetch_sector_performance() -> list[dict]:
    """
    Fetch sector performance table from Finviz.
    Returns list of dicts with keys: sector, change_1d, change_1w, change_1m,
    change_3m, change_6m, change_1y.
    """
    try:
        from finvizfinance.group.performance import Performance

        df = Performance().screener_view()
        log.info("Finviz columns: %s", list(df.columns))

        # Finviz actual columns: Name, Perf Week, Perf Month, Perf Quart,
        # Perf Half, Perf Year, Perf YTD, Change (1d), Volume, Avg Volume, Rel Volume
        col_map = {
            "Name": "sector",
            "Change": "change_1d",
            "Perf Week": "change_1w",
            "Perf Month": "change_1m",
            "Perf Quart": "change_3m",
            "Perf Half": "change_6m",
            "Perf Year": "change_1y",
        }
        df = df.rename(columns=col_map)

        perf_cols = ["change_1d", "change_1w", "change_1m", "change_3m", "change_6m", "change_1y"]
        for col in perf_cols:
            if col not in df.columns:
                df[col] = None
            else:
                df[col] = _parse_perf_col(df[col])

        if "sector" not in df.columns:
            df["sector"] = df.iloc[:, 0]

        return df[["sector"] + perf_cols].dropna(subset=["sector"]).to_dict("records")

    except Exception as exc:
        log.error("Finviz sector performance fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# ETF Mansfield RS extraction (from pre-fetched market data)
# ---------------------------------------------------------------------------

def extract_etf_rs(market_data: dict) -> dict[str, dict]:
    """
    Pull Mansfield RS and rotation signals for SPDR ETFs and commodity proxies
    from the pre-fetched market_data dict (avoids duplicate API calls).
    """
    all_tracked = SPDR_ETFS + COMMODITY_PROXIES
    results: dict[str, dict] = {}

    for ticker in all_tracked:
        data = market_data.get(ticker)
        if not data:
            continue

        rs = data["mansfield_rs"]
        rs_5d = data.get("rs_5d", 0.0)
        rs_20d = data.get("rs_20d", 0.0)
        rs_60d = data.get("rs_60d", 0.0)
        direction = data["mansfield_rs_direction"]

        results[ticker] = {
            "mansfield_rs": rs,
            "positive": rs > 0,
            "direction": direction,
            "rs_5d": rs_5d,
            "rs_20d": rs_20d,
            "rs_60d": rs_60d,
            # Rotation signals
            "early_rotation": rs_5d > 0 and rs_20d < 0,
            "momentum_building": rs_5d > rs_20d > rs_60d,
            "rotation_peaking": rs_5d < 0 < rs_20d,
        }

    return results


# ---------------------------------------------------------------------------
# Portfolio alignment
# ---------------------------------------------------------------------------

def compute_portfolio_alignment(
    holdings: list[dict],
    etf_rs: dict[str, dict],
) -> dict:
    """
    Returns alignment score and per-holding sector status.
    holdings: list of dicts with at least 'ticker' and 'sector' keys.
    """
    positive_count = 0
    total_with_sector = 0
    per_holding = []

    for h in holdings:
        sector = h.get("sector", "")
        etf = SECTOR_TO_ETF.get(sector)
        rs_data = etf_rs.get(etf) if etf else None

        sector_positive = rs_data["positive"] if rs_data else None
        per_holding.append({
            "ticker": h["ticker"],
            "sector": sector,
            "sector_etf": etf,
            "sector_rs_positive": sector_positive,
        })

        if rs_data is not None:
            total_with_sector += 1
            if rs_data["positive"]:
                positive_count += 1

    alignment_pct = (
        round((positive_count / total_with_sector) * 100, 1)
        if total_with_sector > 0
        else 50.0
    )

    return {
        "alignment_pct": alignment_pct,
        "positive_count": positive_count,
        "total_with_sector": total_with_sector,
        "per_holding": per_holding,
    }


# ---------------------------------------------------------------------------
# Sector leaders — top 5 stocks per sector by 1-week performance
# ---------------------------------------------------------------------------

def _market_cap_bucket(market_cap) -> str:
    """Small / mid / large bucket from a raw Finviz market cap float."""
    if market_cap is None or pd.isna(market_cap):
        return "large"
    if market_cap < _SECTOR_LEADER_SMALL_CAP_MAX:
        return "small"
    if market_cap < _SECTOR_LEADER_MID_CAP_MAX:
        return "mid"
    return "large"


def _screener_view_with_429_retry(screener, **kwargs):
    """
    Call screener.screener_view(**kwargs), retrying once after a longer backoff
    if Finviz responds with 429 Too Many Requests. Any other HTTP error (or a
    second 429) propagates to the caller.
    """
    try:
        return screener.screener_view(**kwargs)
    except requests.exceptions.HTTPError as exc:
        if "429" not in str(exc):
            raise
        log.warning(
            "Finviz 429 rate limit — backing off %ss before one retry",
            _SECTOR_LEADER_429_BACKOFF_SECONDS,
        )
        time.sleep(_SECTOR_LEADER_429_BACKOFF_SECONDS)
        return screener.screener_view(**kwargs)


def _fetch_leaders_for_sector(sector_name: str) -> list[dict]:
    """
    Top 5 large-cap, volume-confirmed stocks in a single Finviz sector by
    1-week performance.

    Two lightweight single-page requests: the stock-level Performance screener
    sorted by "Performance (Week)" descending (limit=5, so Finviz's own sort +
    pagination does the ranking work) over the large-cap/above-average-volume
    pool, then an Overview lookup scoped to just those 5 tickers for company
    name and market cap.
    """
    from finvizfinance.screener.overview import Overview
    from finvizfinance.screener.performance import Performance as StockPerformance

    filters = {"Sector": sector_name, **FINVIZ_SECTOR_LEADER_FILTER}

    perf = StockPerformance()
    perf.set_filter(filters_dict=filters)
    perf_df = _screener_view_with_429_retry(
        perf, order="Performance (Week)", ascend=False, limit=5, verbose=0
    )
    if perf_df is None or perf_df.empty:
        return []

    tickers = perf_df["Ticker"].dropna().tolist()
    if not tickers:
        return []

    overview = Overview()
    overview.set_filter(ticker=",".join(tickers))
    ov_df = _screener_view_with_429_retry(overview, verbose=0)
    if ov_df is None or ov_df.empty:
        return []

    merged = perf_df.merge(ov_df[["Ticker", "Company", "Market Cap"]], on="Ticker", how="inner")
    merged["perf_1w"] = _parse_perf_col(merged["Perf Week"])
    merged = merged.dropna(subset=["perf_1w"]).sort_values("perf_1w", ascending=False)

    leaders = []
    for _, row in merged.head(5).iterrows():
        leaders.append({
            "ticker":            row["Ticker"],
            "company_name":      row.get("Company", ""),
            "perf_1w":           float(row["perf_1w"]),
            "market_cap_bucket": _market_cap_bucket(row.get("Market Cap")),
        })
    return leaders


def fetch_sector_leaders() -> dict[str, list[dict]]:
    """
    Top 5 large-cap, volume-confirmed stocks by 1-week performance for each of
    the 11 Finviz sectors.

    Portfolio holdings are NOT excluded — this is a sector shortlist, not a
    screener candidate list. Cached for 24 hours (cache/sector_leaders.json).
    A sector whose fetch fails gets [] rather than aborting the whole call.
    If fewer than _SECTOR_LEADER_MIN_SUCCESSFUL_SECTORS sectors returned any
    data (e.g. a burst of 429s), the result is NOT cached — writing a mostly-
    empty payload would otherwise lock in near-total failure for 24 hours.
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if SECTOR_LEADERS_CACHE.exists():
        try:
            age_h = (
                datetime.now() - datetime.fromtimestamp(SECTOR_LEADERS_CACHE.stat().st_mtime)
            ).total_seconds() / 3600
            if age_h < SECTOR_LEADERS_CACHE_HOURS:
                cached = json.loads(SECTOR_LEADERS_CACHE.read_text(encoding="utf-8"))
                log.info("Sector leaders: using cached result (%.1fh old)", age_h)
                return cached
        except Exception:
            pass

    log.info("Sector leaders: fetching top stocks per sector from Finviz...")
    leaders: dict[str, list[dict]] = {}
    for sector_name in FINVIZ_SECTORS:
        try:
            leaders[sector_name] = _fetch_leaders_for_sector(sector_name)
        except Exception as exc:
            log.warning("Sector leaders fetch failed for %s: %s", sector_name, exc)
            leaders[sector_name] = []
        time.sleep(_SECTOR_LEADER_SLEEP_SECONDS)

    successful = sum(1 for v in leaders.values() if v)
    if successful < _SECTOR_LEADER_MIN_SUCCESSFUL_SECTORS:
        log.warning(
            "Sector leaders: only %d/%d sectors returned data — skipping cache "
            "write so the next run retries fresh instead of being stuck with a "
            "mostly-empty 24h cache",
            successful, len(FINVIZ_SECTORS),
        )
        return leaders

    try:
        SECTOR_LEADERS_CACHE.write_text(json.dumps(leaders), encoding="utf-8")
        log.info("Sector leaders cached: %d/%d sectors with data", successful, len(leaders))
    except Exception as exc:
        log.warning("Sector leaders cache write failed: %s", exc)

    return leaders


# ---------------------------------------------------------------------------
# Main entry point for this module
# ---------------------------------------------------------------------------

def fetch_sector_data(market_data: dict, holdings: list[dict]) -> dict:
    """
    Assemble all sector data from pre-fetched market data + Finviz.
    Returns a unified sector context dict consumed by the renderer and Claude.
    """
    log.info("Fetching Finviz sector performance...")
    finviz_perf = fetch_sector_performance()

    log.info("Extracting ETF Mansfield RS from market data...")
    etf_rs = extract_etf_rs(market_data)

    log.info("Computing portfolio alignment score...")
    alignment = compute_portfolio_alignment(holdings, etf_rs)

    log.info("Fetching sector leaders (top stocks per sector)...")
    sector_leaders = fetch_sector_leaders()

    # Identify early rotation signals across all tracked ETFs
    rotation_signals = []
    for ticker, rs in etf_rs.items():
        if rs["early_rotation"]:
            rotation_signals.append({"ticker": ticker, "signal": "early_rotation"})
        elif rs["momentum_building"]:
            rotation_signals.append({"ticker": ticker, "signal": "momentum_building"})
        elif rs["rotation_peaking"]:
            rotation_signals.append({"ticker": ticker, "signal": "rotation_peaking"})

    return {
        "finviz_performance": finviz_perf,
        "etf_rs": etf_rs,
        "alignment": alignment,
        "rotation_signals": rotation_signals,
        "sector_leaders": sector_leaders,
    }
