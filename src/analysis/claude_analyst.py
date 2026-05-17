"""
Claude analysis layer — five prompt functions that turn raw data into
plain-English insights for the daily dashboard.

Call order inside run_analysis():
  1. macro_plain_english     — independent
  2. sector_rotation         — independent
  3. analyse_holdings        — independent (batched, one API call)
  4. growth_opportunities    — uses macro summary for context
  5. todays_verdict          — synthesises outputs 1-4

All functions accept pre-fetched data dicts and return strings or lists.
No data fetching is done here.
"""

import logging
import re
from datetime import datetime

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a personal financial co-pilot speaking to a non-expert investor.
Your job is to interpret financial data and deliver clear conclusions in
plain, everyday English. Never use jargon without immediately explaining
it in simple terms. Always lead with what the person should think about
or consider doing — not with the data itself. The data is context, not
the message. Write like a trusted, knowledgeable friend who happens to
understand markets — direct, honest, clear, and never alarmist.
Every output must have a clear "so what" that a non-expert can act on.
All analysis is for informational purposes only and is not financial advice."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _call(prompt: str, max_tokens: int = 800) -> str:
    """Single Claude API call against the shared system prompt."""
    msg = _client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _fmt_pct(v, suffix="%") -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}{suffix}" if isinstance(v, (int, float)) else str(v)


def _fmt_num(v, dp=1) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{dp}f}"


def _strip_md_markers(text: str) -> str:
    """Remove **bold** and *italic* markers so plain-text fields render clean."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
    return text.strip()


# ---------------------------------------------------------------------------
# 1. Macro plain English
# ---------------------------------------------------------------------------

def macro_plain_english(macro: dict) -> str:
    """
    Translate FRED macro data into a plain-English market environment summary.
    Returns 2-3 paragraph string.
    """
    if not macro:
        return "Macro data unavailable."

    series = macro.get("series", {})

    def s(name, field="current"):
        return _fmt_num(series.get(name, {}).get(field))

    prompt = f"""Here is today's macroeconomic data pulled from FRED:

Fed Funds Rate: {s('fed_funds')}% (3m change: {s('fed_funds', 'change_3m')}pp)
Rate trajectory: {macro.get('rate_trajectory', 'unknown')}

10yr Treasury: {s('treasury_10y')}% | 2yr Treasury: {s('treasury_2y')}%
Yield curve (10y–2y): {macro.get('yield_curve', {}).get('spread_bps', 'n/a')} bps \
— status: {macro.get('yield_curve', {}).get('status', 'unknown')}

HY Credit Spread: {macro.get('hy_spread_bps', 'n/a')} bps \
— regime: {macro.get('hy_regime', 'unknown')}

VIX: {s('vix')} (3m ago: {s('vix', 'prior_3m')})
CPI (annual): {s('cpi')} (12m change: {s('cpi', 'change_12m')})

Write a 2–3 paragraph plain-English summary covering:
1. What the rate environment means for investors right now
2. What the yield curve and credit spreads are signalling about recession risk
3. One key macro risk and one tailwind for equities over the next 3–6 months

Lead with what this means for a portfolio owner — not just the numbers.
Target 150–200 words total."""

    return _call(prompt, max_tokens=400)


# ---------------------------------------------------------------------------
# 2. Sector rotation narrative
# ---------------------------------------------------------------------------

def sector_rotation_narrative(sector_flows: dict, macro: dict) -> str:
    """
    Describe where money is rotating and what sector signals suggest.
    Returns 1-2 paragraph string.
    """
    if not sector_flows:
        return "Sector flow data unavailable."

    # Build compact sector table (finviz_performance uses change_1w/1m/1y keys)
    rows = []
    sector_perf = sector_flows.get("finviz_performance") or sector_flows.get("sector_performance", [])
    for s in sector_perf:
        name = s.get("sector", "?")
        w1  = _fmt_pct(s.get("change_1w") or s.get("perf_1w"))
        m1  = _fmt_pct(s.get("change_1m") or s.get("perf_1m"))
        ytd = _fmt_pct(s.get("change_1y") or s.get("perf_ytd"))
        rows.append(f"  {name:<30} 1W:{w1:>7}  1M:{m1:>7}  1Y:{ytd:>7}")

    rotation_signals = sector_flows.get("rotation_signals", [])
    signal_lines = [f"  {r['ticker']}: {r['signal']}" for r in rotation_signals[:6]]

    hy_regime = macro.get("hy_regime", "normal") if macro else "normal"
    rate_traj  = macro.get("rate_trajectory", "on hold") if macro else "on hold"

    prompt = f"""Today's S&P sector performance:

