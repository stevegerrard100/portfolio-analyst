"""Market data: yfinance OHLCV, Mansfield RS, pandas-ta indicators."""

# Phase 2 implementation


def fetch_ohlcv(ticker: str, period: str = "1y"):
    raise NotImplementedError("Phase 2")


def mansfield_rs(ticker_weekly_close, spy_weekly_close, period: int = 52):
    raise NotImplementedError("Phase 2")


def compute_technicals(ohlcv_df):
    raise NotImplementedError("Phase 2")
