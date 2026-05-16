"""SEC EDGAR 13F institutional positioning tracker."""

import json
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from lxml import etree

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "portfolio-analyst contact@stevegerrard.org"}
CACHE_DIR = Path("cache")
CACHE_FILE = CACHE_DIR / "institutional.json"
CACHE_MAX_DAYS = 30  # 13F data is quarterly — refresh monthly

FILERS = {
    "Blackrock":     "0001364742",
    "Vanguard":      "0000102909",
    "State Street":  "0000093751",
    "Fidelity":      "0000315066",
    "JPMorgan":      "0000019617",
    "Goldman Sachs": "0000886982",
    "Bridgewater":   "0001350694",
    "Citadel":       "0001423298",
    "AQR":           "0001167557",
    "Millennium":    "0001273931",
}

# Namespace varies across filers (camelCase vs lowercase 't'); use local-name helpers instead.


# ---------------------------------------------------------------------------
# Name normalisation for fuzzy matching
# ---------------------------------------------------------------------------

_STRIP_PATTERNS = [
    r"\bCLASS\s+[A-Z]\b", r"\bCL\s+[A-Z]\b", r"\bSER(?:IES)?\s+[A-Z]\b",
    r"\bINC\.?\b", r"\bCORP\.?\b", r"\bCO\.?\b", r"\bLTD\.?\b",
    r"\bLLC\.?\b", r"\bLP\.?\b", r"\bPLC\.?\b", r"\bN\.?V\.?\b",
    r"\bS\.?A\.?\b", r"\bHOLDINGS?\b", r"\bGROUP\b",
    r"\bINTERNATIONAL\b", r"\bCOMPANY\b",
]