{chr(10).join(rows) if rows else '  (no data)'}

Rotation signals from portfolio ETFs:
{chr(10).join(signal_lines) if signal_lines else '  (none)'}

Macro context: HY credit regime = {hy_regime}, rates = {rate_traj}

Write 1–2 paragraphs explaining:
1. Which sectors are leading and which are lagging, and what that pattern suggests
2. What a private investor should pay attention to in terms of sector positioning

Plain English, no jargon. 100–150 words."""

    return _call(prompt, max_tokens=350)


# ---------------------------------------------------------------------------
# 3. Per-holding analysis (batched — one API call for all positions)
# ---------------------------------------------------------------------------

def _format_position(pos: dict, mkt: dict, fund: dict) -> str:
    """Format one position as a compact context block for the prompt."""
    t = pos["ticker"]
    pnl_sym = "+" if pos["pnl_pct"] >= 0 else ""

    lines = [
        f"[{t}] {pos.get('sector','?')} | {pos.get('holding_type','medium')}",
        f"  P&L: {pnl_sym}{pos['pnl_pct']:.1f}% (£{pos['ppl']:+.0f})"
        f"  |  value: £{pos['market_value']:.0f}",
    ]

    # Technical signals (from market_data fetch in main.py; may be absent)
    if mkt:
        rs  = _fmt_num(mkt.get("mansfield_rs"))
        sma = "above SMA50" if mkt.get("above_sma50") else "below SMA50"
        mac = "MACD bullish" if mkt.get("macd_bullish") else "MACD bearish"
        sl  = f"  stop: ${mkt['stop_loss']:.2f}" if mkt.get("stop_loss") else ""
        d52 = _fmt_pct(mkt.get("dist_52w_high"))
        lines.append(f"  RS:{rs}  {sma}  {mac}  52w-high:{d52}{sl}")

    # Fundamental signals (from fundamentals fetch; may be absent)
    if fund:
        pe   = _fmt_num(fund.get("forward_pe"))
        rg   = _fmt_pct(fund.get("revenue_growth_pct"))
        fcf  = _fmt_pct(fund.get("fcf_yield"))
        si   = _fmt_pct(fund.get("short_interest_pct"))
        ins  = "insider buying" if fund.get("insider_buying") else (
               "insider selling" if fund.get("insider_buying") is False else "insider neutral")
        earn = "beat streak" if fund.get("earnings_beat_streak") else (
               f"{fund.get('earnings_miss_count',0)} recent misses")
        lines.append(f"  PE:{pe}  revGrowth:{rg}  FCF:{fcf}  SI:{si}  {ins}  {earn}")

    return "\n".join(lines)


def analyse_holdings(
    positions: list[dict],
    market_data: dict | None = None,
    fundamentals: dict | None = None,
) -> list[dict]:
    """
    Analyse all portfolio holdings in a single batched Claude call.

    Returns list of dicts: [{ticker, signal, analysis}]
    signal is one of: HOLD / WATCH / REDUCE / ADD / EXIT
    """
    if not positions:
        return []

    mkt  = market_data  or {}
    fund = fundamentals or {}

    blocks = []
    for pos in positions:
        t = pos["ticker"]
        blocks.append(_format_position(pos, mkt.get(t), fund.get(t)))

    positions_text = "\n\n".join(blocks)

    prompt = f"""Analyse each of the following portfolio holdings. For each, provide:
- A signal: one of HOLD / WATCH / REDUCE / ADD / EXIT
- 2–3 sentences of plain-English assessment covering technical setup, \
fundamental health, and anything to act on
- If a stop loss is shown, mention whether it's at risk

Format your response exactly as:
[TICKER] SIGNAL — assessment text here.

One entry per line. Use the exact ticker symbol shown in brackets.
Do not add headers, bullet points, or any other formatting.

Portfolio positions:

