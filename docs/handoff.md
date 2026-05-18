# Portfolio Analyst — Handoff

Automated daily investment analysis system. Fetches a Trading 212 portfolio, enriches it with market data and fundamentals, runs a Claude AI analysis pipeline, and publishes a dark-mode HTML dashboard to GitHub Pages.

Runs weekdays at 07:00 UTC via GitHub Actions. Total pipeline time: 40–90 minutes (dominated by the S&P 500 screener and T212 pie rate limits).

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Repository Layout](#2-repository-layout)
3. [Data Sources and APIs](#3-data-sources-and-apis)
4. [Module Reference](#4-module-reference)
5. [Configuration](#5-configuration)
6. [Dashboard](#6-dashboard)
7. [CI/CD](#7-cicd)
8. [Key Constraints and Design Decisions](#8-key-constraints-and-design-decisions)
9. [Rate Limits and Timing](#9-rate-limits-and-timing)
10. [Known Quirks](#10-known-quirks)
11. [Extending the System](#11-extending-the-system)

---

## 1. Architecture

The pipeline runs as a single sequential process (`python -m src.main`) with nine steps:

```
Step 1  fetch_portfolio()         Trading 212 pies + direct positions
Step 2  fetch_market_data()       yfinance OHLCV + Mansfield RS + indicators
Step 3  fetch_sector_data()       Finviz sector perf + SPDR ETF rotation signals
Step 4  fetch_macro_data()        FRED rates, spreads, yield curve
Step 5  fetch_all_fundamentals()  Finnhub basic/insider/earnings + yfinance FCF
Step 6  run_screener()            S&P 500 growth screen (8h cached)
Step 7  run_breakout_screener()   S&P 500 accumulation/breakout screen (8h cached)
Step 8  run_analysis()            Claude 5-prompt pipeline
Step 9  render_dashboard()        Assemble output/index.html from template
```

Each step returns a plain dict. Steps are independent except that Step 8 receives the outputs of Steps 1–6, and Step 9 receives all outputs.

---

## 2. Repository Layout

```
src/
  main.py                     Entry point; orchestrates all 9 steps
  analysis/
    claude_analyst.py         Five Claude prompts + run_analysis() orchestrator
  dashboard/
    renderer.py               Builds the data dict; injects into template
    template.html             Self-contained dark-mode SPA (no build step)
    placeholder.html          Served while a fresh run is in progress
  data/
    trading212.py             T212 API client (read-only)
    market_data.py            yfinance OHLCV, Mansfield RS, ta indicators
    ticker_resolver.py        Dynamic defunct/renamed ticker resolution
    fundamentals.py           Finnhub + yfinance per-holding fundamentals
    macro.py                  FRED macro series
    sector_flows.py           Finviz sector perf + SPDR ETF Mansfield RS
    screener.py               S&P 500 growth screen (multi-pass)
    breakout_screener.py      S&P 500 accumulation/breakout screen (5-signal)
    institutional.py          (unused in main pipeline; available for extension)

config/
  sectors.json                Ticker → sector, ticker → holding_type, pie labels

cache/                        Written at runtime, gitignored
  last_portfolio.json         T212 portfolio snapshot (fallback if API down)
  pie_positions.json          Raw T212 pie positions (4h TTL — skips rate-limited fetch)
  screener.json               Screener results (8h TTL)
  breakout_screener.json      Breakout screener results (8h TTL)
  sec_company_tickers.json    EDGAR CIK map (7-day TTL)

output/
  index.html                  Generated dashboard

.github/workflows/
  analyse.yml                 Daily 07:00 UTC schedule + gh-pages deploy

docs/
  handoff.md                  This document
```

---

## 3. Data Sources and APIs

### Trading 212
- **Auth**: Bearer token — `Authorization: Bearer {T212_API_KEY}`
- **Base URL**: `https://live.trading212.com/api/v0`
- **Endpoints used** (GET only — no order placement ever):
  - `GET /equity/account/summary` — total value, unrealised/realised P&L
  - `GET /equity/account/cash` — free cash
  - `GET /equity/positions` — direct (non-pie) holdings
  - `GET /equity/pies` — list of pie IDs
  - `GET /equity/pies/{id}` — pie composition and per-holding data
- **Keys**: generated in T212 Settings → API Beta
- **Rate limit**: pie endpoints — 1 request per 30 seconds (enforced in `trading212.py`)
- **Pie cache**: raw pie positions cached at `cache/pie_positions.json` for 4 hours to avoid repeated rate-limited fetches during local development. GitHub Actions always runs cold (no persistent cache), so every CI run fetches fresh.

### yfinance
- No API key. Downloads via Yahoo Finance.
- Default period: 2 years (`period="2y"`) per ticker
- 0.5s sleep between requests to avoid throttling
- Used in: `market_data.py` (OHLCV), `fundamentals.py` (FCF, revenue growth, short interest), `screener.py` (RS and OHLCV for candidates), `ticker_resolver.py` (defunct ticker checks)
- yfinance calls in `fundamentals.py` are parallelised with `ThreadPoolExecutor(max_workers=8)`. Finnhub calls remain sequential (rate limit).

### Finnhub
- **Key**: `FINNHUB_API_KEY`
- Free tier: 60 API calls/minute → 1.0s sleep between each call in `fundamentals.py`
- Data fetched per portfolio holding: basic financials (PEG, P/B, ROE, margins), insider sentiment (90-day MSPR), earnings surprises (last 4)

### FRED (Federal Reserve Economic Data)
- **Key**: `FRED_API_KEY`
- Series fetched: Fed Funds Rate, 10yr/2yr Treasury yields, yield spread (T10Y2Y), VIX, HY credit spread (BAMLH0A0HYM2), CPI
- Used to compute: yield curve status (positive/flat/inverted), HY regime (tight/normal/stress), rate trajectory (easing/on hold/tightening), credit stress flag
- Also drives the macro data pills displayed on the Today's Brief tab

### Finviz
- No API key. Uses `finvizfinance` library.
- Fetches 11-sector performance table: 1d, 1w, 1m, 3m, 6m, 1y returns
- Weekly performance data drives the sector rotation heatmap on Today's Brief
- **Note**: Finviz column formats are inconsistent — `Perf Week` returns `"-3.50%"` strings while other performance columns return decimal floats. The `_parse_perf_col()` function in `sector_flows.py` detects and normalises both.

### SEC EDGAR
- No API key. **Required User-Agent**: `portfolio-analyst contact@stevegerrard.org` (this exact string must be used on all EDGAR requests or responses will be blocked)
- Endpoints:
  - `https://www.sec.gov/files/company_tickers.json` — full ticker→CIK map (~14,000 companies)
  - `https://data.sec.gov/submissions/CIK{10-digit}.json` — per-company filings + current tickers
- Used in: `screener.py` (CIK deduplication for share classes like GOOG/GOOGL), `ticker_resolver.py` (rename detection)
- CIK map cached at `cache/sec_company_tickers.json` for 7 days

### Anthropic Claude
- **Key**: `ANTHROPIC_API_KEY`
- **Model**: `claude-sonnet-4-6`
- Five prompts per run for main analysis (see §4.1). Breakout screener uses one additional batched call for candidate reasoning (see §4.7).
- Total token usage (main pipeline): ~3,000–5,000 input + ~2,000–3,500 output per run.

---

## 4. Module Reference

### 4.1 `src/analysis/claude_analyst.py`

Five-prompt pipeline, all using the same `SYSTEM_PROMPT` (non-expert investor persona):

| # | Function | Input | Output | max_tokens |
|---|----------|-------|--------|-----------|
| 1 | `macro_plain_english` | FRED macro dict | 2–3 paragraphs | 400 |
| 2 | `sector_rotation_narrative` | Finviz perf + ETF RS | 1–2 paragraphs | 350 |
| 3 | `analyse_holdings` | All positions (batched) | `[{ticker, signal, analysis}]` | max(2000, n×120) |
| 4 | `growth_opportunities` | Screener top 10 | 2–3 paragraphs | 700 |
| 5 | `todays_verdict` | Summaries of 1–4 | 1 paragraph ≤100 words | 250 |

Prompts 1 and 2 run first (independent of each other). Prompt 3 is independent of 1 and 2. Prompt 4 uses the macro summary for context. Prompt 5 synthesises all four.

**Holdings parser**: Claude returns one line per holding in the format `[TICKER] SIGNAL — assessment`. The regex `r"^\[([A-Z0-9.\-]+)\]\s+([A-Z]+)\s*[—–\-]+\s*(.+?)(?=^\[[A-Z0-9]|\Z)"` parses this. Signals: `HOLD / WATCH / REDUCE / ADD / EXIT`. Any unparsed holding gets a `HOLD` placeholder.

### 4.2 `src/data/trading212.py`

**Ticker normalisation** — T212 uses internal identifiers; `normalise_ticker()` converts them:
- `AAPL_US_EQ` → `AAPL` (strip `_US_EQ`)
- `SEMIl_EQ` → `SEMI.L` (lowercase letter = exchange suffix: `l`=`.L`, `d`=`.DE`, `p`=`.PA`)
- `BRK_B_US_EQ` → `BRK-B` (override dict for special cases)

Hard overrides in `_TICKER_OVERRIDES` at the top of the file handle renames and special cases. After normalisation, `_apply_merger_overrides()` applies a second pass of overrides from `ticker_resolver._TICKER_OVERRIDES` — this resolves post-merger SPAC tickers (e.g. DMYI→IONQ, XPOA→QBTS, SNII→RGTI, VACQ→RKLB, NPA→ASTS). The module-level import `from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES` is critical — the import must be at module level, not inside the function, to avoid a silent lazy-import failure pattern.

**Pie cache**: Before fetching pie positions from T212, `fetch_portfolio()` checks `cache/pie_positions.json`. If the file exists and is less than 4 hours old, it's used directly. This avoids the 30s-per-pie rate limit during repeated local runs.

**Position merging**: `_merge_raw_positions()` aggregates direct + pie positions by ticker (same stock may appear in multiple pies). If a position dict is missing a `ticker` key, it logs a warning and skips rather than crashing.

**Fallback cache**: If the T212 API is unreachable, `fetch_portfolio()` falls back to `cache/last_portfolio.json`. The pipeline continues with stale data rather than failing entirely.

### 4.3 `src/data/market_data.py`

**Mansfield RS**: 52-week relative strength vs SPY benchmark.
```
ratio = ticker_weekly_close / spy_weekly_close
mansfield_rs = ((ratio / ratio.shift(52)) - 1) * 100
```
Computed weekly; stored as both weekly (raw) and daily (linearly interpolated) series for chart embedding.

**Indicators computed per ticker** (via `ta` library): MACD (12/26/9), SMA 20/50, ATR 14, Bollinger Bands (20/2). Volume ratio (5-day vs 20-day average). 52-week high/low proximity.

**ATR stop loss multipliers** (in `renderer.py`): `long_term` = 3.0×, `medium` = 2.5×, `short_term` = 1.5×. Computed as `current_price - (multiplier × ATR14)`.

**Chart data**: Four arrays embedded per holding in the dashboard JSON:
- `ohlcv_daily`: last 365 trading days
- `ohlcv_weekly`: last 104 weeks (resample to weekly)
- `mrs_daily`: Mansfield RS interpolated to daily frequency, last 365 days
- `mrs_weekly`: raw weekly Mansfield RS, last 104 weeks

**Defunct ticker handling**: When `process_ticker()` returns `None`, `fetch_market_data()` calls `resolve_ticker()` from `ticker_resolver.py` and retries with the resolved ticker. If resolution fails, a warning is logged with the best company name found.

### 4.4 `src/data/ticker_resolver.py`

Dynamic resolution for defunct or renamed tickers. Called only when yfinance returns no data for a ticker.

**Strategy 1 — yfinance redirect**: `yf.Ticker(old).info["symbol"]` sometimes returns the successor ticker (Yahoo Finance redirects old SPAC tickers to merged entities). If the symbol differs and has data, that's the resolution.

**Strategy 2 — EDGAR CIK rename**: If the ticker appears in EDGAR's company_tickers.json, fetches the CIK's current submissions to check whether the same legal entity now trades under a different symbol.

**Strategy 3 — Claude AI fallback**: `_resolve_via_ai(ticker, company_name)` calls Claude with a strict system prompt that forces a ticker-only response. Uses `max_tokens=10` and strips non-alpha characters from the response. Returns `None` if the response doesn't match `[A-Z]{1,5}(-[A-Z])?`.

`_TICKER_OVERRIDES: dict[str, str]` is a module-level dict that accumulates resolved mappings for the lifetime of the process. It is imported by reference into `trading212.py` and `main.py` using the alias `_MERGER_OVERRIDES`. Any ticker resolved during `fetch_market_data()` is immediately available to all other modules via this shared dict.

**Hard-coded overrides** (pre-populated at import time): DMYI→IONQ, XPOA→QBTS, SNII→RGTI, IIVI→COHR, VACQ→RKLB, NPA→ASTS, UTX→RTX.

### 4.5 `src/data/screener.py`

Three-pass S&P 500 growth screen. Results cached for 8 hours (`cache/screener.json`).

**Pass 1**: Download Mansfield RS for all ~500 S&P constituents. Filter: RS > 0 AND direction rising. Typically reduces to ~120–150 candidates.

**Pass 2**: For each Pass-1 survivor, download 2y daily OHLCV + fundamentals (revenue growth, P/S ratio). Compute composite score: weighted sum of RS score, revenue growth, and P/S ratio. Also capture `ohlcv_daily`, `ohlcv_weekly`, `mrs_daily`, `mrs_weekly` for chart embedding. Compute `reasoning` string (2–3 sentences) from screener data.

**Pass 3**: CIK-based deduplication. GOOG and GOOGL have the same CIK — only the higher-RS ticker advances.

Portfolio tickers are excluded from the screener output (already owned).

### 4.6 `src/data/breakout_screener.py`

Five-signal accumulation and early breakout screen over the S&P 500. Results cached for 8 hours (`cache/breakout_screener.json`).

**Five signals scored per candidate:**
1. `stage_transition` — transitioning from Stage 1 base to Stage 2 uptrend (RS acceleration + price above SMA50)
2. `vcp` — Volatility Contraction Pattern: series of narrowing price swings indicating institutional accumulation
3. `volume_accumulation` — above-average volume on up days vs down days over 20 sessions
4. `rs_leading_price` — Mansfield RS making new highs before price (leads the breakout)
5. `pivot_proximity` — price within 5% of recent consolidation high (near the buy point)

Composite score = count of signals present (0–5).

**AI reasoning**: All candidates are enriched in a **single batched Claude API call**. The prompt sends all candidates as `###TICKER\n<context>` delimited blocks and requests one-sentence reasoning per ticker. Response is parsed with `re.findall(r"###([A-Z]{1,5})\n(.+?)(?=\n+###[A-Z]|\Z)", ...)`. Falls back to a technical description if the ticker is absent from the batch response. `max_tokens` is capped at 8,192 (model hard limit): `min(8192, 220 * len(candidates))`.

**High-conviction flag**: A candidate is marked `high_conviction = True` if it has both `stage_transition` AND (`vcp` OR `volume_accumulation`) signals. These are displayed with a green "High Conviction" badge and a green left border in the Breakout Watch List table.

### 4.7 `src/data/fundamentals.py`

Per-holding fundamentals fetched from two sources:

| Field | Source |
|-------|--------|
| FCF, FCF yield | yfinance cashflow statement |
| Revenue growth (YoY) | yfinance financials |
| Net debt/EBITDA | yfinance balance sheet |
| Short interest % | yfinance info |
| Forward PE, P/B, PEG, ROE, gross margin | Finnhub basic financials |
| Insider sentiment (MSPR, 90-day) | Finnhub insider transactions |
| Earnings surprises (last 4) | Finnhub company earnings |

yfinance calls are parallelised with `ThreadPoolExecutor(max_workers=min(8, n_tickers))`. Finnhub calls are sequential with 1.0s sleep between each (free tier: 60 req/min).

Finnhub MSPR > 0 = net insider buying; < 0 = net selling.

### 4.8 `src/data/macro.py`

Fetches 7 FRED series. Derives:
- **Yield curve status**: `positive` (>50bps), `flat` (0–50bps), `inverted` (<0bps)
- **HY regime**: `tight` (<300bps), `normal` (300–500bps), `stress` (>500bps)
- **Rate trajectory**: `easing` / `on hold` / `tightening` (based on 12m Fed Funds change)
- **Credit stress flag**: True if HY spread > 500bps (used to downgrade opportunity signals)

The full `series` dict (including `current`, `prior_3m`, `prior_12m`, `change_12m` per series) is passed to `render_dashboard()` and used by `_macro_pills()` to generate colour-coded display values.

### 4.9 `src/data/sector_flows.py`

Two inputs → one output dict:

1. **Finviz sector performance** — scrapes 11 S&P sectors: 1d/1w/1m/3m/6m/1y performance
2. **SPDR ETF Mansfield RS** — extracted from pre-fetched `market_data` dict (no extra downloads). Computes rotation signals per ETF:
   - `early_rotation`: RS 5d > 0, 20d < 0 (momentum just turning positive)
   - `momentum_building`: RS 5d > 20d > 60d (acceleration)
   - `rotation_peaking`: RS 5d < 0 but 20d > 0 (momentum fading)

Also computes a **portfolio alignment score** — what percentage of held sectors have positive Mansfield RS. The `finviz_performance` list (weekly returns per sector) is passed to `render_dashboard()` and used by `_sector_heatmap()` to generate the heatmap grid.

### 4.10 `src/dashboard/renderer.py`

Assembles the `data` dict that replaces `{{DASHBOARD_DATA}}` in `template.html`. Key additions beyond basic account/holdings data:

**`_macro_pills(macro)`** — converts FRED series data into a list of `{label, value, status}` dicts where `status` is `green`, `amber`, or `red`. Thresholds:

| Pill | Green | Amber | Red |
|------|-------|-------|-----|
| 10yr Yield | < 3.5% | 3.5–5% | > 5% |
| 2yr Yield | < 4% | 4–5% | > 5% |
| Yield Curve | positive | flat | inverted |
| HY Spread | < 300 bps | 300–500 bps | > 500 bps |
| Fed Funds | < 3% | 3–5% | > 5% |
| VIX | < 15 | 15–25 | > 25 |
| CPI YoY | < 2.5% | 2.5–4% | > 4% |

**`_sector_heatmap(sector_flows)`** — converts Finviz weekly returns into `{sector, change_1w}` list sorted best→worst.

**Stop loss**: `current_price - (multiplier × ATR14)`. Multipliers: `long_term` = 3.0×, `medium` = 2.5×, `short_term` = 1.5×.

**Breakout candidates**: includes `high_conviction` flag (True if `stage_transition` AND `vcp`/`volume_accumulation` both present).

Full function signature:
```python
def render_dashboard(
    analysis, portfolio, market_data, screener, breakout,
    macro, sector_flows,
    output_path="output/index.html"
) -> None
```

---

## 5. Configuration

### Environment Variables

Set as GitHub Actions secrets and locally in `.env`:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `T212_API_KEY` | trading212.py | Trading 212 API authentication |
| `ANTHROPIC_API_KEY` | claude_analyst.py, ticker_resolver.py | Claude API access |
| `FINNHUB_API_KEY` | fundamentals.py | Finnhub fundamentals |
| `FRED_API_KEY` | macro.py | FRED macro series |

### `config/sectors.json`

Controls three things:

```json
{
  "ticker_to_sector": {
    "AAPL": "Technology",
    "VWRP.L": "ETF"
  },
  "ticker_to_holding_type": {
    "AAPL": "long_term",
    "PLTR": "medium"
  },
  "pie_labels": {
    "123456": "Growth Pie"
  }
}
```

- `ticker_to_sector`: overrides yfinance-derived sector. Required for ETFs and non-US equities.
- `ticker_to_holding_type`: overrides auto-inferred holding type. Auto-inference: `long_term` if market cap > $100B and beta < 1.2; `short_term` if beta > 2.0 or market cap < $300M; `medium` otherwise.
- `pie_labels`: maps T212 pie numeric IDs to display names.

---

## 6. Dashboard

`output/index.html` is a **self-contained single file** — no external CSS, no build step, all JavaScript inline. Deployable by copying the file anywhere.

### Structure

The template (`src/dashboard/template.html`) has one placeholder: `{{DASHBOARD_DATA}}`. At render time, `renderer.py` replaces this with a JSON blob. All rendering happens client-side in vanilla JavaScript on page load.

### Tab Layout (3 tabs)

**Tab 1 — Today's Brief** (default landing tab):
- **Today's Verdict** card — Claude's one-paragraph overall assessment
- **Macro Environment** card — 7 colour-coded data pills (10yr/2yr yield, yield curve, HY spread, Fed Funds, VIX, CPI YoY) above the macro narrative text
- **Sector Rotation** card — 11-sector heatmap grid (green/red, intensity proportional to weekly return magnitude) above the sector narrative text
- **Top Momentum Picks** — top 3 screener candidates as teaser cards with 30-day SVG sparkline price charts and a "See all N opportunities →" link to Tab 3

**Tab 2 — My Portfolio**:
- **Sector Allocation** — donut chart (Chart.js) + allocation table, lazy-initialised on first portfolio tab visit
- **Account stats bar** — Total Value, Unrealised P&L, Realised P&L, Position count
- **Holdings list** — compact table, one row per stock (see Holdings List below)
- Signal filter buttons (ALL / ADD / WATCH / HOLD / REDUCE / EXIT) with counts

**Tab 3 — Opportunities**:
- **Growth Opportunities** — condensed intro (first 3 sentences of Claude's opportunities narrative)
- **Momentum Opportunities** table — top 10 screener candidates with expandable chart rows
- **Breakout Watch List** table — top 15 breakout candidates with expandable chart rows; high-conviction picks highlighted green

### Holdings List

Each holding is a `<tr class="h-row">` in a `<table class="h-list-table">`. Columns: Ticker, Sector, P&L%, P&L£, Signal badge, RS score + inline 2px bar, SMA50 arrow, MACD arrow, Stop loss price, 52w distance from high.

Clicking a row expands a sibling `<div class="h-expand-content">` (rendered **outside** the scrollable table wrapper) containing the Claude analysis text and a candlestick chart. This placement prevents the chart from inheriting the table's horizontal overflow width.

Three subsections: **Individual Holdings**, **Pie Holdings** (grouped by pie name), **ETFs**.

Filter buttons toggle row visibility with `display: none` rather than recreating DOM — preserves chart instances across filter changes.

### Charts

TradingView Lightweight Charts v4.1.3 (CDN: `cdn.jsdelivr.net`). Three series per chart:
- Candlestick (right price scale)
- Volume histogram (custom `'vol'` scale, bottom 15%)
- SMA50 line (yellow, overlaid on candles)

Stop loss rendered as a dashed horizontal price line.

**Holdings charts** use explicit `width`/`height` (not `autoSize`) computed from the expand-content div's `clientWidth`. A `resize` event listener updates chart width on viewport changes. Period selector forces weekly candles when `2Y` is selected.

**Screener/breakout charts** use `autoSize: true` (their containers are in regular divs, not scrollable tables, so ResizeObserver works correctly).

Chart instances are cached in `chartInstances` (holdings), `screenerChartInstances`, and `breakoutChartInstances` dicts. First open initialises; subsequent opens call `fitContent()`.

### Mobile Support

`isMobile` is detected at page load: `UA matches /Mobi|Android|iPhone|iPad|iPod/i` OR `window.innerWidth < 768`. On mobile:
- Holdings charts use `height: 250px` and `barSpacing: 10` (wider candles for touch)
- Chart control buttons stack vertically via `@media (max-width: 768px)`
- Rightmost table columns (SMA50, MACD, Stop, 52w) are hidden via CSS media query to keep the table readable

### Markdown Stripping

The `md(text)` JavaScript function processes all Claude-generated narrative text before display:
1. Strips entire sentences containing "informational purposes only" or "not financial advice" (sentence-level regex, not word-level), then re-adds one clean footer line per section
2. Strips `## / ###` heading markers (keeps the text)
3. Removes `---` / `***` / `___` horizontal rules
4. Strips `**bold**` and `*italic*` asterisk markers

The `firstSentences(text, n)` helper truncates the opportunities narrative to 3 sentences for the Tab 3 intro card.

### Sector Donut

Chart.js v4 (CDN). Lazy-initialised on first visit to the My Portfolio tab via `ensureSectorChart()`. The 14-colour palette in `renderer.py` (`_SECTOR_COLORS`) matches the donut legend.

---

## 7. CI/CD

`.github/workflows/analyse.yml` triggers on:
- **Schedule**: `0 7 * * 1-5` (07:00 UTC, Monday–Friday)
- **`workflow_dispatch`**: manual trigger from the GitHub Actions UI

Steps:
1. Checkout repo
2. Set up Python 3.11
3. Cache pip packages (keyed on `requirements.txt` hash)
4. Install dependencies
5. Run `python -m src.main` (must use `-m`, not `python src/main.py`, to set sys.path correctly)
6. Deploy `./output/` to GitHub Pages at `{repo}/markets/` via `peaceiris/actions-gh-pages@v4`

Secrets required in the repository: `T212_API_KEY`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`. `GITHUB_TOKEN` is provided automatically.

`timeout-minutes: 120` — necessary because T212 pie fetches (32s/pie) plus the screener (45+ min for full S&P run) can exceed 60 minutes.

The deploy step uses `keep_files: true` so previously published files are not deleted on each push.

Note: the `cache/` directory is **not** persisted between GitHub Actions runs. The screener and breakout screener always run fresh in CI. The pie cache only helps local development.

---

## 8. Key Constraints and Design Decisions

### Read-Only T212

**The system must never place, modify, or cancel any orders.** `trading212.py` only calls GET endpoints. The order-placement endpoints (`POST /equity/orders`, etc.) are intentionally absent from the file. This constraint must be preserved in any future changes.

### SEC EDGAR User-Agent

Every request to `*.sec.gov` must include `User-Agent: portfolio-analyst contact@stevegerrard.org`. SEC will block requests without a valid User-Agent. This header is defined as `_HEADERS` in both `screener.py` and `ticker_resolver.py`.

### Python Module Invocation

The pipeline must be run as `python -m src.main`, not `python src/main.py`. Running the file directly adds `src/` to `sys.path`, breaking all `from src.data...` absolute imports. The GitHub Actions workflow uses `-m`.

### Ticker Override Import — Must Be Module-Level

The import of `_TICKER_OVERRIDES` from `ticker_resolver` into `trading212.py` and `main.py` must happen at module level:
```python
from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES
```
If this import is placed inside a function (e.g. inside `_apply_merger_overrides()`), Python's `except Exception: return ticker` pattern will silently catch any import errors and return the original broken ticker. This was a past source of bugs where DMYI, XPOA, SNII etc. passed through unresolved.

### Breakout AI Batch Call — Token Cap

`breakout_screener.py` sends all candidates in one Claude API call. `max_tokens` is capped at `min(8192, 220 * len(candidates))`. The model hard limit is 8,192 output tokens for `claude-sonnet-4-6`. Exceeding it silently returns an empty batch response (the error is caught and logged as a warning). With 37 candidates at 220 tokens each = 8,140, the cap just fits. If the candidate count grows significantly, reduce the per-candidate token budget.

### Screener and Breakout Cache

Both screeners are expensive. Results cached for 8 hours. In GitHub Actions there is no persistent cache — both run fresh every morning. If you want to persist them in CI, add `cache/screener.json` and `cache/breakout_screener.json` to an `actions/cache` step.

### Holdings Charts Outside the Scrollable Table

Expand content for each holding row is rendered as a `<div class="h-expand-content">` placed **after** the `<div class="h-table-wrap">` that wraps the data table, not inside the table as a `<td>`. This is the fix for chart overflow: a `<td colspan="N">` inside a horizontally-scrolling table inherits the table's full overflow width, causing TradingView charts to render at that width and bleed off-screen. The expand div is a sibling of the table wrapper, so it takes the card's width instead.

### Sector Donut — Lazy Init on Hidden Canvas

`new Chart(canvas)` called while the canvas has `display:none` (tab not active) produces a zero-size chart. `ensureSectorChart()` is guarded by a `sectorChartCreated` flag and called only when the My Portfolio tab is first activated.

---

## 9. Rate Limits and Timing

| Source | Limit | Enforced by |
|--------|-------|-------------|
| Trading 212 pies | 1 req / 30s | `trading212.py` sleep |
| yfinance | ~2 req/s practical | 0.5s sleep in `_download()` |
| Finnhub (free tier) | 60 req/min | 1.0s sleep in `fundamentals.py` |
| FRED | 120 req/min | No sleep needed |
| Finviz | No stated limit | Single request per run |
| SEC EDGAR | 10 req/s | 0.3s sleep in `ticker_resolver.py` |

**Typical step durations** (approximate):
- Step 1 (T212 portfolio): 5–15 min depending on pie count; 0 min if pie cache is warm
- Step 2 (market data): 2–5 min
- Steps 3–4 (sector + macro): ~1 min
- Step 5 (fundamentals): ~3 min (yfinance parallel; 3 Finnhub calls × 1s × n holdings)
- Step 6 (screener): 45–75 min (cold); 0 min if cached
- Step 7 (breakout screener): 10–20 min (cold); 0 min if cached
- Step 8 (Claude analysis): ~30s (5 API calls + 1 batch breakout call)
- Step 9 (render): <1s

---

## 10. Known Quirks

**T212 ticker format**: T212's internal identifiers are not standard tickers. `normalise_ticker()` handles most cases via regex, but edge cases require entries in the `_TICKER_OVERRIDES` dict at the top of `trading212.py`. The current overrides include BRK-B, BRK-A, BF-B, META (was FB), X (was TWTR), AVAV (double-underscore), FP.PA (TotalEnergies on Euronext Paris), and post-merger SPACs.

**Finviz column inconsistency**: The `Perf Week` column returns percentage strings (`"-3.50%"`) while other performance columns return decimal fractions (`-0.035`). `_parse_perf_col()` detects the format by checking for `%` in the string value.

**yfinance weekly resampling**: `df.resample("W")` anchors to Sunday. When aligning the ticker's weekly series with SPY's weekly series for Mansfield RS, `reindex(..., method="ffill")` fills any gaps.

**Mansfield RS sign convention**: Positive = outperforming SPY over 52 weeks. Zero crossing upward = early momentum signal. Values of 20+ indicate strong sustained outperformance.

**Holdings parser token budget**: `max_tokens = max(2000, len(positions) * 120)`. With 40 holdings this is 4,800 tokens. If Claude truncates mid-response, increase the multiplier. Any position whose ticker doesn't appear in the parsed output gets a `HOLD` placeholder.

**Dead SPAC positions**: The system may hold defunct SPAC tickers from pre-merger investments. The hard-coded overrides in `ticker_resolver._TICKER_OVERRIDES` resolve known cases at import time. For new cases, add to the dict at the top of `ticker_resolver.py`. If dynamic resolution fails, the ticker is skipped with a warning that includes the company name for manual investigation.

**CPI YoY calculation**: FRED's `CPIAUCSL` series is an index level (~310–320), not a percentage. The YoY inflation rate is computed in `_macro_pills()` as `(current / prior_12m - 1) * 100`, not from the `change_12m` field directly.

**`md()` disclaimer regex is sentence-level**: The regex `[^.!?\n]*(?:informational purposes only|not financial advice)[^.!?\n]*[.!?]?\s*` captures the entire sentence containing a disclaimer phrase. This prevents orphaned fragments like "This is" when Claude wraps the phrase in bold markdown that gets stripped mid-sentence.

---

## 11. Extending the System

### Adding a new data source

1. Create `src/data/yourmodule.py` returning a plain dict.
2. Import and call it in `src/main.py` as a new step.
3. Pass the result to `run_analysis()` and/or `render_dashboard()`.
4. Add any new API key to `.env.example`, GitHub Secrets, and the workflow `env:` block.

### Adding a new Claude prompt

Add a function to `claude_analyst.py` following the pattern of the existing five. Call it from `run_analysis()` and include its output in the returned dict. Update `renderer.py` to pass the new field through to the template, and add a corresponding section to `template.html`.

### Adding a new ticker override (T212 format or SPAC merger)

For T212 identifier quirks, add to `_TICKER_OVERRIDES` in `src/data/trading212.py`.

For post-merger SPAC resolution, add to `_TICKER_OVERRIDES` at the top of `src/data/ticker_resolver.py`. This dict is imported by `trading212.py` and `main.py` at module level, so adding it here propagates everywhere automatically.

### Adding a new sector or holding type

Edit `config/sectors.json`. No code changes needed. Sector names must match the `SECTOR_TO_ETF` mapping in `sector_flows.py` if you want alignment scoring for that sector.

### Changing the screener criteria

The composite score weights and pass-1 filters are in `screener.py`. The `_disqualifiers()` function defines negative screens (high short interest, earnings flags, etc.). The 8-hour cache means changes take effect on the next cold run.

### Changing breakout signal thresholds

Signal detection logic is in `breakout_screener.py`. Each of the five signals has its own function (`_stage_transition()`, `_vcp()`, etc.) and can be tuned independently. The high-conviction badge in `renderer.py` checks `signals` list membership — update both if you rename a signal key.

### Running locally

```bash
cp .env.example .env
# Fill in .env with real keys

pip install -r requirements.txt
python -m src.main
open output/index.html
```

The screener and breakout screener caches at `cache/screener.json` and `cache/breakout_screener.json` are reused across local runs. Delete them to force a fresh run. The T212 pie cache at `cache/pie_positions.json` is valid for 4 hours — delete it to force a fresh T212 fetch.
