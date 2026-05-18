"""
Resolve defunct or renamed tickers to their current equivalents at runtime.

Resolution strategy (in order):
  0. _TICKER_OVERRIDES (hardcoded) — instant, no API call.
     Covers known mergers and renames where the old ticker is permanent.
  1. _KNOWN_DEFUNCT — tickers with no live successor (SPAC liquidated, or
     company acquired/delisted without a replacement ticker). Skipped
     immediately with a warning; no API calls attempted.
  2. yfinance .info — Yahoo Finance sometimes redirects old SPAC tickers to
     the merged entity via info["symbol"].
  3. SEC EDGAR submissions API — for tickers in EDGAR's CIK map, checks
     whether the legal entity now trades under a different symbol (covers
     simple renames where the company keeps its SEC registration).

If resolution succeeds, _TICKER_OVERRIDES is updated in-process so subsequent
references to the old ticker are resolved instantly.

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

# ---------------------------------------------------------------------------
# Hardcoded override map — populated at module load, no API calls needed.
# Add entries here whenever a holding's ticker is permanently renamed/merged.
# Key: old ticker (as reported by T212 or as it appeared before the event).
# Value: current yfinance-compatible ticker.
# ---------------------------------------------------------------------------
_TICKER_OVERRIDES: dict[str, str] = {
    "IIVI": "COHR",   # II-VI Incorporated → Coherent Corp (Jan 2022 acquisition)
    "VACQ": "RKLB",   # Vector Acquisition Corp → Rocket Lab USA (Aug 2021 merger)
    "NPA":  "ASTS",   # New Providence Acquisition → AST SpaceMobile (Apr 2021 merger)
    "UTX":  "RTX",    # United Technologies → Raytheon Technologies (Apr 2020 merger)
    "DMYI": "IONQ",   # dMY Technology IV → IonQ (quantum computing)
    "XPOA": "QBTS",   # SPAC → D-Wave Quantum Inc.
    "SNII": "RGTI",   # Supernova Partners II → Rigetti Computing
}

# ---------------------------------------------------------------------------
# Known-defunct set — tickers confirmed to have no live US-listed successor.
# These are skipped immediately to avoid repeated failed API lookups.
# ---------------------------------------------------------------------------
_KNOWN_DEFUNCT: frozenset[str] = frozenset()

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

    Resolution order:
      0. Hardcoded _TICKER_OVERRIDES — instant
      1. _KNOWN_DEFUNCT — skip immediately, no API calls
      2. yfinance .info redirect (info["symbol"])
      3. EDGAR CIK → submissions current ticker

    Returns:
        (resolved_ticker, company_name)
        resolved_ticker is None if resolution failed.
        company_name is the best human-readable label available (for warnings).
    """
    upper = ticker.upper()

    # ── 0. Hardcoded override (already applied upstream, but catch stragglers) ─
    if upper in _TICKER_OVERRIDES:
        resolved = _TICKER_OVERRIDES[upper]
        log.info("Resolved %s → %s via hardcoded override", ticker, resolved)
        return resolved, ticker

    # ── 1. Known defunct — no live successor ──────────────────────────────────
    if upper in _KNOWN_DEFUNCT:
        log.warning("Skipping %-10s — known defunct SPAC/merger with no live successor", ticker)
        return None, ticker

    company_name: str = ticker

    # ── 2. yfinance .info redirect ────────────────────────────────────────────
    try:
        time.sleep(0.4)
        info = yf.Ticker(ticker).info
        yf_name = info.get("longName") or info.get("shortName")
        if yf_name:
            company_name = yf_name
        yf_sym = (info.get("symbol") or "").upper().strip()
        if yf_sym and yf_sym != upper and _has_yf_data(yf_sym):
            log.info("Resolved %s → %s via yfinance redirect (%s)", ticker, yf_sym, company_name)
            _TICKER_OVERRIDES[ticker] = yf_sym
            return yf_sym, company_name
    except Exception:
        pass

    # ── 3. EDGAR CIK → submissions current ticker ─────────────────────────────
    cik = _load_cik_map().get(upper)
    if cik:
        current = _submissions_ticker(cik)
        if current and current != upper and _has_yf_data(current):
            log.info(
                "Resolved %s → %s via EDGAR CIK rename (CIK %s, %s)",
                ticker, current, cik, company_name,
            )
            _TICKER_OVERRIDES[ticker] = current
            return current, company_name

    # ── 4. Anthropic AI last resort ───────────────────────────────────────────
    resolved = _resolve_via_ai(ticker, company_name)
    if resolved and resolved != upper:
        if _has_yf_data(resolved):
            log.info("Resolved %s → %s via Anthropic AI (company: %s)", ticker, resolved, company_name)
            _TICKER_OVERRIDES[ticker] = resolved
            return resolved, company_name
        log.debug("AI suggested %s for %s but yfinance returned no data", resolved, ticker)

    log.warning("Could not resolve %-10s (%s) — no live successor found", ticker, company_name)
    return None, company_name


def _resolve_via_ai(ticker: str, company_name: str) -> str | None:
    """Ask the Anthropic API to identify the current successor ticker for a defunct symbol."""
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import re
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=15,
            messages=[{
                "role": "user",
                "content": (
                    f"The US stock ticker '{ticker}' ({company_name}) no longer trades. "
                    f"It may be a SPAC that completed a merger or a company that was renamed. "
                    f"What is the current active US ticker for its successor? "
                    f"Reply with ONLY the ticker symbol (e.g. 'AAPL') or 'NONE'."
                ),
            }],
        )
        result = msg.content[0].text.strip().upper()
        if re.match(r"^[A-Z]{1,5}(-[A-Z])?$", result) and result != "NONE":
            return result
        return None
    except Exception as exc:
        log.debug("AI ticker resolution failed for %s: %s", ticker, exc)
        return None
