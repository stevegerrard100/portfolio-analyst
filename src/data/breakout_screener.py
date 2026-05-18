"""
Breakout Watch List screener — S&P 500 universe.

Identifies stocks forming accumulation bases before a potential breakout.
Five signals, each scored 0/1 (max score = 5):

  1. Stage 1→2 transition — RS crossed above 0 from below in last 4 weeks,
     confirming with price stabilisation or ATR contraction.
  2. VCP (Volatility Contraction Pattern) — progressively smaller price swings
     with declining volume over rolling 20-day windows.
  3. Volume accumulation — multiple high-volume up-days during a flat base.
  4. RS line new high before price — RS near its 52-week high while price is
     still more than 5% below its own 52-week high.
  5. Pivot proximity — price within 2-8% below a clear resistance level.

Cache: 8h TTL (same policy as the growth screener).
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from src.data.market_data import (
    _add_indicators,
    mansfield_rs,
    _ohlcv_daily_to_json,
    _ohlcv_weekly_to_json,
    _mrs_daily_to_json,
    _mrs_weekly_to_json,
)
from src.data.screener import fetch_sp500_tickers, _load_cik_map

log = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
BREAKOUT_CACHE = CACHE_DIR / "breakout_screener.json"
BREAKOUT_CACHE_HOURS = 8


def _calc_stop_loss(price: float, atr: float | None) -> float | None:
    if not atr or not price:
        return None
    return round(price - 2.5 * atr, 4)  # medium holding multiplier


# ---------------------------------------------------------------------------
# Signal 1: Stage 1→2 transition
# ---------------------------------------------------------------------------

def _check_stage_12_transition(rs_series: pd.Series) -> bool:
    """RS crossed from negative (< -3) to above 0 within the last 5 weekly bars."""
    if len(rs_series) < 8:
        return False
    recent = rs_series.iloc[-5:]
    crossed = any(prev < 0 <= curr for prev, curr in zip(recent.iloc[:-1], recent.iloc[1:]))
    if not crossed:
        return False
    # Confirm it was meaningfully negative before the cross (not just noise)
    prior = rs_series.iloc[-12:-5]
    return any(v < -3 for v in prior)


def _check_price_stabilising(weekly_close: pd.Series) -> bool:
    """Price high-low range in last 8 weeks < 80% of range in prior 8 weeks."""
    if len(weekly_close) < 16:
        return False
    recent_rng = float(weekly_close.iloc[-8:].max() - weekly_close.iloc[-8:].min())
    prior_rng  = float(weekly_close.iloc[-16:-8].max() - weekly_close.iloc[-16:-8].min())
    return prior_rng > 0 and recent_rng < prior_rng * 0.80


def _check_atr_contracting(df_daily: pd.DataFrame) -> bool:
    """Recent 20-day ATR mean < 80% of the preceding 6-month ATR mean."""
    atr = df_daily["atr_14"].dropna()
    if len(atr) < 126:
        return False
    recent = float(atr.iloc[-20:].mean())
    older  = float(atr.iloc[-126:-20].mean())
    return older > 0 and recent < older * 0.80


# ---------------------------------------------------------------------------
# Signal 2: VCP (Volatility Contraction Pattern)
# ---------------------------------------------------------------------------

def _check_vcp(df_daily: pd.DataFrame) -> bool:
    """
    Divide the last 80 trading days into four 20-day windows.
    VCP fires when swing size (high-low%) and average volume both decline
    monotonically across all four windows.
    """
    if len(df_daily) < 80:
        return False
    df = df_daily.tail(80)
    swing_sizes: list[float] = []
    vol_avgs:    list[float] = []
    for start in range(0, 80, 20):
        w = df.iloc[start:start + 20]
        if len(w) < 15:
            continue
        lo = float(w["Low"].min())
        if lo <= 0:
            continue
        swing_sizes.append((float(w["High"].max()) - lo) / lo)
        vol_avgs.append(float(w["Volume"].mean()))

    if len(swing_sizes) < 3:
        return False

    sizes_contracting = all(
        swing_sizes[i] < swing_sizes[i - 1] * 0.80
        for i in range(1, len(swing_sizes))
    )
    vol_declining = all(
        vol_avgs[i] < vol_avgs[i - 1] * 0.92
        for i in range(1, len(vol_avgs))
    )
    return sizes_contracting and vol_declining


# ---------------------------------------------------------------------------
# Signal 3: Volume accumulation
# ---------------------------------------------------------------------------

def _check_volume_accumulation(df_daily: pd.DataFrame) -> bool:
    """
    In the last 20 days, ≥3 up-days have volume ≥1.5× the 40-day average,
    while the stock is in a flat base (price range ≤15% over 20 days).
    """
    if len(df_daily) < 30:
        return False
    last20 = df_daily.tail(20)
    lo20   = float(last20["Close"].min())
    if lo20 <= 0:
        return False
    if (float(last20["Close"].max()) - lo20) / lo20 > 0.15:
        return False  # not a flat base

    vol_avg = float(df_daily.tail(40)["Volume"].mean())
    up_with_vol = sum(
        1 for _, row in last20.iterrows()
        if row["Close"] > row["Open"] and row["Volume"] >= vol_avg * 1.5
    )
    return up_with_vol >= 3


# ---------------------------------------------------------------------------
# Signal 4: RS line new high before price
# ---------------------------------------------------------------------------

def _check_rs_new_high(rs_series: pd.Series, weekly_close: pd.Series) -> bool:
    """
    RS is within 8% of its 52-week high while price is more than 5% below
    its own 52-week high — RS leading indicator of an upcoming breakout.
    """
    if len(rs_series) < 26 or len(weekly_close) < 26:
        return False

    rs_window   = rs_series.iloc[-52:]   if len(rs_series)   >= 52 else rs_series
    price_window = weekly_close.iloc[-52:] if len(weekly_close) >= 52 else weekly_close

    rs_52w_high    = float(rs_window.max())
    current_rs     = float(rs_series.iloc[-1])
    price_52w_high = float(price_window.max())
    current_price  = float(weekly_close.iloc[-1])

    if rs_52w_high <= 0 or current_rs <= 0 or price_52w_high <= 0:
        return False

    rs_pct_from_high    = (current_rs    / rs_52w_high    - 1) * 100
    price_pct_from_high = (current_price / price_52w_high - 1) * 100
    return rs_pct_from_high > -8 and price_pct_from_high < -5


# ---------------------------------------------------------------------------
# Volume profile (HVN/LVN — support and resistance identification)
# ---------------------------------------------------------------------------

def _volume_profile(df_daily: pd.DataFrame, bins: int = 40) -> dict:
    """
    Build a 1-year volume profile using numpy histogram on typical price.

    Each day's volume is assigned to the bin containing its typical price
    ((High + Low + Close) / 3), giving a distribution of trading activity
    across the price range.

    Returns:
        poc:         float — price of the highest-volume bin (Point of Control)
        supports:    [(price, vol_M)] HVN peaks below current price, closest first
        resistances: [(price, vol_M)] HVN peaks above current price, closest first
    """
    df = df_daily.tail(252).copy()
    if len(df) < 30:
        return {"poc": None, "supports": [], "resistances": []}

    current_price = float(df["Close"].iloc[-1])
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    if hi <= lo:
        return {"poc": None, "supports": [], "resistances": []}

    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    bin_edges = np.linspace(lo, hi, bins + 1)
    bin_vol, _ = np.histogram(typical, bins=bin_edges, weights=df["Volume"])
    bin_mid = (bin_edges[:-1] + bin_edges[1:]) / 2

    poc_idx = int(np.argmax(bin_vol))
    poc = round(float(bin_mid[poc_idx]), 2)

    # HVN = local peaks at or above the 65th percentile of non-zero volume bins
    nonzero = bin_vol[bin_vol > 0]
    if len(nonzero) == 0:
        return {"poc": poc, "supports": [], "resistances": []}
    threshold = float(np.percentile(nonzero, 65))

    hvns: list[tuple[float, float]] = []
    for i in range(1, bins - 1):
        if (bin_vol[i] >= threshold
                and bin_vol[i] >= bin_vol[i - 1]
                and bin_vol[i] >= bin_vol[i + 1]):
            hvns.append((round(float(bin_mid[i]), 2), float(bin_vol[i])))

    cp = current_price
    supports    = sorted([h for h in hvns if h[0] < cp * 0.995], key=lambda x: x[0], reverse=True)[:3]
    resistances = sorted([h for h in hvns if h[0] > cp * 1.005], key=lambda x: x[0])[:3]

    to_m = lambda v: round(v / 1_000_000, 1)
    return {
        "poc":         poc,
        "supports":    [(p, to_m(v)) for p, v in supports],
        "resistances": [(p, to_m(v)) for p, v in resistances],
    }


# ---------------------------------------------------------------------------
# Signal 5: Pivot proximity (enhanced with volume profile)
# ---------------------------------------------------------------------------

def _check_pivot_proximity(df_daily: pd.DataFrame) -> bool:
    """Price is within 2-8% below its 52-week high — approaching but not at resistance."""
    close    = df_daily["Close"]
    window   = close.iloc[-252:] if len(close) >= 252 else close
    high_52w = float(window.max())
    current  = float(close.iloc[-1])
    if high_52w <= 0:
        return False
    dist = (current / high_52w - 1) * 100
    return -8 <= dist < -1


def _check_pivot_proximity_vp(df_daily: pd.DataFrame, vp: dict) -> bool:
    """
    Price is within 2-8% below a HVN resistance level from the volume profile,
    OR within 2-8% below the 52-week high (fallback if no HVN resistances found).
    """
    current_price = float(df_daily["Close"].iloc[-1])
    for res_price, _ in vp.get("resistances", []):
        dist = (current_price / res_price - 1) * 100
        if -8 <= dist < -0.5:
            return True
    return _check_pivot_proximity(df_daily)


# ---------------------------------------------------------------------------
# Per-ticker deep profile (daily download + indicators)
# ---------------------------------------------------------------------------

def _breakout_profile(ticker: str) -> dict | None:
    """Download 2y daily OHLCV, compute indicators + volume profile. Returns None on failure."""
    try:
        df = yf.Ticker(ticker).history(period="2y")
        if df.empty or len(df) < 60:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df = _add_indicators(df)

        price   = round(float(df["Close"].iloc[-1]), 2)
        atr_val = df["atr_14"].iloc[-1]
        atr     = round(float(atr_val), 4) if not pd.isna(atr_val) else None
        vp      = _volume_profile(df)

        info = yf.Ticker(ticker).info or {}
        return {
            "df":             df,
            "price":          price,
            "atr_14":         atr,
            "volume_profile": vp,
            "ohlcv_daily":    _ohlcv_daily_to_json(df),
            "ohlcv_weekly":   _ohlcv_weekly_to_json(df),
            "company_name":   info.get("longName") or info.get("shortName") or ticker,
            "sector":         info.get("sector") or "?",
        }
    except Exception as exc:
        log.debug("Breakout profile failed for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

def _breakout_reasoning(current_rs: float, signals: dict[str, bool], vp: dict) -> str:
    """Plain-English explanation of which signals fired, enriched with volume profile levels."""
    parts: list[str] = []

    if current_rs >= 0:
        parts.append(
            f"Mansfield RS of {current_rs:+.1f} — this stock has just crossed into positive "
            f"relative strength territory, outperforming the S&P 500 over the past year."
        )
    elif current_rs >= -10:
        parts.append(
            f"Mansfield RS of {current_rs:+.1f} — relative strength is turning up from "
            f"below the zero line, a classic early accumulation signal."
        )
    else:
        parts.append(
            f"Mansfield RS of {current_rs:+.1f} — still underperforming the index, "
            f"but showing signs of early-stage accumulation."
        )

    resistances = vp.get("resistances", [])
    supports    = vp.get("supports", [])

    # Volume profile context line — inserted after the RS intro if space allows
    if resistances and supports:
        vp_line = (
            f"Volume profile shows key resistance at ${resistances[0][0]:.2f} "
            f"and support at ${supports[0][0]:.2f}."
        )
    elif resistances:
        vp_line = f"Volume profile shows key resistance at ${resistances[0][0]:.2f}."
    elif supports:
        vp_line = f"Volume profile shows key support at ${supports[0][0]:.2f}."
    else:
        vp_line = None

    if signals.get("stage_transition"):
        parts.append(
            "The RS line crossed above zero from below in the last four weeks — "
            "a Stage 1→2 transition signal, indicating fresh institutional interest."
        )
    if signals.get("vcp"):
        parts.append(
            "A Volatility Contraction Pattern is forming: each successive price swing "
            "is tighter than the last with declining volume — the coiling action that "
            "often precedes a powerful breakout."
        )
    if signals.get("volume_accumulation"):
        parts.append(
            "Volume accumulation is visible: multiple up-days in the current base "
            "show above-average volume, consistent with quiet institutional buying."
        )
    if signals.get("rs_new_high"):
        parts.append(
            "The RS line is near its 52-week high while price has not yet recovered "
            "its own prior high — this divergence typically resolves with a breakout."
        )
    if signals.get("pivot_proximity"):
        if resistances:
            parts.append(
                f"The stock is approaching the ${resistances[0][0]:.2f} high-volume resistance "
                f"node — a strong-volume close above this level would confirm the breakout."
            )
        else:
            parts.append(
                "The stock is within striking distance of a prior resistance level — "
                "a strong-volume close above this pivot would confirm the breakout."
            )

    # Insert the volume profile line as sentence 2 if we have fewer than 3 parts
    if vp_line and len(parts) < 3:
        parts.insert(1, vp_line)

    return " ".join(parts[:3])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_breakout_screener(
    exclude_tickers: list[str] | None = None,
    max_candidates: int = 15,
    force_refresh: bool = False,
) -> dict:
    """
    Scan S&P 500 for breakout setups. Returns top max_candidates ranked by signal count.

    Returns dict with keys:
        candidates       list of candidate dicts (with chart data + signals)
        screened_at      ISO timestamp
        universe_size    tickers in S&P 500 (excl. portfolio)
        initial_count    after weekly signal pre-filter
        qualified_count  after daily signal check + CIK dedup
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if not force_refresh and BREAKOUT_CACHE.exists():
        try:
            age_h = (
                datetime.now() - datetime.fromtimestamp(BREAKOUT_CACHE.stat().st_mtime)
            ).total_seconds() / 3600
            if age_h < BREAKOUT_CACHE_HOURS:
                log.info("Using cached breakout screener (%.1fh old)", age_h)
                with open(BREAKOUT_CACHE) as f:
                    return json.load(f)
        except Exception:
            pass

    exclude = set(exclude_tickers or [])

    # ── Step 1: S&P 500 universe ──────────────────────────────────────────────
    sp500 = fetch_sp500_tickers()
    if not sp500:
        return {"candidates": [], "error": "Could not fetch S&P 500 universe"}

    universe = [t for t in sp500 if t not in exclude]
    log.info("Breakout screener: %d tickers in universe", len(universe))

    # ── Step 2: SPY weekly benchmark ─────────────────────────────────────────
    spy_raw = yf.Ticker("SPY").history(period="3y", interval="1wk")
    if spy_raw.empty:
        return {"candidates": [], "error": "Could not fetch SPY weekly data"}
    if spy_raw.index.tz is not None:
        spy_raw.index = spy_raw.index.tz_localize(None)
    spy_weekly = spy_raw["Close"].dropna()

    # ── Step 3: Batch weekly download ────────────────────────────────────────
    log.info("Downloading 2y weekly prices for %d tickers...", len(universe))
    try:
        raw = yf.download(
            universe,
            period="2y",
            interval="1wk",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        weekly_close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
    except Exception as exc:
        log.error("Batch weekly download failed: %s", exc)
        return {"candidates": [], "error": str(exc)}

    # ── Step 4: Weekly signal pre-filter ─────────────────────────────────────
    log.info("Scanning weekly signals...")
    initial_candidates: list[dict] = []

    for ticker in universe:
        if ticker not in weekly_close.columns:
            continue
        series = weekly_close[ticker].dropna()
        if len(series) < 60:
            continue
        try:
            aligned_spy = spy_weekly.reindex(series.index, method="ffill")
            rs_series   = mansfield_rs(series, aligned_spy).dropna()
            if rs_series.empty:
                continue
            current_rs = float(rs_series.iloc[-1])

            # Focus on the accumulation zone — not confirmed uptrends (RS > 40)
            # and not deep downtrends (RS < -30) that need more time
            if current_rs < -30 or current_rs > 40:
                continue

            s1   = _check_stage_12_transition(rs_series)
            s4   = _check_rs_new_high(rs_series, series)
            stab = _check_price_stabilising(series)

            # Price distance from 52-week high (proxy for pivot proximity)
            price_52w = float(series.iloc[-52:].max()) if len(series) >= 52 else float(series.max())
            price_pct  = (float(series.iloc[-1]) / price_52w - 1) * 100 if price_52w > 0 else -100
            s5_hint    = -10 <= price_pct < -1

            initial_score = int(s1) + int(s4) + int(s5_hint)
            if initial_score < 1:
                continue

            initial_candidates.append({
                "ticker":           ticker,
                "current_rs":       current_rs,
                "rs_series":        rs_series,
                "weekly_close":     series,
                "s1_stage":         s1,
                "s_price_stab":     stab,
                "s4_rs_new_high":   s4,
                "s5_pivot_hint":    s5_hint,
                "initial_score":    initial_score,
            })
        except Exception as exc:
            log.debug("Weekly scan error for %s: %s", ticker, exc)
            continue

    log.info("Weekly pre-filter: %d candidates with ≥1 signal", len(initial_candidates))

    # Sort by initial score, take top 60 for daily analysis
    initial_candidates.sort(
        key=lambda x: (x["initial_score"], x["current_rs"]), reverse=True
    )
    top_for_daily = initial_candidates[:60]

    # ── Step 5: Daily analysis ────────────────────────────────────────────────
    log.info("Fetching daily data for %d candidates...", len(top_for_daily))
    final_candidates: list[dict] = []

    for c in top_for_daily:
        ticker = c["ticker"]
        try:
            profile = _breakout_profile(ticker)
            if profile is None:
                continue

            df = profile["df"]
            vp = profile["volume_profile"]

            s2_vcp     = _check_vcp(df)
            s3_vol_acc = _check_volume_accumulation(df)
            s5_pivot   = _check_pivot_proximity_vp(df, vp)

            # Stage 1→2 confirms if RS crossed AND (price stabilising OR ATR contracting)
            s_price_stab = c["s_price_stab"]
            s_atr_contr  = _check_atr_contracting(df)
            s1_final     = c["s1_stage"] and (s_price_stab or s_atr_contr)

            signals = {
                "stage_transition":    s1_final,
                "vcp":                 s2_vcp,
                "volume_accumulation": s3_vol_acc,
                "rs_new_high":         c["s4_rs_new_high"],
                "pivot_proximity":     s5_pivot,
            }
            total_score = sum(1 for v in signals.values() if v)
            if total_score == 0:
                continue

            rs_series       = c["rs_series"]
            mrs_weekly_data = _mrs_weekly_to_json(rs_series)
            mrs_daily_data  = _mrs_daily_to_json(rs_series)

            final_candidates.append({
                "ticker":          ticker,
                "company_name":    profile["company_name"],
                "sector":          profile["sector"],
                "mansfield_rs":    round(c["current_rs"], 1),
                "composite_score": total_score,
                "reasoning":       _breakout_reasoning(c["current_rs"], signals, vp),
                "signals":         [k for k, v in signals.items() if v],
                "stop_loss":       _calc_stop_loss(profile["price"], profile["atr_14"]),
                "ohlcv_daily":     profile["ohlcv_daily"],
                "ohlcv_weekly":    profile["ohlcv_weekly"],
                "mrs_daily":       mrs_daily_data,
                "mrs_weekly":      mrs_weekly_data,
            })
            time.sleep(0.3)

        except Exception as exc:
            log.debug("Daily analysis failed for %s: %s", ticker, exc)
            continue

    log.info("Breakout screener: %d qualified candidates", len(final_candidates))

    # ── CIK dedup (same policy as growth screener) ────────────────────────────
    cik_map   = _load_cik_map()
    seen_ciks: set[str] = set()
    deduped:   list[dict] = []
    for c in sorted(final_candidates, key=lambda x: (x["composite_score"], x["mansfield_rs"]), reverse=True):
        cik = cik_map.get(c["ticker"].upper())
        if cik:
            if cik in seen_ciks:
                continue
            seen_ciks.add(cik)
        deduped.append(c)

    top = deduped[:max_candidates]

    result = {
        "candidates":     top,
        "screened_at":    datetime.now().isoformat(),
        "universe_size":  len(universe),
        "initial_count":  len(initial_candidates),
        "qualified_count":len(deduped),
    }

    try:
        with open(BREAKOUT_CACHE, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info("Breakout screener cached: %d candidates", len(top))
    except Exception as exc:
        log.warning("Breakout cache write failed: %s", exc)

    return result
