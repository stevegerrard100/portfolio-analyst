"""Market data: yfinance OHLCV, Mansfield RS, ta indicators."""

import logging
import time

import numpy as np
import pandas as pd
import ta
import yfinance as yf

from src.data.ticker_resolver import _TICKER_OVERRIDES, resolve_ticker

log = logging.getLogger(__name__)

INDEX_TICKERS = ["SPY", "QQQ", "IWM"]
VIX_TICKER = "^VIX"
SPDR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB", "XLC"]
COMMODITY_PROXIES = ["GLD", "GDX", "SLV", "XME", "USO"]


# ---------------------------------------------------------------------------
# Raw download
# ---------------------------------------------------------------------------

def _download(ticker: str, period: str = "2y") -> pd.DataFrame | None:
    """Download OHLCV via yfinance with rate limiting and timezone strip."""
    try:
        time.sleep(0.5)
        df = yf.Ticker(ticker).history(period=period)
        if df.empty:
            log.warning("No data for %s", ticker)
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        return df
    except Exception as exc:
        log.error("Download failed for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Mansfield RS (primary momentum indicator)
# ---------------------------------------------------------------------------

def mansfield_rs(
    ticker_weekly_close: pd.Series,
    spy_weekly_close: pd.Series,
    period: int = 52,
) -> pd.Series:
    """52-week Mansfield RS: ((ratio / ratio.shift(N)) - 1) * 100."""
    ratio = ticker_weekly_close / spy_weekly_close
    return ((ratio / ratio.shift(period)) - 1) * 100


def _daily_rs(
    ticker_daily: pd.Series,
    spy_daily: pd.Series,
) -> dict:
    """Short-term daily relative strength for rotation signals (5/20/60 day)."""
    ratio = ticker_daily / spy_daily
    out = {}
    for days in (5, 20, 60):
        shifted = ratio.shift(days)
        rs = ((ratio / shifted) - 1) * 100
        out[f"rs_{days}d"] = float(rs.iloc[-1]) if not pd.isna(rs.iloc[-1]) else 0.0
    return out


# ---------------------------------------------------------------------------
# Technical indicators (ta library)
# ---------------------------------------------------------------------------

def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close, high, low = df["Close"], df["High"], df["Low"]

    macd = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()

    df["sma_20"]  = ta.trend.SMAIndicator(close, window=20).sma_indicator()
    df["sma_50"]  = ta.trend.SMAIndicator(close, window=50).sma_indicator()
    df["sma_200"] = ta.trend.SMAIndicator(close, window=200).sma_indicator()

    df["atr_14"] = ta.volatility.AverageTrueRange(
        high, low, close, window=14
    ).average_true_range()

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_pct"] = bb.bollinger_pband()

    return df


def _macd_crossover_recent(df: pd.DataFrame, lookback: int = 5) -> bool:
    """True if MACD crossed above signal line in the last N sessions."""
    diff = df["macd_diff"].dropna()
    if len(diff) < lookback + 1:
        return False
    recent = diff.iloc[-(lookback + 1):]
    for i in range(1, len(recent)):
        if recent.iloc[i - 1] < 0 <= recent.iloc[i]:
            return True
    return False


def _volume_ratio(df: pd.DataFrame) -> float:
    vol = df["Volume"]
    v5 = vol.iloc[-5:].mean()
    v20 = vol.iloc[-20:].mean()
    return round(float(v5 / v20) if v20 > 0 else 1.0, 2)


def _52w_proximity(df: pd.DataFrame) -> tuple[float, float]:
    """Distance from 52w high/low as %. Negative dist_high = below the high."""
    close = df["Close"]
    current = float(close.iloc[-1])
    window = min(252, len(close))
    high_52w = float(close.rolling(window).max().iloc[-1])
    low_52w = float(close.rolling(window).min().iloc[-1])
    dist_high = round((current / high_52w - 1) * 100, 2)
    dist_low = round((current / low_52w - 1) * 100, 2)
    return dist_high, dist_low


# ---------------------------------------------------------------------------
# JSON serialisers for chart embedding
# ---------------------------------------------------------------------------

def _ohlcv_daily_to_json(df: pd.DataFrame, days: int = 365) -> list[dict]:
    """Last N trading days of OHLCV for the daily candlestick view."""
    subset = df.tail(days)
    return [
        {
            "time": ts.strftime("%Y-%m-%d"),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        }
        for ts, row in subset.iterrows()
    ]


def _ohlcv_weekly_to_json(df: pd.DataFrame, weeks: int = 104) -> list[dict]:
    """Last N weeks of OHLCV aggregated to weekly candles (OHLCV) for the 2Y view."""
    weekly = df.resample("W").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    ).dropna(subset=["Close"])
    subset = weekly.tail(weeks)
    return [
        {
            "time": ts.strftime("%Y-%m-%d"),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        }
        for ts, row in subset.iterrows()
    ]