def _normalize(name: str) -> str:
    name = name.upper().strip()
    for p in _STRIP_PATTERNS:
        name = re.sub(p, " ", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


# ---------------------------------------------------------------------------
# EDGAR API helpers
# ---------------------------------------------------------------------------

def _pad_cik(cik: str) -> str:
    return cik.zfill(10)

def _cik_int(cik: str) -> str:
    return str(int(cik))

def _nodash(accession: str) -> str:
    return accession.replace("-", "")


def _get_recent_13f_filings(cik: str, count: int = 2) -> list[dict]:
    """Return the N most recent 13F-HR filings from the EDGAR submissions API."""
    url = f"https://data.sec.gov/submissions/CIK{_pad_cik(cik)}.json"
    time.sleep(0.3)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    periods = recent.get("reportDate", recent.get("periodOfReport", []))

    filings: list[dict] = []
    for i, form in enumerate(forms):
        if form == "13F-HR":
            filings.append({
                "accession": accessions[i],
                "date": dates[i],
                "period": periods[i] if i < len(periods) else None,
            })
        if len(filings) >= count:
            break
    return filings


def _get_infotable_url(cik_int_str: str, accession_nodash: str) -> str | None:
    """
    Fetch the filing index page and extract the infotable XML URL.
    Looks for an <a> tag whose href ends in .xml and whose text/href
    contains 'infotable' or 'form13f' (case-insensitive).
    Falls back to the largest .xml link if no name-match is found.
    """
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int_str}/{accession_nodash}/"
    time.sleep(0.3)
    try:
        resp = requests.get(index_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Filing index fetch failed (%s): %s", index_url, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    xml_links: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if not href.lower().endswith(".xml"):
            continue
        text = (a.get_text(strip=True) + " " + href).lower()
        full_url = f"https://www.sec.gov{href}" if href.startswith("/") else href

        # Direct match on well-known infotable naming patterns
        if any(kw in text for kw in ("infotable", "form13f", "13f_table", "information")):
            # Exclude the primary/cover document
            if "primary" not in text and "cover" not in text:
                return full_url

        xml_links.append(full_url)

    # Fallback: the largest XML file that isn't the primary doc is usually the infotable
    non_primary = [u for u in xml_links if "primary" not in u.lower()]
    return non_primary[0] if non_primary else (xml_links[0] if xml_links else None)


# ---------------------------------------------------------------------------
# Infotable XML parser
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Strip XML namespace prefix from a tag string."""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_child(elem, local_tag: str):
    """Find first direct child by local name, ignoring namespace."""
    for child in elem:
        if _local(child.tag) == local_tag:
            return child
    return None


def _text_el(elem, local_tag: str) -> str:
    """Get text of a direct child element by local name, namespace-agnostic."""
    child = _find_child(elem, local_tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_infotable(url: str, target_names: dict[str, str]) -> dict[str, dict]:
    """
    Stream-download and iterparse the 13F infotable XML.
    target_names: {normalized_company_name → ticker}
    Returns: {ticker → {issuer_name, cusip, shares, value_usd}}
    Aggregates multiple share classes and sub-accounts per ticker.
    Note: SEC spec says value is in thousands, but most large filers
    (e.g. Blackrock) report in actual USD — verified against implied price.
    """
    log.info("Downloading infotable (%s)...", url)
    time.sleep(0.2)
    try:
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=120)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Infotable download failed: %s", exc)
        return {}

    results: dict[str, dict] = {}
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            for chunk in resp.iter_content(chunk_size=131072):
                tmp.write(chunk)
            tmp_path = tmp.name

        for event, elem in etree.iterparse(tmp_path, events=("end",), recover=True):
            if _local(elem.tag) != "infoTable":
                continue  # Never clear child elements — text is read at infoTable level

            issuer = _text_el(elem, "nameOfIssuer")
            normalized = _normalize(issuer)

            # Match against all target portfolio company names
            matched_ticker: str | None = None
            for norm_target, ticker in target_names.items():
                if norm_target and (norm_target in normalized or normalized in norm_target):
                    matched_ticker = ticker
                    break

            if matched_ticker:
                cusip = _text_el(elem, "cusip")
                value_text = _text_el(elem, "value").replace(",", "") or "0"

                shares_parent = _find_child(elem, "shrsOrPrnAmt")
                shares_text = "0"
                if shares_parent is not None:
                    shares_text = _text_el(shares_parent, "sshPrnamt").replace(",", "") or "0"

                try:
                    value = int(value_text)
                    shares = int(shares_text)
                except ValueError:
                    value, shares = 0, 0

                # Aggregate share classes (CL A + CL B etc.)
                if matched_ticker in results:
                    results[matched_ticker]["shares"] += shares
                    results[matched_ticker]["value_usd"] += value
                else:
                    results[matched_ticker] = {
                        "issuer_name": issuer,
                        "cusip": cusip,
                        "shares": shares,
                        "value_usd": value,
                    }

            # Free processed element and release prior siblings from parent
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

    except Exception as exc:
        log.error("XML parse error: %s", exc)

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    log.info("Infotable parsed: found %d portfolio matches", len(results))
    return results


# ---------------------------------------------------------------------------
# QoQ action classification
# ---------------------------------------------------------------------------

def _action(current_shares: int, prior_shares: int) -> str:
    if prior_shares == 0 and current_shares > 0:
        return "initiated"
    if current_shares == 0 and prior_shares > 0:
        return "exited"
    if current_shares > prior_shares:
        return "added"
    if current_shares < prior_shares:
        return "reduced"
    return "unchanged"


def _quarter_label(period_str: str | None) -> str:
    """Convert '2024-06-30' to 'Q2 2024'."""
    if not period_str:
        return "Unknown quarter"
    try:
        d = datetime.strptime(period_str[:10], "%Y-%m-%d")
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"
    except ValueError:
        return period_str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_13f_changes(
    tickers: list[str],
    ticker_to_name: dict[str, str],
) -> dict:
    """
    Fetch institutional 13F position changes for all portfolio tickers.

    ticker_to_name: {ticker → company_name} from yfinance fundamentals.
    Returns a dict with 'holdings' (per-ticker per-institution data),
    'quarter' label, and 'data_lag_warning'.
    """
    CACHE_DIR.mkdir(exist_ok=True)

    # Use cache if it exists and is less than CACHE_MAX_DAYS old
    if CACHE_FILE.exists():
        try:
            age_days = (datetime.now() - datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)).days
            if age_days < CACHE_MAX_DAYS:
                log.info("Using cached institutional data (%d days old)", age_days)
                with open(CACHE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass

    # Build normalized-name → ticker lookup for XML matching
    target_names: dict[str, str] = {}
    for ticker in tickers:
        name = ticker_to_name.get(ticker, "")
        if name:
            norm = _normalize(name)
            if norm:
                target_names[norm] = ticker

    per_ticker: dict[str, dict] = {t: {} for t in tickers}
    latest_period: str | None = None

    for institution, cik in FILERS.items():
        log.info("Processing %s (CIK %s)", institution, cik)
        try:
            filings = _get_recent_13f_filings(cik, count=2)
            if not filings:
                log.warning("No 13F-HR filings found for %s", institution)
                continue

            cik_int_str = _cik_int(cik)
            current_holdings: dict[str, dict] = {}
            prior_holdings: dict[str, dict] = {}

            for i, filing in enumerate(filings[:2]):
                acc_nd = _nodash(filing["accession"])
                infotable_url = _get_infotable_url(cik_int_str, acc_nd)
                if not infotable_url:
                    log.warning("No infotable URL for %s filing %s", institution, filing["accession"])
                    continue

                holdings = _parse_infotable(infotable_url, target_names)
                log.info(
                    "%s %s: %d/%d portfolio tickers found",
                    institution, filing.get("period", filing["date"]), len(holdings), len(tickers),
                )

                if i == 0:
                    current_holdings = holdings
                    if filing.get("period") and not latest_period:
                        latest_period = filing["period"]
                else:
                    prior_holdings = holdings

        except Exception as exc:
            log.error("Failed processing %s: %s", institution, exc)
            continue

        # Compute QoQ changes per ticker
        for ticker in tickers:
            curr = current_holdings.get(ticker)
            prev = prior_holdings.get(ticker)

            if curr is None and prev is None:
                continue  # Not held by this institution in either quarter

            curr_shares = curr["shares"] if curr else 0
            prev_shares = prev["shares"] if prev else 0

            per_ticker[ticker][institution] = {
                "action": _action(curr_shares, prev_shares),
                "current_shares": curr_shares,
                "prior_shares": prev_shares,
                "current_value_usd": curr["value_usd"] if curr else 0,
            }

    result = {
        "holdings": per_ticker,
        "quarter": _quarter_label(latest_period),
        "data_lag_warning": "Data is approximately 45 days old",
        "fetched_at": datetime.now().isoformat(),
    }

    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(result, f, indent=2)
        log.info("Institutional data cached to %s", CACHE_FILE)
    except Exception as exc:
        log.warning("Cache write failed: %s", exc)

    return result
