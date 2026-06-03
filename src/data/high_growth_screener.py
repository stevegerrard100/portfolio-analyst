"""
High-Growth Watch screener — mid/small-cap US universe.

Identifies stocks outside the S&P 500 forming accumulation bases before a potential
breakout. Same five-signal weighted methodology as breakout_screener.py:

  1. Stage 1→2 transition — price above rising 150-day SMA + RS crossed above zero (2.0 pts)
  2. VCP (Volatility Contraction Pattern) (1.5 pts)
  3. Volume accumulation ratio (1.0 pts)
  4. RS leading price (2.0 pts)
  5. Pivot proximity (1.0 pts)

  Base quality bonus: +0.5 pts when base ≥ 6 weeks with depth ≤ 30%

Universe: Finviz screener — US-listed, market cap $300M–$10B, avg daily vol >300k.
          S&P 500 constituents are excluded. Portfolio holdings are excluded at call time.
          Universe cached separately for 24 hours.

Cache: 24h TTL (universe is more stable than signal results).

Key difference from breakout_screener.py:
- Candidates with earnings in the next 21 days are KEPT and flagged with earnings_flag=True
  (breakout screener annotates with earnings_soon; both use Finnhub for detection)
"""

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from src.data.breakout_screener import (
    _calc_stop_loss,
    _compute_base_pivot_high,
    _compute_base_stats,
    _check_stage_transition_150d,
    _check_vcp_with_volume_dryup,
    _check_volume_accumulation_ratio,
    _check_rs_leading,
    _check_pivot_proximity_bph,
    _assess_market_regime,
    _check_earnings_proximity_finnhub,
    _SIGNAL_WEIGHTS,
    _MAX_WITH_BONUS,
    _weighted_score,
    _volume_profile,
    _breakout_profile,
    _batch_enrich_reasoning_via_ai,
    _breakout_reasoning,
)
from src.data.screener import fetch_sp500_tickers, _load_cik_map
from src.data.market_data import mansfield_rs, _mrs_daily_to_json, _mrs_weekly_to_json

log = logging.getLogger(__name__)

CACHE_DIR             = Path("cache")
HG_CACHE              = CACHE_DIR / "high_growth_screener.json"
HG_UNIVERSE_CACHE     = CACHE_DIR / "high_growth_universe.json"
HG_CACHE_HOURS        = 24
HG_UNIVERSE_CACHE_HOURS = 24

CACHE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Universe fetch — Finviz screener for mid/small-cap US stocks
# ---------------------------------------------------------------------------