def _mrs_daily_to_json(mrs_series: pd.Series, days: int = 365) -> list[dict]:
    """Mansfield RS weekly series linearly interpolated to daily frequency."""
    daily = mrs_series.resample("D").interpolate(method="linear")
    subset = daily.tail(days)
    return [
        {"time": ts.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for ts, v in subset.items()
        if not pd.isna(v)
    ]


def _mrs_weekly_to_json(mrs_series: pd.Series, weeks: int = 104) -> list[dict]:
    """Raw weekly Mansfield RS series for the weekly candle view."""
    subset = mrs_series.tail(weeks)
    return [
        {"time": ts.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for ts, v in subset.items()
        if not pd.isna(v)
    ]


# ---------------------------------------------------------------------------
# Per-ticker processing
# ---------------------------------------------------------------------------

def _safe_float(series: pd.Series) -> float | None:
    val = series.iloc[-1]
    return round(float(val), 4) if not pd.isna(val) else None


def process_ticker(ticker: str, spy_daily: pd.DataFrame) -> dict | None:
    """Fetch and compute all market data for a single ticker."""
    df = _download(ticker)
    if df is None or len(df) < 60:
        log.warning("Skipping %s — insufficient data", ticker)
        return None

    df = _add_indicators(df)

    # --- Mansfield RS (52-week weekly) ---
    spy_weekly = spy_daily["Close"].resample("W").last()
    weekly_close = df["Close"].resample("W").last()
    aligned_spy = spy_weekly.reindex(weekly_close.index, method="ffill")
    mrs_series = mansfield_rs(weekly_close, aligned_spy).dropna()

    current_mrs = float(mrs_series.iloc[-1]) if len(mrs_series) > 0 else 0.0
    mrs_4w_ago = float(mrs_series.iloc[-5]) if len(mrs_series) >= 5 else current_mrs

    # MRS crossed above 0 within last 2 weeks (~2 weekly data points)
    if len(mrs_series) >= 3:
        recent = mrs_series.iloc[-3:]
        mrs_recently_crossed = any(
            prev < 0 <= curr
            for prev, curr in zip(recent.iloc[:-1], recent.iloc[1:])
        )
    else:
        mrs_recently_crossed = False

    # --- Daily RS for rotation signals ---
    aligned_spy_daily = spy_daily["Close"].reindex(df.index, method="ffill")
    daily_rs = _daily_rs(df["Close"], aligned_spy_daily)

    # --- Price stats ---
    current_price = float(df["Close"].iloc[-1])
    prev_price = float(df["Close"].iloc[-2]) if len(df) > 1 else current_price
    day_change_pct = round((current_price / prev_price - 1) * 100, 2)

    dist_high, dist_low = _52w_proximity(df)

    sma_20  = _safe_float(df["sma_20"])
    sma_50  = _safe_float(df["sma_50"])
    sma_200 = _safe_float(df["sma_200"])
    atr     = _safe_float(df["atr_14"]) or 0.0

    # 26-week base low — lowest closing price over the trailing 26 weekly candles.
    # Computed here from OHLCV directly (not from the breakout screener).
    if len(weekly_close) >= 5:
        base_low_26w = round(float(weekly_close.tail(26).min()), 4)
    else:
        base_low_26w = None

    macd_bullish = _macd_crossover_recent(df)

    return {
        "ticker": ticker,
        "current_price": round(current_price, 2),
        "day_change_pct": day_change_pct,
        # Chart data (injected into HTML for TradingView Lightweight Charts)
        "ohlcv_daily":  _ohlcv_daily_to_json(df),
        "ohlcv_weekly": _ohlcv_weekly_to_json(df),
        "mrs_daily":    _mrs_daily_to_json(mrs_series),
        "mrs_weekly":   _mrs_weekly_to_json(mrs_series),
        # Mansfield RS
        "mansfield_rs": round(current_mrs, 2),
        "mansfield_rs_4w_ago": round(mrs_4w_ago, 2),
        "mansfield_rs_direction": "rising" if current_mrs > mrs_4w_ago else "falling",
        "mansfield_rs_positive": current_mrs > 0,
        "mansfield_rs_recently_crossed": mrs_recently_crossed,
        # Short-term RS for rotation signals
        "rs_5d": round(daily_rs["rs_5d"], 2),
        "rs_20d": round(daily_rs["rs_20d"], 2),
        "rs_60d": round(daily_rs["rs_60d"], 2),
        # Moving averages (canonical names used by renderer and Claude analyst)
        "sma_20":  sma_20,
        "sma_50":  sma_50,
        "sma_200": sma_200,
        "above_sma50":  bool(sma_50  and current_price > sma_50),
        "above_sma200": bool(sma_200 and current_price > sma_200),
        "above_sma_20": bool(sma_20  and current_price > sma_20),
        # 26-week base low (lowest weekly close over trailing 26 weeks)
        "base_low_26w": base_low_26w,
        # Volatility
        "atr_14": round(atr, 4),
        "bb_upper": _safe_float(df["bb_upper"]),
        "bb_lower": _safe_float(df["bb_lower"]),
        "bb_pct": _safe_float(df["bb_pct"]),
        # MACD (canonical name used by renderer and Claude analyst)
        "macd_bullish": macd_bullish,
        "macd_value": _safe_float(df["macd"]),
        "macd_signal_value": _safe_float(df["macd_signal"]),
        # Volume
        "volume_ratio": _volume_ratio(df),
        # 52-week levels (canonical name used by renderer)
        "dist_52w_high": dist_high,
        "dist_52w_high_pct": dist_high,
        "dist_52w_low_pct": dist_low,
        "near_52w_high": dist_high > -5.0,
        "near_52w_low": dist_low < 5.0,
    }


# ---------------------------------------------------------------------------
# Batch fetch
# ---------------------------------------------------------------------------

def fetch_market_data(tickers: list[str]) -> dict[str, dict]:
    """Fetch and compute market data for a list of tickers."""
    log.info("Downloading SPY benchmark...")
    spy_daily = _download("SPY")
    if spy_daily is None:
        log.error("SPY benchmark fetch failed — cannot compute Mansfield RS")
        return {}

    results: dict[str, dict] = {}
    for ticker in tickers:
        effective = _TICKER_OVERRIDES.get(ticker, ticker)
        log.info("Processing %s%s", effective, f" (override for {ticker})" if effective != ticker else "")
        data = process_ticker(effective, spy_daily)
        if data is None:
            resolved, company_name = resolve_ticker(ticker)
            if resolved:
                data = process_ticker(resolved, spy_daily)
            if data is None:
                log.warning("No market data for %s (%s) — skipping", ticker, company_name)
        if data:
            results[ticker] = data

    log.info("Market data complete: %d/%d tickers", len(results), len(tickers))
    return results


def fetch_index_data() -> dict[str, dict]:
    """Fetch major index + VIX data for the market summary strip."""
    results: dict[str, dict] = {}
    for ticker in INDEX_TICKERS + [VIX_TICKER]:
        df = _download(ticker, period="5d")
        if df is None or len(df) < 2:
            continue
        current = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        results[ticker] = {
            "current": round(current, 2),
            "change_pct": round((current / prev - 1) * 100, 2),
        }
    return results


# ---------------------------------------------------------------------------
# Stop loss calculation
# ---------------------------------------------------------------------------

def calculate_stop_loss(ohlcv_json: list[dict], holding_type: str = "medium") -> float | None:
    """ATR-based stop loss. Returns None for ETFs."""
    multipliers = {
        "long_term": 3.0,
        "medium": 2.0,
        "short_term": 1.5,
        "etf": None,
    }
    multiplier = multipliers.get(holding_type)
    if multiplier is None:
        return None

    df = pd.DataFrame(ohlcv_json)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    atr_series = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()

    atr = float(atr_series.iloc[-1])
    current_price = float(df["Close"].iloc[-1])
    return round(current_price - (atr * multiplier), 2)
