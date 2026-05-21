"""
Breakout Watch List screener — S&P 500 universe.

Identifies stocks forming accumulation bases before a potential breakout.
Five signals with weighted composite score out of 10:

  1. Stage 1→2 transition — price above rising 150-day SMA + RS crossed above zero (2.0 pts)
  2. VCP (Volatility Contraction Pattern) — progressive swing narrowing with
     volume dry-up in final contraction (1.5 pts)
  3. Volume accumulation — up-day vol / down-day vol ratio ≥ 1.5x over 20 sessions (1.0 pts)
  4. RS leading price — RS near its 52-week high while price still below its own (2.0 pts)
  5. Pivot proximity — price within 5% below to 0.5% above Base Pivot High (1.0 pts)

  Base quality bonus: +0.5 pts when base ≥ 6 weeks with depth ≤ 30%

Market regime: bull / caution / bear (SPY 200d SMA + VIX + HY credit spread).
High Conviction: score ≥ 7.0 AND rs_leading AND stage_transition AND non-bear regime.

Gate sequence (R1): yfinance signals first → Finnhub earnings gate last (saves API quota).

Cache: 8h TTL (same policy as the growth screener).

⚠️  Delete cache/breakout_screener.json before the next run after upgrading —
    the old cache has composite_score /5 and lacks regime / base_stats fields.
"""

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
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
# R2: Base Pivot High
# ---------------------------------------------------------------------------

def _compute_base_pivot_high(weekly_close: pd.Series) -> float | None:
    """
    Highest weekly close within the 6-26 week lookback window.
    This is the pivot/resistance level the stock is basing under.
    """
    n = len(weekly_close)
    if n < 6:
        return None
    window = weekly_close.iloc[-26:] if n >= 26 else weekly_close
    return float(window.max())


# ---------------------------------------------------------------------------
# R11: Base statistics
# ---------------------------------------------------------------------------

def _compute_base_stats(weekly_close: pd.Series, bph: float | None) -> dict:
    """
    Compute base formation statistics surfaced in the Breakout Watch List table.

    Returns:
        base_weeks:      int   — weeks since the BPH was set (base length)
        base_depth_pct:  float — how far price fell from BPH to base low (%)
        base_tightness:  float — weekly close std-dev / mean in the base (lower = tighter)
    """
    n = len(weekly_close)
    if n < 8 or bph is None or bph <= 0:
        return {"base_weeks": None, "base_depth_pct": None, "base_tightness": None}

    window  = weekly_close.iloc[-26:] if n >= 26 else weekly_close
    # Find the week closest to the BPH
    bph_idx = int((window - bph).abs().argmin())
    base_window = window.iloc[bph_idx:]

    base_weeks = len(base_window)
    if base_weeks < 2:
        return {"base_weeks": base_weeks, "base_depth_pct": None, "base_tightness": None}

    base_low       = float(base_window.min())
    base_depth_pct = round((bph - base_low) / bph * 100, 1)

    mean_close     = float(base_window.mean())
    std_close      = float(base_window.std())
    base_tightness = round(std_close / mean_close, 4) if mean_close > 0 else None

    return {
        "base_weeks":     base_weeks,
        "base_depth_pct": base_depth_pct,
        "base_tightness": base_tightness,
    }


# ---------------------------------------------------------------------------
# R4 (signal 1): Stage 1→2 transition — 150-day SMA + positive slope + RS cross
# ---------------------------------------------------------------------------

def _check_stage_transition_150d(df_daily: pd.DataFrame, rs_series: pd.Series) -> bool:
    """
    Stage 1→2 transition (Weinstein):
    - Price above the 150-day SMA (≈ 30-week MA)
    - 150-day SMA has positive slope (today > 20 trading days ago)
    - RS crossed from negative to above 0 within the last 5 weekly bars
    """
    if len(df_daily) < 160:
        return False

    close   = df_daily["Close"]
    sma150  = close.rolling(150).mean()

    current_sma = sma150.iloc[-1]
    prior_sma   = sma150.iloc[-21] if len(sma150) > 21 else sma150.iloc[0]
    current_px  = float(close.iloc[-1])

    if pd.isna(current_sma) or pd.isna(prior_sma):
        return False
    if current_px <= float(current_sma):
        return False
    if float(current_sma) <= float(prior_sma):   # SMA must be rising
        return False

    # RS crossed above 0 in last 5 weekly bars
    if len(rs_series) < 6:
        return False
    recent = rs_series.iloc[-5:]
    return any(prev < 0 <= curr for prev, curr in zip(recent.iloc[:-1], recent.iloc[1:]))


