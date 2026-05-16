"""Sector performance and rotation: Finviz + SPDR ETF Mansfield RS."""

import logging

import pandas as pd

log = logging.getLogger(__name__)

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
    }
