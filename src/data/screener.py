"""
Growth stock screener — S&P 500 universe.

Pass 1  (batch):  Download 3y weekly prices for all S&P 500 tickers + SPY.
                  Compute Mansfield RS; keep stocks with RS > 0.
Pass 2  (tech):   Fetch daily OHLCV for the top 100 by RS.
                  Require price > SMA50.  Score MACD, volume, 52w proximity.
Pass 3  (fund):   Pull yfinance fundamentals.
                  Hard-disqualify on: below SMA50, >20% short interest,
                  negative revenue growth, forward PE > 80.
Pass 4  (score):  Rank survivors by composite score; return top N for Claude.

Cache: SCREENER_CACHE_HOURS (default 8) — refreshes ~twice per trading day
       when triggered manually; daily GA run always gets a fresh result since
       the cache is not committed to the repo.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from src.data.market_data import (
    _add_indicators,
    _macd_crossover_recent,
    _volume_ratio,
    _52w_proximity,
    mansfield_rs,
)

log = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
SCREENER_CACHE = CACHE_DIR / "screener.json"
SCREENER_CACHE_HOURS = 8

CIK_CACHE       = CACHE_DIR / "sec_company_tickers.json"
CIK_CACHE_DAYS  = 7
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HEADERS = {"User-Agent": "portfolio-analyst contact@stevegerrard.org"}


# ---------------------------------------------------------------------------
# S&P 500 universe
# ---------------------------------------------------------------------------

def fetch_sp500_tickers() -> list[str]:
    """Fetch S&P 500 constituents from Wikipedia.  Normalises BRK.B → BRK-B."""
    try:
        resp = requests.get(_SP500_WIKI, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", {"id": "constituents"})
        if not table:
            raise ValueError("Constituents table not found")
        tickers = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if cells:
                ticker = cells[0].get_text(strip=True).replace(".", "-")
                tickers.append(ticker)
        log.info("S&P 500: %d tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as exc:
        log.error("S&P 500 fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# CIK map — SEC EDGAR ticker → CIK for same-company deduplication
# ---------------------------------------------------------------------------

def _load_cik_map() -> dict[str, str]:
    """
    Return a {TICKER: CIK_str} mapping built from the SEC EDGAR
    company_tickers.json.  Used to group share classes (GOOG/GOOGL → same CIK)
    so only the highest-RS ticker per company appears in results.

    Cached at cache/sec_company_tickers.json for CIK_CACHE_DAYS days.
    Returns an empty dict on failure — dedup is silently skipped.
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if CIK_CACHE.exists():
        try:
            age_days = (
                datetime.now() - datetime.fromtimestamp(CIK_CACHE.stat().st_mtime)
            ).days
            if age_days < CIK_CACHE_DAYS:
                with open(CIK_CACHE) as f:
                    mapping = json.load(f)
                log.info("CIK map: loaded from cache (%d days old, %d entries)", age_days, len(mapping))
                return mapping
        except Exception:
            pass

    log.info("Downloading SEC EDGAR company_tickers.json...")
    try:
        resp = requests.get(_SEC_TICKERS_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        mapping: dict[str, str] = {}
        for entry in raw.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik    = str(entry.get("cik_str", ""))
            if ticker and cik:
                mapping[ticker] = cik

        with open(CIK_CACHE, "w") as f:
            json.dump(mapping, f)
        log.info("CIK map cached: %d ticker→CIK entries", len(mapping))
        return mapping

    except Exception as exc:
        log.error("CIK map download failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Pass 1: batch Mansfield RS
# ---------------------------------------------------------------------------

def _batch_mansfield_rs(
    universe: list[str],
    spy_weekly: pd.Series,
) -> dict[str, float]:
    """
    Compute Mansfield RS for every ticker in universe against spy_weekly.
    weekly_close must be a DataFrame with ticker columns, already downloaded.
    Returns {ticker: rs_score} for tickers with enough data.
    """
    log.info("Downloading 3y weekly prices for %d tickers + SPY...", len(universe))
    try:
        raw = yf.download(
            universe + ["SPY"],
            period="3y",
            interval="1wk",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        # yfinance multi-ticker download: raw["Close"] is a ticker-columned DataFrame
        if isinstance(raw.columns, pd.MultiIndex):
            weekly_close = raw["Close"]
        else:
            weekly_close = raw[["Close"]] if "Close" in raw.columns else raw
    except Exception as exc:
        log.error("Batch weekly download failed: %s", exc)
        return {}

    rs_scores: dict[str, float] = {}
    for ticker in universe:
        if ticker not in weekly_close.columns:
            continue
        series = weekly_close[ticker].dropna()
        if len(series) < 60:  # Need at least 52w + buffer
            continue
        try:
            rs_series = mansfield_rs(series, spy_weekly)
            latest = rs_series.dropna()
            if latest.empty:
                continue
            rs_scores[ticker] = round(float(latest.iloc[-1]), 2)
        except Exception:
            continue

    log.info("Mansfield RS computed for %d / %d tickers", len(rs_scores), len(universe))
    return rs_scores


# ---------------------------------------------------------------------------
# Pass 2: daily technicals
# ---------------------------------------------------------------------------

def _technical_profile(ticker: str) -> dict | None:
    """
    Fetch 1y daily OHLCV and compute technical signals.
    Returns None if data is insufficient.
    """
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if df.empty or len(df) < 60:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df = _add_indicators(df)

        close = df["Close"]
        price   = round(float(close.iloc[-1]), 2)
        sma50   = df["sma_50"].iloc[-1]
        sma20   = df["sma_20"].iloc[-1]
        bb_pct  = df["bb_pct"].iloc[-1]
        macd_d  = df["macd_diff"].iloc[-1]

        above_sma50  = bool(price > float(sma50))  if not pd.isna(sma50)  else None
        macd_bullish = bool(float(macd_d) > 0)      if not pd.isna(macd_d) else None
        macd_cross   = _macd_crossover_recent(df, lookback=5)
        vol_ratio    = _volume_ratio(df)
        dist_high, dist_low = _52w_proximity(df)

        return {
            "price":         price,
            "sma50":         round(float(sma50), 2)  if not pd.isna(sma50)  else None,
            "sma20":         round(float(sma20), 2)  if not pd.isna(sma20)  else None,
            "above_sma50":   above_sma50,
            "macd_bullish":  macd_bullish,
            "macd_crossover_recent": macd_cross,
            "volume_ratio":  vol_ratio,
            "dist_52w_high": dist_high,
            "dist_52w_low":  dist_low,
            "bb_pct":        round(float(bb_pct), 3) if not pd.isna(bb_pct) else None,
        }
    except Exception as exc:
        log.debug("Technical profile failed for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Pass 3: fundamentals (yfinance info only — fast, no extra API)
# ---------------------------------------------------------------------------

def _fundamental_profile(info: dict) -> dict:
    """Extract fundamental metrics from a yfinance info dict."""
    raw_si = info.get("shortPercentOfFloat")
    short_pct = round(float(raw_si) * 100, 2) if raw_si else None

    rev_growth = info.get("revenueGrowth")           # quarterly YoY, decimal
    earn_growth = info.get("earningsGrowth")          # quarterly YoY, decimal
    fwd_pe = info.get("forwardPE")
    ps = info.get("priceToSalesTrailing12Months")

    return {
        "company_name":       info.get("longName") or info.get("shortName"),
        "sector":             info.get("sector"),
        "industry":           info.get("industry"),
        "market_cap":         info.get("marketCap"),
        "forward_pe":         round(float(fwd_pe), 1)   if fwd_pe else None,
        "price_sales":        round(float(ps), 2)        if ps else None,
        "revenue_growth_pct": round(rev_growth * 100, 1) if rev_growth is not None else None,
        "earnings_growth_pct":round(earn_growth * 100, 1)if earn_growth is not None else None,
        "institutional_pct":  info.get("institutionPercentHeld"),
        "short_interest_pct": short_pct,
    }


# ---------------------------------------------------------------------------
# Disqualifiers (hard stops — any one removes from ranked list)
# ---------------------------------------------------------------------------

_DISQUALIFIER_RULES: list[tuple[str, str]] = [
    # (condition_key_or_logic, label)
]


def _disqualifiers(candidate: dict) -> list[str]:
    """
    Return list of hard-disqualifier labels.  Empty list = candidate passes.
    Disqualifiers are never overridable by score — they are binary removals.
    """
    reasons: list[str] = []

    if candidate.get("above_sma50") is False:
        reasons.append("below_sma50")

    si = candidate.get("short_interest_pct") or 0
    if si > 20:
        reasons.append("high_short_interest_gt20pct")

    rev_g = candidate.get("revenue_growth_pct")
    if rev_g is not None and rev_g < -5:
        reasons.append("revenue_declining")

    fwd_pe = candidate.get("forward_pe")
    if fwd_pe is not None and fwd_pe > 200:
        reasons.append("extreme_valuation_pe_gt200")

    mktcap = candidate.get("market_cap") or 0
    if mktcap and mktcap < 500e6:
        reasons.append("micro_cap_lt500m")

    return reasons


# ---------------------------------------------------------------------------
# Composite score (0–100)
# ---------------------------------------------------------------------------

def _composite_score(c: dict) -> float:
    """
    Weighted composite score for ranking qualified candidates.

    Component breakdown (max pts):
      Mansfield RS         30  — primary signal
      RS trend (4-week)     5  — accelerating vs decelerating
      Revenue growth       25  — top-line momentum
      Technical signals    20  — SMA50, MACD, 52w proximity
      Valuation / short    20  — lower P/S better; lower SI better
    """
    score = 0.0

    # Mansfield RS (30 pts): RS 0–50 → 0–30
    rs = c.get("mansfield_rs") or 0
    score += min(max(rs, 0), 50) * 0.60

    # RS 4-week trend (5 pts): positive trend = accelerating
    rs_trend = c.get("rs_trend_4w") or 0
    if rs_trend > 0:
        score += min(rs_trend * 0.5, 5)

    # Revenue growth (25 pts): 0%→0, 25%+→25
    rev_g = c.get("revenue_growth_pct") or 0
    if rev_g > 0:
        score += min(rev_g, 25)

    # Technical (20 pts)
    if c.get("above_sma50"):
        score += 6
    dist_h = c.get("dist_52w_high") or -100
    if dist_h > -10:   # within 10% of 52w high
        score += 7
    elif dist_h > -20:
        score += 3
    if c.get("macd_bullish"):
        score += 4
    if c.get("macd_crossover_recent"):
        score += 3

    # Valuation (10 pts): P/S <5 best
    ps = c.get("price_sales") or 0
    if 0 < ps <= 5:
        score += 10
    elif 0 < ps <= 10:
        score += 6
    elif 0 < ps <= 20:
        score += 3

    # Short interest penalty (10 pts, inverted)
    si = c.get("short_interest_pct") or 0
    if si < 3:
        score += 10
    elif si < 8:
        score += 6
    elif si < 15:
        score += 2

    return round(score, 1)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_screener(
    exclude_tickers: list[str] | None = None,
    max_candidates: int = 25,
    force_refresh: bool = False,
) -> dict:
    """
    Run the S&P 500 growth screener and return ranked candidates.

    Args:
        exclude_tickers:  Portfolio tickers to skip (already held)
        max_candidates:   Maximum candidates to return (default 25)
        force_refresh:    Bypass cache even if fresh

    Returns dict with keys:
        candidates       list of scored candidate dicts
        screened_at      ISO timestamp
        universe_size    tickers in S&P 500 (excl. portfolio)
        pass1_count      after RS > 0 filter
        pass2_count      with full technical + fundamental data
        pass3_count      after disqualifiers removed
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if not force_refresh and SCREENER_CACHE.exists():
        try:
            age_h = (datetime.now() - datetime.fromtimestamp(
                SCREENER_CACHE.stat().st_mtime)).total_seconds() / 3600
            if age_h < SCREENER_CACHE_HOURS:
                log.info("Using cached screener (%.1fh old)", age_h)
                with open(SCREENER_CACHE) as f:
                    return json.load(f)
        except Exception:
            pass

    exclude = set(exclude_tickers or [])

    # --- Pass 1: universe + Mansfield RS ---
    sp500 = fetch_sp500_tickers()
    if not sp500:
        return {"candidates": [], "error": "Could not fetch S&P 500 universe"}

    universe = [t for t in sp500 if t not in exclude]
    log.info("Screener: %d tickers in universe (%d excluded)", len(universe), len(exclude))

    # SPY weekly series for RS benchmark
    spy_raw = yf.Ticker("SPY").history(period="3y", interval="1wk")
    if spy_raw.empty:
        return {"candidates": [], "error": "Could not fetch SPY weekly data"}
    if spy_raw.index.tz is not None:
        spy_raw.index = spy_raw.index.tz_localize(None)
    spy_weekly = spy_raw["Close"].dropna()

    rs_scores = _batch_mansfield_rs(universe, spy_weekly)

    # Pass 1 filter: RS > 0
    pass1 = {t: rs for t, rs in rs_scores.items() if rs > 0}
    log.info("Pass 1 (RS > 0): %d / %d tickers", len(pass1), len(universe))

    # Take top 100 by RS for deeper analysis
    top_by_rs = sorted(pass1.items(), key=lambda x: x[1], reverse=True)[:100]

    # --- Pass 2 & 3: daily technicals + fundamentals ---
    log.info("Pass 2: fetching daily data + fundamentals for %d tickers...", len(top_by_rs))
    candidates: list[dict] = []

    for ticker, rs_score in top_by_rs:
        try:
            # Technical profile
            tech = _technical_profile(ticker)
            if tech is None:
                continue

            # RS trend (4-week comparison)
            ticker_weekly = yf.Ticker(ticker).history(period="2y", interval="1wk")
            if ticker_weekly.empty:
                rs_trend_4w = None
            else:
                if ticker_weekly.index.tz is not None:
                    ticker_weekly.index = ticker_weekly.index.tz_localize(None)
                wkly = ticker_weekly["Close"].dropna()
                rs_series = mansfield_rs(wkly, spy_weekly)
                rs_clean = rs_series.dropna()
                if len(rs_clean) >= 5:
                    rs_trend_4w = round(float(rs_clean.iloc[-1]) - float(rs_clean.iloc[-5]), 2)
                else:
                    rs_trend_4w = None

            # Fundamentals
            info = yf.Ticker(ticker).info or {}
            fund = _fundamental_profile(info)

            candidate = {
                "ticker":       ticker,
                "mansfield_rs": rs_score,
                "rs_trend_4w":  rs_trend_4w,
                **tech,
                **fund,
            }

            dq = _disqualifiers(candidate)
            candidate["disqualified"]  = bool(dq)
            candidate["disqualifiers"] = dq
            candidate["composite_score"] = (
                _composite_score(candidate) if not dq else 0.0
            )
            candidates.append(candidate)
            time.sleep(0.5)

        except Exception as exc:
            log.debug("Screener error for %s: %s", ticker, exc)
            continue

    log.info("Pass 2: %d candidates processed", len(candidates))

    # Pass 3: remove disqualified, cap runaway RS, dedup by CIK
    qualified = [c for c in candidates if not c["disqualified"]]

    # Cap Mansfield RS at 300 — values far above this are spinoff / rebase artefacts
    # (e.g. SNDK was spun from WDC mid-year; its "52w-ago" price is the parent's).
    for c in qualified:
        if (c.get("mansfield_rs") or 0) > 300:
            c["rs_data_quality"] = "capped"
            c["mansfield_rs"] = 300.0
            c["composite_score"] = _composite_score(c)

    # CIK-based deduplication: group by SEC CIK so share classes of the same
    # company (GOOG/GOOGL, BRK-A/BRK-B, etc.) produce only one candidate.
    # Within each CIK group, keep the ticker with the highest Mansfield RS.
    cik_map = _load_cik_map()
    seen_ciks: set[str] = set()
    deduped: list[dict] = []
    # Sort descending by RS first so the highest-RS share class wins the tie-break
    for c in sorted(qualified, key=lambda x: x.get("mansfield_rs") or 0, reverse=True):
        cik = cik_map.get(c["ticker"].upper())
        if cik:
            if cik in seen_ciks:
                log.info(
                    "Dedup: skipping %s (CIK %s already represented by a higher-RS candidate)",
                    c["ticker"], cik,
                )
                continue
            seen_ciks.add(cik)
        deduped.append(c)

    deduped.sort(key=lambda x: x["composite_score"], reverse=True)
    log.info("Pass 3 (qualified after CIK dedup): %d → returning top %d", len(deduped), max_candidates)

    result = {
        "candidates":     deduped[:max_candidates],
        "all_candidates": candidates,          # full list incl. disqualified (for debugging)
        "screened_at":    datetime.now().isoformat(),
        "universe_size":  len(universe),
        "pass1_count":    len(pass1),
        "pass2_count":    len(candidates),
        "pass3_count":    len(deduped),
    }

    try:
        # Don't persist all_candidates to cache — too large
        cache_result = {k: v for k, v in result.items() if k != "all_candidates"}
        with open(SCREENER_CACHE, "w") as f:
            json.dump(cache_result, f, indent=2, default=str)
        log.info("Screener cached: %d candidates", len(qualified[:max_candidates]))
    except Exception as exc:
        log.warning("Screener cache write failed: %s", exc)

    return result