# ---------------------------------------------------------------------------
# R5 (signal 2): VCP with volume dry-up in final contraction
# ---------------------------------------------------------------------------

def _check_vcp_with_volume_dryup(df_daily: pd.DataFrame) -> bool:
    """
    VCP (Volatility Contraction Pattern):
    - Four 20-day windows must show monotonically contracting swing sizes (each ≤ 80% of prior)
    - Average volume must decline across windows (each ≤ 92% of prior)
    - Volume dry-up in final window: 4-day avg ≥ 20% below the 50-day volume SMA
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
    if not (sizes_contracting and vol_declining):
        return False

    # Volume dry-up: 4-day avg must be ≥ 20% below the 50-day volume SMA
    if len(df_daily) < 54:
        return False
    vol_sma50  = df_daily["Volume"].rolling(50).mean().iloc[-1]
    vol_4d_avg = float(df_daily["Volume"].iloc[-4:].mean())
    if pd.isna(vol_sma50) or float(vol_sma50) <= 0:
        return False
    return vol_4d_avg <= float(vol_sma50) * 0.80


# ---------------------------------------------------------------------------
# R6 (signal 3): Volume accumulation ratio
# ---------------------------------------------------------------------------

def _check_volume_accumulation_ratio(df_daily: pd.DataFrame) -> bool:
    """
    Volume accumulation: up-day volume / down-day volume ≥ 1.5x over trailing 20 sessions,
    within a flat base (price range ≤ 15% over 20 days).
    """
    if len(df_daily) < 20:
        return False

    last20 = df_daily.tail(20)
    lo20   = float(last20["Close"].min())
    if lo20 <= 0:
        return False
    if (float(last20["Close"].max()) - lo20) / lo20 > 0.15:
        return False  # not a flat base

    up_vol   = sum(float(r["Volume"]) for _, r in last20.iterrows() if r["Close"] > r["Open"])
    down_vol = sum(float(r["Volume"]) for _, r in last20.iterrows() if r["Close"] < r["Open"])

    if down_vol <= 0:
        return up_vol > 0
    return (up_vol / down_vol) >= 1.5


# ---------------------------------------------------------------------------
# R4 (signal 4): RS leading price (formerly rs_new_high)
# ---------------------------------------------------------------------------

def _check_rs_leading(rs_series: pd.Series, weekly_close: pd.Series) -> bool:
    """
    RS is within 8% of its 52-week high while price is more than 5% below its
    own 52-week high — RS leading price, a classic pre-breakout divergence.
    """
    if len(rs_series) < 26 or len(weekly_close) < 26:
        return False

    rs_window    = rs_series.iloc[-52:]    if len(rs_series)    >= 52 else rs_series
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
# R7 (signal 5): Pivot proximity using Base Pivot High
# ---------------------------------------------------------------------------

def _check_pivot_proximity_bph(df_daily: pd.DataFrame, bph: float | None) -> bool:
    """
    Price is within 5% below to 0.5% above the Base Pivot High (BPH).
    Falls back to the 52-week daily high if BPH is unavailable.
    """
    current = float(df_daily["Close"].iloc[-1])
    pivot   = bph

    if pivot is None or pivot <= 0:
        close  = df_daily["Close"]
        window = close.iloc[-252:] if len(close) >= 252 else close
        pivot  = float(window.max())
    if pivot <= 0:
        return False

    dist = (current / pivot - 1) * 100
    return -5.0 <= dist <= 0.5


# ---------------------------------------------------------------------------
# R8: Market regime
# ---------------------------------------------------------------------------

def _assess_market_regime() -> str:
    """
    Classify market regime as 'bull', 'caution', or 'bear'.

    Inputs:
      - SPY vs 200-day SMA  (bull = above, bear = below)
      - VIX closing level   (extreme = ≥ 35, stress = ≥ 25)
      - HY credit spread    (FRED BAMLH0A0HYM2, bear zone = ≥ 500 bps)

    Returns 'caution' on data errors so the screener keeps running.
    """
    try:
        # SPY 200-day SMA
        spy_daily = yf.Ticker("SPY").history(period="1y")
        if spy_daily.empty or len(spy_daily) < 200:
            log.warning("Regime: insufficient SPY history — defaulting to caution")
            return "caution"
        if spy_daily.index.tz is not None:
            spy_daily.index = spy_daily.index.tz_localize(None)
        spy_close   = spy_daily["Close"]
        sma200      = float(spy_close.rolling(200).mean().iloc[-1])
        current_spy = float(spy_close.iloc[-1])
        spy_above   = current_spy > sma200

        # VIX via yfinance
        vix_data  = yf.Ticker("^VIX").history(period="5d")
        vix_level = float(vix_data["Close"].iloc[-1]) if not vix_data.empty else 20.0

        # HY spread via FRED (optional — key may be absent)
        hy_spread_bps: float | None = None
        fred_key = os.environ.get("FRED_API_KEY")
        if fred_key:
            try:
                from fredapi import Fred
                fred          = Fred(api_key=fred_key)
                hy_series     = fred.get_series("BAMLH0A0HYM2", limit=5)
                hy_spread_bps = float(hy_series.dropna().iloc[-1]) * 100  # pct → bps
            except Exception as exc:
                log.debug("FRED HY spread unavailable: %s", exc)

        log.info(
            "Market regime: SPY=%s 200d SMA (%.1f vs %.1f), VIX=%.1f, HY=%s bps",
            "above" if spy_above else "below",
            current_spy, sma200, vix_level,
            f"{hy_spread_bps:.0f}" if hy_spread_bps is not None else "n/a",
        )

        # bear: SPY below 200d SMA, OR extreme VIX, OR extreme HY spread
        if (not spy_above
                or vix_level >= 35
                or (hy_spread_bps is not None and hy_spread_bps >= 500)):
            return "bear"
        # bull: all conditions clear
        if (spy_above
                and vix_level < 25
                and (hy_spread_bps is None or hy_spread_bps < 400)):
            return "bull"
        return "caution"

    except Exception as exc:
        log.warning("Market regime assessment failed: %s — defaulting to caution", exc)
        return "caution"


# ---------------------------------------------------------------------------
# R3: Finnhub earnings proximity gate (runs last — saves API quota)
# ---------------------------------------------------------------------------

def _check_earnings_proximity_finnhub(ticker: str, client) -> bool:
    """
    Returns True if the ticker has an earnings event within the next 21 calendar days.
    Stocks with imminent earnings are excluded — too binary an event for base setups.
    Returns False on API error (do not exclude on uncertainty).
    """
    today = date.today()
    end   = today + timedelta(days=21)
    try:
        cal      = client.earnings_calendar(
            _from=today.isoformat(),
            to=end.isoformat(),
            symbol=ticker,
        )
        earnings = (cal or {}).get("earningsCalendar", [])
        return len(earnings) > 0
    except Exception as exc:
        log.debug("Finnhub earnings gate failed for %s: %s", ticker, exc)
        return False  # on error, do not exclude


# ---------------------------------------------------------------------------
# R9: Weighted composite score
# ---------------------------------------------------------------------------

_SIGNAL_WEIGHTS: dict[str, float] = {
    "rs_leading":          2.0,
    "stage_transition":    2.0,
    "vcp":                 1.5,
    "volume_accumulation": 1.0,
    "pivot_proximity":     1.0,
}
_MAX_WITH_BONUS = 8.0   # max raw (7.5) + base quality bonus (0.5)


def _weighted_score(
    signals:        dict[str, bool],
    base_weeks:     int   | None,
    base_depth_pct: float | None,
) -> float:
    """
    Weighted composite score scaled to 10.
    Max raw = 7.5 (all signals fired) + 0.5 base quality bonus → 8.0 → 10.0.
    """
    raw   = sum(w for k, w in _SIGNAL_WEIGHTS.items() if signals.get(k))
    bonus = 0.0
    if (base_weeks is not None and base_weeks >= 6
            and base_depth_pct is not None and base_depth_pct <= 30.0):
        bonus = 0.5
    scaled = (raw + bonus) / _MAX_WITH_BONUS * 10.0
    return round(min(10.0, scaled), 1)


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

    typical    = (df["High"] + df["Low"] + df["Close"]) / 3
    bin_edges  = np.linspace(lo, hi, bins + 1)
    bin_vol, _ = np.histogram(typical, bins=bin_edges, weights=df["Volume"])
    bin_mid    = (bin_edges[:-1] + bin_edges[1:]) / 2

    poc_idx = int(np.argmax(bin_vol))
    poc     = round(float(bin_mid[poc_idx]), 2)

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

    cp          = current_price
    supports    = sorted([h for h in hvns if h[0] < cp * 0.995], key=lambda x: x[0], reverse=True)[:3]
    resistances = sorted([h for h in hvns if h[0] > cp * 1.005], key=lambda x: x[0])[:3]

    to_m = lambda v: round(v / 1_000_000, 1)
    return {
        "poc":         poc,
        "supports":    [(p, to_m(v)) for p, v in supports],
        "resistances": [(p, to_m(v)) for p, v in resistances],
    }


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
# AI batch reasoning (model stays claude-sonnet-4-6 — do not change)
# ---------------------------------------------------------------------------

_MAX_TOKENS_OUTPUT = 8192  # claude-sonnet-4-6 hard cap


def _batch_enrich_reasoning_via_ai(candidates: list[dict]) -> dict[str, str]:
    """
    Single Claude API call to generate reasoning for all breakout candidates.
    Returns {ticker: reasoning_text}; tickers absent from result fall back to technical reasoning.
    """
    import re
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not candidates:
        return {}
    log.info("Batch AI reasoning: sending %d candidates in one request", len(candidates))
    try:
        import anthropic
        lines: list[str] = []
        for c in candidates:
            fired    = [k.replace("_", " ") for k, v in c["signals"].items() if v]
            vp       = c["volume_profile"]
            res      = vp.get("resistances", [])
            sup      = vp.get("supports", [])
            vp_parts: list[str] = []
            if res:
                vp_parts.append(f"resistance ${res[0][0]:.2f}")
            if sup:
                vp_parts.append(f"support ${sup[0][0]:.2f}")
            vp_str = ", ".join(vp_parts)
            lines.append(
                f"###{c['ticker']}\n"
                f"{c['company_name']} | {c['sector']} | RS {c['current_rs']:+.1f} | "
                f"signals: {', '.join(fired) or 'none'}"
                + (f" | {vp_str}" if vp_str else "")
            )

        prompt = (
            "For each stock below, write exactly 3 concise plain-English sentences "
            "(no bullets, no markdown, no bold). "
            "Sentences: 1) what the company does and why its sector is currently favourable; "
            "2) a specific catalyst or tailwind that could drive a breakout move; "
            "3) a direct buy-setup assessment (compelling / speculative / needs more confirmation) "
            "referencing the signals and price levels.\n\n"
            "Format: respond with ###TICKER on its own line, then the 3 sentences on the next line. "
            "Do not add any other text between entries.\n\n"
            + "\n\n".join(lines)
        )

        # Cap at the model output limit — 220 tokens × N candidates can exceed 8 192 for large screens
        max_tokens = min(_MAX_TOKENS_OUTPUT, 220 * len(candidates))
        client     = anthropic.Anthropic(api_key=api_key)
        msg        = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = msg.content[0].text
        result: dict[str, str] = {}
        blocks = re.findall(
            r"###([A-Z]{1,5})\n(.+?)(?=\n+###[A-Z]|\Z)", response_text, re.DOTALL
        )
        for ticker, text in blocks:
            result[ticker] = text.strip()
        log.info(
            "Batch AI reasoning: %d/%d candidates enriched", len(result), len(candidates)
        )
        return result
    except Exception as exc:
        log.warning("Batch AI reasoning failed: %s", exc)
        return {}


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
            "Volume accumulation is visible: up-day volume significantly outpaces "
            "down-day volume in the current base — consistent with quiet institutional buying."
        )
    if signals.get("rs_leading"):
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
                "The stock is within striking distance of its Base Pivot High — "
                "a strong-volume close above this pivot would confirm the breakout."
            )

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
    Scan S&P 500 for breakout setups. Returns top max_candidates ranked by composite score.

    Gate sequence (R1):
      1. Weekly pre-filter    — batch yfinance (fast, ~500 tickers)
      2. Daily signal check   — per-ticker yfinance (moderate, ~60 tickers)
      3. Finnhub earnings gate — per-survivor (slow, so run last to save quota)

    Returns dict with keys:
        candidates       list of candidate dicts (with chart data + signals)
        screened_at      ISO timestamp
        universe_size    tickers in S&P 500 (excl. portfolio)
        initial_count    after weekly pre-filter
        qualified_count  after daily checks + CIK dedup
        regime           market regime ('bull' / 'caution' / 'bear')
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

    # ── Step 2: Market regime (R8 — computed once, not per-ticker) ────────────
    regime = _assess_market_regime()
    log.info("Breakout screener: market regime = %s", regime)

    # ── Step 3: SPY weekly benchmark ─────────────────────────────────────────
    spy_raw = yf.Ticker("SPY").history(period="3y", interval="1wk")
    if spy_raw.empty:
        return {"candidates": [], "error": "Could not fetch SPY weekly data"}
    if spy_raw.index.tz is not None:
        spy_raw.index = spy_raw.index.tz_localize(None)
    spy_weekly = spy_raw["Close"].dropna()

    # ── Step 4: Batch weekly download ────────────────────────────────────────
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

    # ── Step 5: Weekly signal pre-filter ─────────────────────────────────────
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

            # Focus on accumulation zone — not confirmed uptrends (RS > 40)
            # and not deep downtrends (RS < -30) that need more time
            if current_rs < -30 or current_rs > 40:
                continue

            # R2: Base Pivot High + R11: base stats (weekly data available here)
            bph        = _compute_base_pivot_high(series)
            base_stats = _compute_base_stats(series, bph)

            # RS leading signal (weekly)
            s_rs_leading = _check_rs_leading(rs_series, series)

            # Pivot proximity hint using BPH (R7 pre-filter)
            if bph and bph > 0:
                dist_bph = (float(series.iloc[-1]) / bph - 1) * 100
                s5_hint  = -10 <= dist_bph < 0
            else:
                price_52w = float(series.iloc[-52:].max()) if len(series) >= 52 else float(series.max())
                price_pct = (float(series.iloc[-1]) / price_52w - 1) * 100 if price_52w > 0 else -100
                s5_hint   = -10 <= price_pct < -1

            # RS cross above zero (stage transition pre-filter)
            s_rs_cross = (
                any(prev < 0 <= curr for prev, curr in zip(
                    rs_series.iloc[-5:].iloc[:-1], rs_series.iloc[-5:].iloc[1:]
                ))
                if len(rs_series) >= 5 else False
            )

            initial_score = int(s_rs_leading) + int(s5_hint) + int(s_rs_cross)
            if initial_score < 1:
                continue

            initial_candidates.append({
                "ticker":         ticker,
                "current_rs":     current_rs,
                "rs_series":      rs_series,
                "weekly_close":   series,
                "bph":            bph,
                "base_weeks":     base_stats["base_weeks"],
                "base_depth_pct": base_stats["base_depth_pct"],
                "base_tightness": base_stats["base_tightness"],
                "s_rs_leading":   s_rs_leading,
                "s5_pivot_hint":  s5_hint,
                "initial_score":  initial_score,
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

    # ── Step 6: Daily analysis — yfinance gates only (R4, R5, R6, R7) ────────
    log.info("Fetching daily data for %d candidates...", len(top_for_daily))
    pre_candidates: list[dict] = []

    for c in top_for_daily:
        ticker = c["ticker"]
        try:
            profile = _breakout_profile(ticker)
            if profile is None:
                continue

            df = profile["df"]
            vp = profile["volume_profile"]

            s_stage   = _check_stage_transition_150d(df, c["rs_series"])   # R4
            s_vcp     = _check_vcp_with_volume_dryup(df)                   # R5
            s_vol_acc = _check_volume_accumulation_ratio(df)               # R6
            s_pivot   = _check_pivot_proximity_bph(df, c.get("bph"))       # R7
            s_rs_lead = c["s_rs_leading"]                                  # R4 (RS divergence)

            signals = {
                "stage_transition":    s_stage,
                "vcp":                 s_vcp,
                "volume_accumulation": s_vol_acc,
                "rs_leading":          s_rs_lead,
                "pivot_proximity":     s_pivot,
            }

            # R9: weighted composite score
            score = _weighted_score(signals, c.get("base_weeks"), c.get("base_depth_pct"))
            if score == 0.0:
                continue

            pre_candidates.append({
                "ticker":               ticker,
                "company_name":         profile["company_name"],
                "sector":               profile["sector"],
                "current_rs":           c["current_rs"],
                "signals":              signals,
                "composite_score":      score,
                "volume_profile":       vp,
                "base_weeks":           c.get("base_weeks"),
                "base_depth_pct":       c.get("base_depth_pct"),
                "base_tightness":       c.get("base_tightness"),
                "technical_reasoning":  _breakout_reasoning(c["current_rs"], signals, vp),
                "stop_loss":            _calc_stop_loss(profile["price"], profile["atr_14"]),
                "ohlcv_daily":          profile["ohlcv_daily"],
                "ohlcv_weekly":         profile["ohlcv_weekly"],
                "mrs_daily":            _mrs_daily_to_json(c["rs_series"]),
                "mrs_weekly":           _mrs_weekly_to_json(c["rs_series"]),
            })

        except Exception as exc:
            log.debug("Daily analysis failed for %s: %s", ticker, exc)
            continue

    log.info("Pre-candidates after yfinance gates: %d", len(pre_candidates))

    # ── Step 7: Finnhub earnings gate — R1/R3 (run last to save quota) ───────
    fh_key = os.environ.get("FINNHUB_API_KEY")
    if fh_key and pre_candidates:
        log.info(
            "Applying Finnhub earnings gate to %d candidates (21-day window)...",
            len(pre_candidates),
        )
        try:
            import finnhub
            fh_client = finnhub.Client(api_key=fh_key)
            survivors: list[dict] = []
            for c in pre_candidates:
                has_earnings = _check_earnings_proximity_finnhub(c["ticker"], fh_client)
                if has_earnings:
                    log.info("Earnings gate: excluded %s (earnings within 21 days)", c["ticker"])
                else:
                    survivors.append(c)
                time.sleep(1.1)  # Finnhub free tier: 60 calls/min
            log.info(
                "Finnhub earnings gate: %d → %d survivors",
                len(pre_candidates), len(survivors),
            )
            pre_candidates = survivors
        except Exception as exc:
            log.warning("Finnhub earnings gate failed: %s — proceeding without gate", exc)
    else:
        if not fh_key:
            log.info("FINNHUB_API_KEY not set — skipping earnings gate")

    # ── Step 8: AI batch reasoning ────────────────────────────────────────────
    ai_reasonings = _batch_enrich_reasoning_via_ai(pre_candidates)

    # ── Step 9: Finalise candidates with score and high_conviction (R10) ─────
    final_candidates: list[dict] = []
    for c in pre_candidates:
        ticker       = c["ticker"]
        score        = c["composite_score"]
        signals_list = [k for k, v in c["signals"].items() if v]

        # R10: High Conviction — score ≥ 7.0 AND rs_leading AND stage_transition AND non-bear
        high_conviction = (
            score >= 7.0
            and bool(c["signals"].get("rs_leading"))
            and bool(c["signals"].get("stage_transition"))
            and regime != "bear"
        )

        final_candidates.append({
            "ticker":          ticker,
            "company_name":    c["company_name"],
            "sector":          c["sector"],
            "mansfield_rs":    round(c["current_rs"], 1),
            "composite_score": score,
            "signals":         signals_list,
            "high_conviction": high_conviction,
            "base_weeks":      c.get("base_weeks"),
            "base_depth_pct":  c.get("base_depth_pct"),
            "base_tightness":  c.get("base_tightness"),
            "reasoning":       ai_reasonings.get(ticker) or c["technical_reasoning"],
            "stop_loss":       c["stop_loss"],
            "ohlcv_daily":     c["ohlcv_daily"],
            "ohlcv_weekly":    c["ohlcv_weekly"],
            "mrs_daily":       c["mrs_daily"],
            "mrs_weekly":      c["mrs_weekly"],
        })

    # ── CIK dedup (same policy as growth screener) ────────────────────────────
    cik_map   = _load_cik_map()
    seen_ciks: set[str] = set()
    deduped:   list[dict] = []
    for c in sorted(
        final_candidates,
        key=lambda x: (x["composite_score"], x["mansfield_rs"]),
        reverse=True,
    ):
        cik = cik_map.get(c["ticker"].upper())
        if cik:
            if cik in seen_ciks:
                continue
            seen_ciks.add(cik)
        deduped.append(c)

    top = deduped[:max_candidates]

    result = {
        "candidates":      top,
        "screened_at":     datetime.now().isoformat(),
        "universe_size":   len(universe),
        "initial_count":   len(initial_candidates),
        "qualified_count": len(deduped),
        "regime":          regime,
    }

    try:
        with open(BREAKOUT_CACHE, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(
            "Breakout screener cached: %d candidates (regime=%s)", len(top), regime
        )
    except Exception as exc:
        log.warning("Breakout cache write failed: %s", exc)

    return result
