"""Finnhub fundamentals + insider activity + earnings; yfinance for FCF/short interest."""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import finnhub
import yfinance as yf

log = logging.getLogger(__name__)


def _client() -> finnhub.Client:
    return finnhub.Client(api_key=os.environ["FINNHUB_API_KEY"])


# ---------------------------------------------------------------------------
# yfinance supplementals (FCF, revenue growth, net debt, short interest)
# ---------------------------------------------------------------------------

def _fetch_yf_info(ticker: str) -> tuple[dict, dict]:
    """
    Returns (parsed_fundamentals_dict, raw_info_dict).
    Keeping raw info so callers can access any extra fields without re-fetching.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        # Free cash flow = operating CF + capex (capex is stored as negative in yfinance)
        fcf = None
        try:
            cf = t.cashflow
            if cf is not None and not cf.empty:
                op_cf = cf.loc["Operating Cash Flow"].iloc[0]
                capex = cf.loc["Capital Expenditure"].iloc[0]
                fcf = float(op_cf) + float(capex)
        except (KeyError, IndexError, TypeError):
            pass

        mkt_cap = info.get("marketCap")
        fcf_yield = round((fcf / mkt_cap) * 100, 2) if (fcf and mkt_cap) else None

        # Revenue growth YoY — most recent annual vs prior
        rev_growth = None
        try:
            fins = t.financials
            if fins is not None and not fins.empty:
                rev = fins.loc["Total Revenue"].values
                if len(rev) >= 2 and float(rev[1]) > 0:
                    rev_growth = round((float(rev[0]) / float(rev[1]) - 1) * 100, 2)
        except (KeyError, IndexError, TypeError):
            pass

        # Net debt / EBITDA
        net_debt_ebitda = None
        try:
            bs = t.balance_sheet
            ebitda = info.get("ebitda")
            if bs is not None and not bs.empty and ebitda and float(ebitda) > 0:
                total_debt = float(bs.loc["Total Debt"].iloc[0])
                cash = float(bs.loc["Cash And Cash Equivalents"].iloc[0])
                net_debt_ebitda = round((total_debt - cash) / float(ebitda), 2)
        except (KeyError, IndexError, TypeError):
            pass

        # Short interest from yfinance (free tier, reliable)
        raw_si = info.get("shortPercentOfFloat")
        short_pct = round(float(raw_si) * 100, 2) if raw_si else None

        parsed = {
            "company_name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": mkt_cap,
            "forward_pe": info.get("forwardPE"),
            "trailing_pe": info.get("trailingPE"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "price_sales": info.get("priceToSalesTrailing12Months"),
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "revenue_growth_pct": rev_growth,
            "institutional_pct": info.get("institutionPercentHeld"),
            "net_debt_ebitda": net_debt_ebitda,
            "short_interest_pct": short_pct,
            "short_interest_flag": (short_pct or 0) > 15,
            "short_interest_strong_flag": (short_pct or 0) > 20,
        }
        return parsed, info

    except Exception as exc:
        log.error("yfinance fundamentals failed for %s: %s", ticker, exc)
        return {}, {}


# ---------------------------------------------------------------------------
# Finnhub calls (1-second delay each — free tier: 60 calls/minute)
# ---------------------------------------------------------------------------

def _finnhub_basic(ticker: str, client: finnhub.Client) -> dict:
    """PEG, P/B, ROE, ROA, margins from Finnhub basic financials."""
    try:
        time.sleep(1.0)
        resp = client.company_basic_financials(ticker, "all")
        m = resp.get("metric", {})
        return {
            "peg_ratio": m.get("pegAnnual"),
            "pb_ratio": m.get("pbAnnual"),
            "roe": m.get("roeTTM"),
            "roa": m.get("roaTTM"),
            "debt_equity": m.get("totalDebt/totalEquityAnnual"),
            "current_ratio": m.get("currentRatioAnnual"),
            "gross_margin": m.get("grossMarginTTM"),
            "net_margin": m.get("netProfitMarginTTM"),
        }
    except Exception as exc:
        log.error("Finnhub basic financials failed for %s: %s", ticker, exc)
        return {}


def _finnhub_insider(ticker: str, client: finnhub.Client) -> dict:
    """Net insider buying/selling via MSPR score (90-day window)."""
    try:
        time.sleep(1.0)
        date_to = date.today().isoformat()
        date_from = (date.today() - timedelta(days=90)).isoformat()
        resp = client.stock_insider_sentiment(ticker, date_from, date_to)
        data = resp.get("data", [])
        if data:
            msprs = [d["mspr"] for d in data if d.get("mspr") is not None]
            avg_mspr = round(sum(msprs) / len(msprs), 2) if msprs else None
        else:
            avg_mspr = None
        return {
            "insider_mspr": avg_mspr,
            "insider_buying": (avg_mspr > 0) if avg_mspr is not None else None,
        }
    except Exception as exc:
        log.error("Finnhub insider sentiment failed for %s: %s", ticker, exc)
        return {"insider_mspr": None, "insider_buying": None}


def _finnhub_earnings(ticker: str, client: finnhub.Client) -> dict:
    """Last 4 earnings surprises — flag if 2+ misses."""
    try:
        time.sleep(1.0)
        raw = client.company_earnings(ticker, limit=4) or []
        surprises = []
        miss_count = 0
        for e in raw:
            surprise = e.get("surprise")
            if surprise is None:
                continue
            surprises.append({
                "period": e.get("period"),
                "actual": e.get("actual"),
                "estimate": e.get("estimate"),
                "surprise": surprise,
                "surprise_pct": e.get("surprisePercent"),
            })
            if surprise < 0:
                miss_count += 1
        return {
            "earnings_surprises": surprises,
            "earnings_miss_count": miss_count,
            "earnings_beat_streak": bool(surprises and all(s["surprise"] >= 0 for s in surprises)),
            "earnings_flag": miss_count >= 2,
        }
    except Exception as exc:
        log.error("Finnhub earnings failed for %s: %s", ticker, exc)
        return {
            "earnings_surprises": [],
            "earnings_miss_count": 0,
            "earnings_beat_streak": False,
            "earnings_flag": False,
        }


# ---------------------------------------------------------------------------
# Combined fetch
# ---------------------------------------------------------------------------

def fetch_all_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """Fetch combined yfinance + Finnhub fundamentals for all portfolio tickers."""
    client = _client()
    results: dict[str, dict] = {}

    # yfinance has no rate limit — fetch all tickers in parallel
    yf_data_map: dict[str, dict] = {}
    workers = min(8, len(tickers)) if tickers else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {ex.submit(_fetch_yf_info, t): t for t in tickers}
        for future in as_completed(future_to_ticker):
            t = future_to_ticker[future]
            try:
                parsed, _ = future.result()
                yf_data_map[t] = parsed
            except Exception as exc:
                log.error("yfinance fundamentals failed for %s: %s", t, exc)
                yf_data_map[t] = {}

    # Finnhub has a 60-calls/minute free-tier limit — must remain sequential
    for ticker in tickers:
        log.info("Fetching Finnhub fundamentals for %s", ticker)
        fh_basic = _finnhub_basic(ticker, client)
        insider  = _finnhub_insider(ticker, client)
        earnings = _finnhub_earnings(ticker, client)
        results[ticker] = {**yf_data_map.get(ticker, {}), **fh_basic, **insider, **earnings}

    return results