{positions_text}"""

    raw = _call(prompt, max_tokens=max(2000, len(positions) * 120))

    # Build a canonical upper→original mapping so parsed tickers can be
    # mapped back to the exact format stored in the portfolio (COPGl, SEMI.L…)
    upper_to_orig: dict[str, str] = {p["ticker"].upper(): p["ticker"] for p in positions}

    # Parse [TICKER] SIGNAL — text
    # Lookahead captures multi-line analysis; character class allows dots/hyphens
    results: list[dict] = []
    pattern = re.compile(
        r"^\[([A-Z0-9.\-]+)\]\s+([A-Z]+)\s*[—–\-]+\s*(.+?)(?=^\[[A-Z0-9]|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    parsed_uppers: set[str] = set()

    for m in pattern.finditer(raw):
        upper   = m.group(1).upper()
        signal  = m.group(2).upper()
        analysis = _strip_md_markers(re.sub(r"\s+", " ", m.group(3)).strip())
        # Restore original ticker casing (COPGl, SEMI.L etc.)
        ticker = upper_to_orig.get(upper, upper)
        results.append({"ticker": ticker, "signal": signal, "analysis": analysis})
        parsed_uppers.add(upper)

    # Fallback: any holding whose ticker (uppercased) wasn't parsed gets a
    # placeholder so the dashboard always has an entry for every position
    for pos in positions:
        if pos["ticker"].upper() not in parsed_uppers:
            results.append({
                "ticker":   pos["ticker"],
                "signal":   "HOLD",
                "analysis": "Analysis pending — data may be incomplete.",
            })

    log.info("Holdings analysis: %d/%d positions parsed", len(parsed_uppers), len(positions))
    return results


# ---------------------------------------------------------------------------
# 4. Growth opportunities
# ---------------------------------------------------------------------------

def growth_opportunities(
    screener: dict,
    portfolio_sectors: list[str],
    macro: dict | None = None,
) -> str:
    """
    Narrate the top screener candidates — what they are, why they're interesting,
    and what catalyst could drive the next move.

    Returns narrative string (2-3 paragraphs).
    """
    candidates = screener.get("candidates", [])
    if not candidates:
        return "No screener candidates available today."

    top = candidates[:10]
    rows = []
    for c in top:
        name = c.get("company_name") or c["ticker"]
        sector = c.get("sector") or "?"
        rs  = _fmt_num(c.get("mansfield_rs"))
        rg  = _fmt_pct(c.get("revenue_growth_pct"))
        ps  = _fmt_num(c.get("price_sales"), dp=1)
        score = _fmt_num(c.get("composite_score"))
        rows.append(
            f"  {c['ticker']:<6} {name[:28]:<30} {sector[:22]:<24}"
            f"  RS:{rs:>6}  RevG:{rg:>7}  P/S:{ps:>5}  score:{score}"
        )

    sector_concentration = ", ".join(
        f"{s}" for s in sorted(set(portfolio_sectors))[:8]
    )

    hy_regime  = macro.get("hy_regime",  "normal") if macro else "normal"
    rate_traj  = macro.get("rate_trajectory", "on hold") if macro else "on hold"

    prompt = f"""These are today's top growth stock opportunities from a scan of the S&P 500.
They were selected because they have strong relative strength vs the market,
positive revenue growth, and no major technical red flags.

{chr(10).join(rows)}

My current portfolio is concentrated in: {sector_concentration}
Macro backdrop: credit regime = {hy_regime}, rates = {rate_traj}

Pick the 5 most compelling opportunities and explain each in 30–40 words:
- What the company does (one phrase)
- Why the technical and fundamental setup is attractive right now
- What could be the catalyst for the next move up