def _fetch_high_growth_universe(sp500_set: set[str]) -> list[str]:
    """
    Fetch US mid/small-cap tickers (market cap $300M–$10B, avg vol >300k)
    from Finviz, excluding S&P 500 constituents.

    Universe is cached separately for 24 hours (more stable than screener results).
    Returns an empty list on failure — caller handles gracefully.
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if HG_UNIVERSE_CACHE.exists():
        try:
            age_h = (
                datetime.now() - datetime.fromtimestamp(HG_UNIVERSE_CACHE.stat().st_mtime)
            ).total_seconds() / 3600
            if age_h < HG_UNIVERSE_CACHE_HOURS:
                cached = json.loads(HG_UNIVERSE_CACHE.read_text(encoding="utf-8"))
                filtered = [t for t in cached if t not in sp500_set]
                log.info(
                    "High-growth universe: cached (%.1fh old) — %d tickers (%d after S&P exclusion)",
                    age_h, len(cached), len(filtered),
                )
                return filtered
        except Exception:
            pass

    log.info("High-growth universe: fetching from Finviz screener...")
    try:
        from finvizfinance.screener.overview import Overview

        tickers: set[str] = set()

        for cap_filter in ["Small ($300mln to $2bln)", "Mid ($2bln to $10bln)"]:
            try:
                overview = Overview()
                overview.set_filter(filters_dict={
                    "Market Cap.": cap_filter,
                    "Average Volume": "Over 300K",
                    "Country": "USA",
                })
                df = overview.screener_view()
                if df is not None and not df.empty:
                    col = "Ticker" if "Ticker" in df.columns else (
                          "Symbol" if "Symbol" in df.columns else None)
                    if col:
                        batch = set(df[col].dropna().tolist())
                        tickers.update(batch)
                        log.info("Finviz '%s': %d tickers", cap_filter, len(batch))
                    else:
                        log.warning("Finviz result has no Ticker/Symbol column for '%s'", cap_filter)
            except Exception as exc:
                log.warning("Finviz screener call failed for '%s': %s", cap_filter, exc)

        if not tickers:
            log.error("High-growth universe: no tickers returned from Finviz")
            return []

        # Cache raw universe (before portfolio/S&P exclusion) for reuse
        universe_all = sorted(tickers)
        try:
            HG_UNIVERSE_CACHE.write_text(
                json.dumps(universe_all), encoding="utf-8"
            )
            log.info("High-growth universe cached: %d tickers", len(universe_all))
        except Exception as exc:
            log.warning("Universe cache write failed: %s", exc)

        filtered = [t for t in universe_all if t not in sp500_set]
        log.info(
            "High-growth universe: %d tickers after S&P 500 exclusion (%d excluded)",
            len(filtered), len(universe_all) - len(filtered),
        )
        return filtered

    except ImportError:
        log.error("finvizfinance not installed — cannot fetch high-growth universe")
        return []
    except Exception as exc:
        log.error("High-growth universe fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_high_growth_screener(
    exclude_tickers: list[str] | None = None,
    max_candidates: int = 15,
    force_refresh: bool = False,
) -> dict:
    """
    Scan mid/small-cap US universe for breakout setups.

    Identical signal methodology to run_breakout_screener(), with two differences:
    - Universe is dynamic (Finviz) rather than S&P 500
    - Earnings-flagged candidates are kept and annotated (earnings_flag=True)

    Returns dict with keys:
        candidates       list[dict]  — up to max_candidates, scored and ranked
        screened_at      str         — ISO timestamp
        universe_size    int         — tickers after exclusions
        initial_count    int         — after weekly pre-filter
        qualified_count  int         — after daily checks + CIK dedup
        regime           str         — 'bull' / 'caution' / 'bear'
    """
    CACHE_DIR.mkdir(exist_ok=True)

    if not force_refresh and HG_CACHE.exists():
        try:
            age_h = (
                datetime.now() - datetime.fromtimestamp(HG_CACHE.stat().st_mtime)
            ).total_seconds() / 3600
            if age_h < HG_CACHE_HOURS:
                with open(HG_CACHE) as f:
                    cached = json.load(f)
                if cached.get("schema_version") == CACHE_SCHEMA_VERSION:
                    log.info("High-growth screener: using cached result (%.1fh old)", age_h)
                    return cached
                log.info(
                    "High-growth cache schema mismatch (found %s, expected %d) — discarding",
                    cached.get("schema_version"), CACHE_SCHEMA_VERSION,
                )
        except Exception:
            pass

    exclude = set(exclude_tickers or [])

    # ── Step 1: S&P 500 constituents (to exclude from universe) ──────────────
    sp500 = fetch_sp500_tickers()
    sp500_set = set(sp500)

    # ── Step 2: Mid/small-cap universe ───────────────────────────────────────
    universe_raw = _fetch_high_growth_universe(sp500_set)
    if not universe_raw:
        return {
            "candidates": [], "screened_at": datetime.now().isoformat(),
            "error": "Could not fetch high-growth universe",
            "universe_size": 0, "initial_count": 0,
            "qualified_count": 0, "regime": "caution",
        }

    universe = [t for t in universe_raw if t not in exclude]
    log.info("High-growth screener: %d tickers in universe (%d excluded as portfolio holdings)",
             len(universe), len(universe_raw) - len(universe))

    # ── Step 3: Market regime ─────────────────────────────────────────────────
    regime = _assess_market_regime()
    log.info("High-growth screener: market regime = %s", regime)

    # ── Step 4: SPY weekly benchmark ─────────────────────────────────────────
    spy_raw = yf.Ticker("SPY").history(period="3y", interval="1wk")
    if spy_raw.empty:
        return {
            "candidates": [], "screened_at": datetime.now().isoformat(),
            "error": "Could not fetch SPY weekly data",
            "universe_size": len(universe), "initial_count": 0,
            "qualified_count": 0, "regime": regime,
        }
    if spy_raw.index.tz is not None:
        spy_raw.index = spy_raw.index.tz_localize(None)
    spy_weekly = spy_raw["Close"].dropna()

    # ── Step 5: Batch weekly download ────────────────────────────────────────
    log.info("High-growth: downloading 2y weekly prices for %d tickers...", len(universe))
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
        log.error("High-growth batch weekly download failed: %s", exc)
        return {
            "candidates": [], "screened_at": datetime.now().isoformat(),
            "error": str(exc),
            "universe_size": len(universe), "initial_count": 0,
            "qualified_count": 0, "regime": regime,
        }

    # ── Step 6: Weekly signal pre-filter ─────────────────────────────────────
    log.info("High-growth: scanning weekly signals...")
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

            if current_rs < -30 or current_rs > 40:
                continue

            bph        = _compute_base_pivot_high(series)
            base_stats = _compute_base_stats(series, bph)

            s_rs_leading = _check_rs_leading(rs_series, series)

            if bph and bph > 0:
                dist_bph = (float(series.iloc[-1]) / bph - 1) * 100
                s5_hint  = -10 <= dist_bph < 0
            else:
                price_52w = float(series.iloc[-52:].max()) if len(series) >= 52 else float(series.max())
                price_pct = (float(series.iloc[-1]) / price_52w - 1) * 100 if price_52w > 0 else -100
                s5_hint   = -10 <= price_pct < -1

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
            log.debug("High-growth weekly scan error for %s: %s", ticker, exc)
            continue

    log.info("High-growth weekly pre-filter: %d candidates with ≥1 signal", len(initial_candidates))

    initial_candidates.sort(
        key=lambda x: (x["initial_score"], x["current_rs"]), reverse=True
    )
    top_for_daily = initial_candidates[:60]

    # ── Step 7: Daily analysis ────────────────────────────────────────────────
    log.info("High-growth: fetching daily data for %d candidates...", len(top_for_daily))
    pre_candidates: list[dict] = []
    gate1_excluded = 0

    for c in top_for_daily:
        ticker = c["ticker"]
        try:
            profile = _breakout_profile(ticker)
            if profile is None:
                # Counts as a gate 1 exclusion — no data = no valid setup.
                # Unlike the breakout screener (S&P 500 stocks always have history),
                # the HG universe includes newer listings where a failed download
                # means the stock genuinely lacks the history needed for scoring.
                log.debug("Gate 1 fail: %s — daily profile unavailable (rate limit or missing data)", ticker)
                gate1_excluded += 1
                continue

            # Gate 1: minimum base quality.
            # NOTE: unlike breakout_screener.py, None here means the stock lacks
            # sufficient listing history to form a valid base — that is a disqualifier,
            # not an edge case to allow through.  We use `is None or` (not `is not None and`)
            # so that missing stats exclude the candidate rather than passing it silently
            # through to a score-0 dead end.
            base_weeks     = c.get("base_weeks")
            base_depth_pct = c.get("base_depth_pct")
            if base_weeks is None or base_weeks < 6:
                log.debug(
                    "Gate 1 fail: %s — base_weeks=%s < 6 or missing (insufficient base length)",
                    ticker, base_weeks,
                )
                gate1_excluded += 1
                continue
            if base_depth_pct is None or base_depth_pct < 10.0:
                log.debug(
                    "Gate 1 fail: %s — base_depth_pct=%s < 10%% or missing (too shallow or no data)",
                    ticker, base_depth_pct,
                )
                gate1_excluded += 1
                continue
            if base_depth_pct > 55.0:
                log.debug(
                    "Gate 1 fail: %s — base_depth_pct=%.1f%% > 55%% (too deep, likely distribution)",
                    ticker, base_depth_pct,
                )
                gate1_excluded += 1
                continue

            df = profile["df"]
            vp = profile["volume_profile"]

            s_stage   = _check_stage_transition_150d(df, c["rs_series"])
            s_vcp     = _check_vcp_with_volume_dryup(df)
            s_vol_acc = _check_volume_accumulation_ratio(df)
            s_pivot   = _check_pivot_proximity_bph(df, c.get("bph"))
            s_rs_lead = c["s_rs_leading"]

            signals = {
                "stage_transition":    s_stage,
                "vcp":                 s_vcp,
                "volume_accumulation": s_vol_acc,
                "rs_leading":          s_rs_lead,
                "pivot_proximity":     s_pivot,
            }

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
            log.debug("High-growth daily analysis failed for %s: %s", ticker, exc)
            continue

    log.info(
        "High-growth gate 1: %d excluded (missing data, base too short/shallow/deep)",
        gate1_excluded,
    )
    log.info("High-growth pre-candidates after daily gates: %d", len(pre_candidates))

    # ── Step 8: Finnhub earnings annotation — all candidates retained ─────────
    # Unlike the breakout screener, earnings-flagged candidates are KEPT.
    # They receive earnings_flag=True and are surfaced with an amber badge.
    for c in pre_candidates:
        c["earnings_flag"] = False
        c["earnings_date"] = None

    fh_key = os.environ.get("FINNHUB_API_KEY")
    if fh_key and pre_candidates:
        log.info(
            "High-growth: checking earnings for %d candidates (21-day window)...",
            len(pre_candidates),
        )
        try:
            import finnhub
            fh_client = finnhub.Client(api_key=fh_key)
            for c in pre_candidates:
                has_earnings, earnings_date = _check_earnings_proximity_finnhub(
                    c["ticker"], fh_client
                )
                if has_earnings:
                    log.info(
                        "High-growth earnings: %s flagged (event on %s)",
                        c["ticker"], earnings_date,
                    )
                    c["earnings_flag"] = True
                    c["earnings_date"] = earnings_date
                time.sleep(1.1)
            n_flagged = sum(1 for c in pre_candidates if c["earnings_flag"])
            log.info(
                "High-growth Finnhub: %d/%d candidates flagged with imminent earnings",
                n_flagged, len(pre_candidates),
            )
        except Exception as exc:
            log.warning(
                "High-growth Finnhub check failed: %s — earnings_flag defaults to False",
                exc,
            )
    else:
        if not fh_key:
            log.info("FINNHUB_API_KEY not set — earnings_flag defaults to False for all candidates")

    # ── Step 9: AI batch reasoning ────────────────────────────────────────────
    ai_reasonings = _batch_enrich_reasoning_via_ai(pre_candidates)

    # ── Step 10: Finalise candidates ──────────────────────────────────────────
    final_candidates: list[dict] = []
    for c in pre_candidates:
        ticker       = c["ticker"]
        score        = c["composite_score"]
        signals_list = [k for k, v in c["signals"].items() if v]

        high_conviction = (
            score >= 7.0
            and bool(c["signals"].get("rs_leading"))
            and bool(c["signals"].get("stage_transition"))
            and regime != "bear"
        )

        regime_watchlist = (
            regime == "bear"
            and score >= 7.0
            and bool(c["signals"].get("rs_leading"))
            and bool(c["signals"].get("stage_transition"))
        )

        ai_result = ai_reasonings.get(ticker) or {}
        final_candidates.append({
            "ticker":          ticker,
            "company_name":    c["company_name"],
            "sector":          c["sector"],
            "mansfield_rs":    round(c["current_rs"], 1),
            "composite_score": score,
            "signals":         signals_list,
            "high_conviction": high_conviction,
            "regime_watchlist": regime_watchlist,
            "earnings_flag":   c.get("earnings_flag", False),
            "earnings_date":   c.get("earnings_date"),
            "base_weeks":      c.get("base_weeks"),
            "base_depth_pct":  c.get("base_depth_pct"),
            "base_tightness":  c.get("base_tightness"),
            "reasoning":       c["technical_reasoning"],
            "setup_strength":  ai_result.get("setup_strength"),
            "key_risk":        ai_result.get("key_risk"),
            "maturity":        ai_result.get("maturity"),
            "stop_loss":       c["stop_loss"],
            "ohlcv_daily":     c["ohlcv_daily"],
            "ohlcv_weekly":    c["ohlcv_weekly"],
            "mrs_daily":       c["mrs_daily"],
            "mrs_weekly":      c["mrs_weekly"],
        })

    # ── CIK dedup ─────────────────────────────────────────────────────────────
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
        "schema_version":  CACHE_SCHEMA_VERSION,
        "candidates":      top,
        "screened_at":     datetime.now().isoformat(),
        "universe_size":   len(universe),
        "initial_count":   len(initial_candidates),
        "qualified_count": len(deduped),
        "regime":          regime,
    }

    try:
        with open(HG_CACHE, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(
            "High-growth screener cached: %d candidates (regime=%s)", len(top), regime
        )
    except Exception as exc:
        log.warning("High-growth cache write failed: %s", exc)

    return result
