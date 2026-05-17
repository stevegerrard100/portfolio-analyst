"""
Resolve defunct or renamed tickers to their current equivalents at runtime.

Resolution strategy (in order):
  1. yfinance .info — company name + symbol redirect (Yahoo Finance often
     redirects old SPAC tickers to the merged entity, surfacing the successor
     symbol in info["symbol"])
  2. SEC EDGAR submissions API — for tickers found in EDGAR's CIK map, checks
     whether the CIK now trades under a different ticker (covers renames where
     the same legal entity changes its exchange symbol)

If resolution succeeds, _TICKER_OVERRIDES is updated so callers can map future
references from old → new without repeating the lookup.

If resolution fails, callers receive the best company name found so they can
emit a meaningful warning rather than a bare ticker symbol.
"""

import json
import logging
import time
from pathlib import Path

import requests
import yfinance as yf

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "portfolio-analyst contact@stevegerrard.org"}
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_CIK_CACHE = Path("cache/sec_company_tickers.json")  # shared with screener.py

# Runtime mapping populated by resolve_ticker().
# Other modules import this dict by reference — mutations are visible everywhere.
_TICKER_OVERRIDES: dict[str, str] = {}

# Module-level in-process cache (ticker → bare CIK string)
_cik_map: dict[str, str] | None = None


def _load_cik_map() -> dict[str, str]:
    """
    Return {TICKER: cik_str} from SEC EDGAR company_tickers.json.
    Reads screener's cache file if present and non-empty, otherwise downloads.
    """
    global _cik_map
    if _cik_map is not None:
        return _cik_map

    if _CIK_CACHE.exists():
        try:
            with open(_CIK_CACHE) as f:
                loaded = json.load(f)
            if loaded:
                _cik_map = loaded
                log.debug("ticker_resolver: CIK map loaded from cache (%d entries)", len(_cik_map))
                return _cik_map
        except Exception:
            pass

    try:
        resp = requests.get(_SEC_TICKERS_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        _cik_map = {
            str(e.get("ticker", "")).upper(): str(e.get("cik_str", ""))
            for e in resp.json().values()
            if e.get("ticker") and e.get("cik_str")
        }
        log.debug("ticker_resolver: CIK map downloaded (%d entries)", len(_cik_map))
    except Exception as exc:
        log.warning("ticker_resolver: EDGAR CIK map unavailable — %s", exc)
        _cik_map = {}

    return _cik_map


def _submissions_ticker(cik: str) -> str | None:
    """
    Ask the EDGAR submissions API for the current exchange ticker for a CIK.
    Catches simple renames: same legal entity, new exchange symbol.
    """
    try:
        time.sleep(0.3)
        resp = requests.get(
            _SEC_SUBMISSIONS_URL.format(cik=cik.zfill(10)),
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        tickers = resp.json().get("tickers", [])
        return tickers[0].upper() if tickers else None
    except Exception as exc:
        log.debug("EDGAR submissions lookup failed for CIK %s: %s", cik, exc)
        return None


def _has_yf_data(ticker: str) -> bool:
    """Return True if yfinance can return at least 10 days of price history."""
    try:
        time.sleep(0.4)
        return len(yf.Ticker(ticker).history(period="3mo")) >= 10
    except Exception:
        return False


def resolve_ticker(ticker: str) -> tuple[str | None, str]:
    """
    Try to find the current active ticker when yfinance returned no data.

    Returns:
        (resolved_ticker, company_name)
        resolved_ticker is None if resolution failed.
        company_name is the best human-readable label available (for warnings).
    """
    company_name: str = ticker

    # ── 1. yfinance .info ────────────────────────────────────────────────────
    try:
        time.sleep(0.4)
        info = yf.Ticker(ticker).info
        yf_name = info.get("longName") or info.get("shortName")
        if yf_name:
            company_name = yf_name
        yf_sym = (info.get("symbol") or "").upper().strip()
        if yf_sym and yf_sym != ticker.upper() and _has_yf_data(yf_sym):
            log.info("Resolved %s → %s via yfinance redirect (%s)", ticker, yf_sym, company_name)
            _TICKER_OVERRIDES[ticker] = yf_sym
            return yf_sym, company_name
    except Exception:
        pass

    # ── 2. EDGAR CIK → submissions current ticker ────────────────────────────
    cik = _load_cik_map().get(ticker.upper())
    if cik:
        current = _submissions_ticker(cik)
        if current and current != ticker.upper() and _has_yf_data(current):
            log.info(
                "Resolved %s → %s via EDGAR CIK rename (CIK %s, %s)",
                ticker, current, cik, company_name,
            )
            _TICKER_OVERRIDES[ticker] = current
            return current, company_name

    return None, company_name