Lead with the most compelling pick. Use plain English — assume the reader
doesn't know these companies. No bullet points; write each as a short paragraph
starting with the ticker and company name in bold (using **TICKER — Name**)."""

    return _call(prompt, max_tokens=700)


# ---------------------------------------------------------------------------
# 5. Today's Verdict
# ---------------------------------------------------------------------------

def todays_verdict(
    portfolio: dict,
    macro: dict | None,
    analysis_summaries: dict,
) -> str:
    """
    One crisp paragraph summarising the portfolio's overall positioning and the
    single most important thing to watch or act on today.

    analysis_summaries: dict with keys 'macro', 'sectors', 'opportunities'
                        (shortened versions of earlier outputs)
    """
    account = portfolio.get("account", {})
    positions = portfolio.get("positions", [])
    currency = account.get("currency", "GBP")

    total     = account.get("total", 0)
    unrealised = account.get("unrealized_ppl", 0)
    realised  = account.get("realized_ppl", 0)
    pct_gain  = (unrealised / (total - unrealised) * 100) if (total - unrealised) > 0 else 0

    # Top 3 gainers and losers by absolute P&L
    sorted_pos = sorted(positions, key=lambda x: x["ppl"], reverse=True)
    top3   = [(p["ticker"], p["ppl"]) for p in sorted_pos[:3]]
    bot3   = [(p["ticker"], p["ppl"]) for p in sorted_pos[-3:]]

    def pnl_str(items):
        return "  ".join(f"{t} {'+' if v>=0 else ''}{v:.0f}" for t, v in items)

    # Sector breakdown
    sector_counts: dict[str, int] = {}
    for p in positions:
        sector_counts[p.get("sector","Unknown")] = sector_counts.get(p.get("sector","Unknown"),0) + 1
    top_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:4]

    macro_blurb = analysis_summaries.get("macro", "")[:300]
    sector_blurb = analysis_summaries.get("sectors", "")[:200]
    opps_blurb = analysis_summaries.get("opportunities", "")[:200]

    prompt = f"""Portfolio snapshot:
  Total value: {currency} {total:,.0f}
  Unrealised P&L: {currency} {unrealised:+,.0f} ({pct_gain:+.1f}%)
  Realised P&L (all time): {currency} {realised:+,.0f}
  Positions: {len(positions)} holdings
  Top sectors: {', '.join(f'{s}({n})' for s,n in top_sectors)}
  Top gainers: {pnl_str(top3)}
  Top losers:  {pnl_str(bot3)}

Macro context (summary): {macro_blurb}

Sector rotation (summary): {sector_blurb}

Biggest opportunity flagged today: {opps_blurb}

Write "Today's Verdict" — a single punchy paragraph (max 100 words) that:
1. States in one sentence how the portfolio is positioned for current market conditions
2. Names the single most important thing to watch or consider acting on today
3. Ends with a forward-looking statement about what to expect next

Be direct. Lead with a verdict, not with observations. No hedging."""

    return _strip_md_markers(_call(prompt, max_tokens=250))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_analysis(
    portfolio: dict,
    market_data: dict | None = None,
    fundamentals: dict | None = None,
    macro: dict | None = None,
    sector_flows: dict | None = None,
    screener: dict | None = None,
) -> dict:
    """
    Run all five Claude analysis prompts and return a unified result dict.

    Args:
        portfolio:    Output of fetch_portfolio()
        market_data:  {ticker: {mansfield_rs, stop_loss, macd_bullish, ...}}
        fundamentals: {ticker: {forward_pe, revenue_growth_pct, ...}}
        macro:        Output of fetch_macro_data()
        sector_flows: Output of fetch_sector_data()
        screener:     Output of run_screener()

    All optional inputs default to {} / None — analysis degrades gracefully
    to whatever data is available.
    """
    log.info("Claude analysis: starting five-prompt pipeline (model=%s)", MODEL)

    # 1 & 2 — independent
    log.info("Claude: macro narrative...")
    macro_text = macro_plain_english(macro or {})

    log.info("Claude: sector rotation narrative...")
    sector_text = sector_rotation_narrative(sector_flows or {}, macro or {})

    # 3 — independent (may lack market_data / fundamentals before Phase 8)
    log.info("Claude: holdings analysis (%d positions)...", len(portfolio.get("positions", [])))
    holdings = analyse_holdings(
        portfolio.get("positions", []),
        market_data,
        fundamentals,
    )

    # 4 — uses macro context
    portfolio_sectors = [p.get("sector", "Unknown") for p in portfolio.get("positions", [])]
    log.info("Claude: growth opportunities...")
    opps_text = growth_opportunities(screener or {}, portfolio_sectors, macro)

    # 5 — synthesises 1-4
    log.info("Claude: today's verdict...")
    verdict = todays_verdict(
        portfolio,
        macro,
        {
            "macro":         macro_text,
            "sectors":       sector_text,
            "opportunities": opps_text,
        },
    )

    log.info("Claude analysis complete")
    return {
        "macro_narrative":    macro_text,
        "sector_narrative":   sector_text,
        "holdings_analysis":  holdings,
        "opportunities":      opps_text,
        "verdict":            verdict,
        "generated_at":       datetime.now().isoformat(),
        "model":              MODEL,
    }
