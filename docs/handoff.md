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

The pipeline runs as a single sequential process (`python -m src.main`) with eight steps:

```
Step 1  fetch_portfolio()         Trading 212 pies + direct positions
Step 2  fetch_market_data()       yfinance OHLCV + Mansfield RS + indicators
Step 3  fetch_sector_data()       Finviz sector perf + SPDR ETF rotation signals
Step 4  fetch_macro_data()        FRED rates, spreads, yield curve
Step 5  fetch_all_fundamentals()  Finnhub basic/insider/earnings + yfinance FCF
Step 6  run_screener()            S&P 500 growth screen (8h cached)
Step 7  run_analysis()            Claude 5-prompt pipeline
Step 8  render_dashboard()        Assemble output/index.html from template
```

Each step returns a plain dict. Steps are independent except that Step 7 receives the outputs of 1–6, and Step 8 receives the outputs of 1, 2, 6, and 7.

---

## 2. Repository Layout

```
src/
  main.py                     Entry point; orchestrates all 8 steps
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
    institutional.py          (unused in main pipeline; available for extension)

config/
  sectors.json                Ticker → sector, ticker → holding_type, pie labels

cache/                        Written at runtime, gitignored
  last_portfolio.json         T212 portfolio snapshot (fallback if API down)
  screener.json               Screener results (8h TTL)
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
- **Auth**: HTTP Basic Auth — `Authorization: Basic base64(API_KEY:API_SECRET)`
- **Base URL**: `https://live.trading212.com/api/v0`
- **Endpoints used** (GET only — no order placement ever):
  - `GET /equity/account/summary` — total value, unrealised/realised P&L
  - `GET /equity/account/cash` — free cash
  - `GET /equity/positions` — direct (non-pie) holdings
  - `GET /equity/pies` — list of pie IDs
  - `GET /equity/pies/{id}` — pie composition and per-holding data
- **Keys**: separate keys for live vs demo accounts; generated in Settings → API Beta
- **Rate limit**: pies endpoints — 1 request per 30 seconds (enforced in `trading212.py`)

### yfinance
- No API key. Downloads via Yahoo Finance.
- Default period: 2 years (`period="2y"`) per ticker
- 0.5s sleep between requests to avoid throttling
- Used in: `market_data.py` (OHLCV), `fundamentals.py` (FCF, revenue growth, short interest), `screener.py` (RS and OHLCV for candidates), `ticker_resolver.py` (defunct ticker checks)

### Finnhub
- **Key**: `FINNHUB_API_KEY`
- Free tier: 60 API calls/minute → 1.0s sleep between each call in `fundamentals.py`
- Data fetched per portfolio holding: basic financials (PEG, P/B, ROE, margins), insider sentiment (90-day MSPR), earnings surprises (last 4)

### FRED (Federal Reserve Economic Data)
- **Key**: `FRED_API_KEY`
- Series fetched: Fed Funds Rate, 10yr/2yr Treasury yields, yield spread (T10Y2Y), VIX, HY credit spread (BAMLH0A0HYM2), CPI
- Used to compute: yield curve status (positive/flat/inverted), HY regime (tight/normal/stress), rate trajectory (easing/on hold/tightening), credit stress flag

### Finviz
- No API key. Uses `finvizfinance` library.
- Fetches 11-sector performance table: 1d, 1w, 1m, 3m, 6m, 1y returns
- **Note**: Finviz column formats are inconsistent — `Perf Week` returns `"-3.50%"` strings while other performance columns return decimal floats. The `_parse_perf_col()` function in `sector_flows.py` detects and normalises both.

### SEC EDGAR
- No API key. **Required User-Agent**: `portfolio-analyst contact@stevegerrard.org` (this exact string must be used on all EDGAR requests or responses will be blocked)
- Endpoints:
  - `https://www.sec.gov/files/company_tickers.json` — full ticker→CIK map (~14,000 companies)
  - `https://data.sec.gov/submissions/CIK{10-digit}.json` — per-company filings + current tickers
- Used in: `screener.py` (CIK deduplication to deduplicate share classes like GOOG/GOOGL), `ticker_resolver.py` (rename detection)
- CIK map cached at `cache/sec_company_tickers.json` for 7 days

### Anthropic Claude
- **Key**: `ANTHROPIC_API_KEY`
- **Model**: `claude-sonnet-4-6`
- Five prompts per run (see §4.1). Total token usage: ~3,000–5,000 input + ~2,000–3,500 output per run.

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

Prompts 1 and 2 run first (independent). Prompt 3 runs in parallel with 1 and 2 conceptually (independent from them). Prompt 4 uses the macro summary for context. Prompt 5 synthesises all four.

**Holdings parser**: Claude returns one line per holding in the format `[TICKER] SIGNAL — assessment`. The regex `r"^\[([A-Z0-9.\-]+)\]\s+([A-Z]+)\s*[—–\-]+\s*(.+?)(?=^\[[A-Z0-9]|\Z)"` parses this. Signals: `HOLD / WATCH / REDUCE / ADD / EXIT`. Any unparsed holding gets a `HOLD` placeholder.

### 4.2 `src/data/trading212.py`

**Ticker normalisation** — T212 uses internal identifiers; `normalise_ticker()` converts them:
- `AAPL_US_EQ` → `AAPL` (strip `_US_EQ`)
- `SEMIl_EQ` → `SEMI.L` (lowercase letter = exchange suffix: `l`=`.L`, `d`=`.DE`, `p`=`.PA`)
- `BRK_B_US_EQ` → `BRK-B` (override dict for special cases)
- `FB_US_EQ` → `META` (override dict for renames)

Hard overrides live in `_TICKER_OVERRIDES` dict at the top of the file. Add new ones there for any ticker T212 uses that can't be derived by the regex rules.

**Pie rate limit**: T212 imposes 1 request per 30 seconds on pie endpoints. With many pies this is the dominant delay in the pipeline. Each pie's position list is fetched separately (`GET /equity/pies/{id}`).

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

Dynamic resolution for defunct or renamed tickers. Called only when yfinance returns no data.

**Strategy 1 — yfinance redirect**: `yf.Ticker(old).info["symbol"]` sometimes returns the successor ticker (Yahoo Finance redirects old SPAC tickers to merged entities). If the symbol differs and has data, that's the resolution.

**Strategy 2 — EDGAR CIK rename**: If the ticker appears in EDGAR's company_tickers.json, fetches the CIK's current submissions to check whether the same legal entity now trades under a different symbol. Covers simple ticker renames (same company, new exchange symbol).

`_TICKER_OVERRIDES: dict[str, str]` is a module-level dict imported by reference into `market_data.py`. Mutations made by `resolve_ticker()` are immediately visible to the caller. Once resolved, the mapping is reused for all subsequent references to the old ticker within the same pipeline run.

### 4.5 `src/data/screener.py`

Three-pass S&P 500 growth screen. Results cached for 8 hours (screener runs are expensive — 45+ minutes).

**Pass 1**: Download Mansfield RS for all ~500 S&P constituents. Filter: RS > 0 AND direction rising. Typically reduces to ~120–150 candidates.

**Pass 2**: For each Pass-1 survivor, download 2y daily OHLCV + fundamentals (revenue growth, P/S ratio). Compute composite score: weighted sum of RS score, revenue growth, and P/S ratio. Also capture `ohlcv_daily`, `ohlcv_weekly`, `mrs_daily`, `mrs_weekly` for chart embedding in the dashboard. Compute `reasoning` string (2–3 sentences) from screener data — no additional API calls.

**Pass 3**: CIK-based deduplication. GOOG and GOOGL have different tickers but the same CIK — only the higher-RS ticker advances.

Portfolio tickers are excluded from the screener output (already owned).

### 4.6 `src/data/fundamentals.py`

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

Finnhub MSPR > 0 = net insider buying; < 0 = net selling.

### 4.7 `src/data/macro.py`

Fetches 7 FRED series. Derives:
- **Yield curve status**: `positive` (>50bps), `flat` (0–50bps), `inverted` (<0bps)
- **HY regime**: `tight` (<300bps), `normal` (300–500bps), `stress` (>500bps)
- **Rate trajectory**: `easing` / `on hold` / `tightening` (based on 12m Fed Funds change)
- **Credit stress flag**: True if HY spread > 500bps (used to downgrade opportunity signals)

### 4.8 `src/data/sector_flows.py`

Two inputs → one output dict:

1. **Finviz sector performance** — scrapes 11 S&P sectors: 1d/1w/1m/3m/6m/1y performance
2. **SPDR ETF Mansfield RS** — extracted from pre-fetched `market_data` dict (no extra downloads). Computes rotation signals per ETF:
   - `early_rotation`: RS 5d > 0, 20d < 0 (momentum just turning positive)
   - `momentum_building`: RS 5d > 20d > 60d (acceleration)
   - `rotation_peaking`: RS 5d < 0 but 20d > 0 (momentum fading)

Also computes a **portfolio alignment score** — what percentage of held sectors have positive Mansfield RS.

### 4.9 `src/dashboard/renderer.py`

Assembles the `data` dict that replaces `{{DASHBOARD_DATA}}` in `template.html`. All chart arrays, formatted numbers, and stop loss values are computed here. Key sections:

- **Account totals**: total, unrealised P&L, cost basis, % gain
- **Sector allocation**: aggregated by sector from positions; used for donut chart
- **Holdings**: merged from T212 positions + market_data + Claude analysis. Stop loss computed as `current_price - (multiplier × ATR14)`.
- **Screener candidates** (top 10): includes `reasoning`, `stop_loss`, and all four chart arrays

---

## 5. Configuration

### Environment Variables

Set as GitHub Actions secrets and locally in `.env`:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `T212_API_KEY` | trading212.py | Trading 212 API authentication |
| `T212_API_SECRET` | trading212.py | Trading 212 API authentication |
| `ANTHROPIC_API_KEY` | claude_analyst.py | Claude API access |
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
  },
  "watchlist": ["NVDA", "TSLA"]
}
```

- `ticker_to_sector`: overrides yfinance-derived sector. Required for ETFs and non-US equities where yfinance returns wrong or blank sectors.
- `ticker_to_holding_type`: overrides auto-inferred holding type. Auto-inference: `long_term` if market cap > $100B and beta < 1.2; `short_term` if beta > 2.0 or market cap < $300M; `medium` otherwise.
- `pie_labels`: maps T212 pie numeric IDs to display names.

---

## 6. Dashboard

`output/index.html` is a **self-contained single file** — no external CSS, no build step, all JavaScript inline. Deployable by copying the file anywhere.

### Structure

The template (`src/dashboard/template.html`) has one placeholder: `{{DASHBOARD_DATA}}`. At render time, `renderer.py` replaces this with a JSON blob. All rendering happens client-side in vanilla JavaScript on page load.

### Charts

TradingView Lightweight Charts v4.1.3 (CDN: `cdn.jsdelivr.net`). Three chart types per ticker:
- Candlestick series (right price scale)
- Volume histogram (custom `'vol'` price scale, bottom 15%)
- Mansfield RS line (left price scale, hidden axis, top 30%)

Charts are **lazily initialised** — created on first `<details>` open, then reused. The `chartInstances` dict (holding cards) and `screenerChartInstances` dict (screener table) map `ticker → {chart, candleSeries, volSeries, rsSeries, period, candle}`.

Period selector forces weekly candles when `2Y` is selected. Stop loss is rendered as a dashed horizontal price line on the candlestick series.

### Holding Cards

Each holding is a `<details class="hcard" data-ticker="...">` element. When expanded it spans full grid width (`grid-column: 1 / -1`). Filter buttons (sector, signal, holding type) show/hide cards with `display: none` rather than recreating DOM — this preserves chart instances across filter changes.

### Screener Table

Each row is a `<tr class="s-row">` with a hidden `<tr class="s-detail">` immediately below it. Clicking the row toggles the detail row. The detail row contains the `reasoning` text paragraph and the same chart controls as holding cards. `createScreenerChart()` uses the same LWC configuration as `createChart()` but reads from `DATA.screener_candidates` instead of `DATA.holdings`.

### Sector Donut

Chart.js v4 (CDN). Rendered once on page load from `DATA.sectors`. The 14-colour palette in `renderer.py` (`_SECTOR_COLORS`) matches the sector donut legend.

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

Secrets required in the repository: `T212_API_KEY`, `T212_API_SECRET`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`. `GITHUB_TOKEN` is provided automatically.

`timeout-minutes: 120` — necessary because T212 pie fetches (32s/pie) plus the screener (45+ min for full S&P run) can exceed 60 minutes.

The deploy step uses `keep_files: true` so previously published files (e.g. older dashboards if you were archiving them) are not deleted on each push.

---

## 8. Key Constraints and Design Decisions

### Read-Only T212

**The system must never place, modify, or cancel any orders.** `trading212.py` only calls GET endpoints. The order-placement endpoints (`POST /equity/orders`, etc.) are intentionally absent from the file. This constraint must be preserved in any future changes.

### SEC EDGAR User-Agent

Every request to `*.sec.gov` must include `User-Agent: portfolio-analyst contact@stevegerrard.org`. SEC will block requests without a valid User-Agent. This header is defined as `_HEADERS` in both `screener.py` and `ticker_resolver.py`.

### Python Module Invocation

The pipeline must be run as `python -m src.main`, not `python src/main.py`. Running the file directly adds `src/` to `sys.path`, breaking all `from src.data...` absolute imports. The GitHub Actions workflow uses `-m`.

### Screener Cache

The screener is expensive (~45 min for a full S&P 500 run). Results are cached for 8 hours at `cache/screener.json`. In the GitHub Actions environment, this cache is not persisted between runs — the screener runs fresh every morning. If you want to persist it, add the `cache` directory to the `actions/cache` step.

### Ticker Overrides (T212 vs yfinance)

`trading212.py` has its own `_TICKER_OVERRIDES` dict mapping T212 internal identifiers to standard yfinance tickers. This is separate from `ticker_resolver.py`'s runtime `_TICKER_OVERRIDES` dict (which maps failed tickers to their current successors). Both are module-level dicts named the same but in different modules with different purposes.

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
- Steps 1–2 (T212 + market data): 5–20 min depending on pie count
- Step 3–4 (sector + macro): ~1 min
- Step 5 (fundamentals): ~3 min (3 Finnhub calls × 1s sleep × n holdings)
- Step 6 (screener): 45–75 min (cold); 0 min if cached
- Step 7 (Claude): ~30s (5 API calls)
- Step 8 (render): <1s

---

## 10. Known Quirks

**T212 ticker format**: T212's internal identifiers are not standard tickers. `normalise_ticker()` handles most cases via regex, but edge cases require entries in the `_TICKER_OVERRIDES` dict at the top of `trading212.py`. The current overrides include BRK-B, BRK-A, BF-B, META (was FB), X (was TWTR), AVAV (double-underscore), and FP.PA (TotalEnergies on Euronext Paris).

**Finviz column inconsistency**: The `Perf Week` column returns percentage strings (`"-3.50%"`) while other performance columns return decimal fractions (`-0.035`). `_parse_perf_col()` detects the format by checking for `%` in the string value.

**yfinance weekly resampling**: `df.resample("W")` anchors to Sunday. When aligning the ticker's weekly series with SPY's weekly series for Mansfield RS, `reindex(..., method="ffill")` fills any gaps.

**Mansfield RS sign convention**: Positive = outperforming SPY over 52 weeks. Zero crossing upward = early momentum signal. Values of 20+ indicate strong sustained outperformance.

**Holdings parser token budget**: `max_tokens = max(2000, len(positions) * 120)`. With 40 holdings this is 4,800 tokens. If Claude truncates mid-response, increase the multiplier. Any position whose ticker doesn't appear in the parsed output gets a `HOLD` placeholder.

**Dead SPAC positions**: The system holds defunct SPAC tickers (VACQ, NPA, etc.) from pre-merger investments. `ticker_resolver.py` handles these at runtime — yfinance's redirect mechanism resolves most SPAC mergers automatically. If resolution fails, the ticker is skipped with a warning that includes the company name for manual investigation.

---

## 11. Extending the System

### Adding a new data source

1. Create `src/data/yourmodule.py` returning a plain dict.
2. Import and call it in `src/main.py` as a new step.
3. Pass the result to `run_analysis()` and/or `render_dashboard()`.
4. Add any new API key to `.env.example`, GitHub Secrets, and the workflow `env:` block.

### Adding a new Claude prompt

Add a function to `claude_analyst.py` following the pattern of the existing five. Call it from `run_analysis()` and include its output in the returned dict. Update `renderer.py` to pass the new field through to the template, and add a corresponding section to `template.html`.

### Adding a new ticker override (T212 format)

Add to `_TICKER_OVERRIDES` in `src/data/trading212.py`:
```python
"T212_INTERNAL_ID": "STANDARD_TICKER",
```

### Adding a new sector or holding type

Edit `config/sectors.json`. No code changes needed. Sector names must match the `SECTOR_TO_ETF` mapping in `sector_flows.py` if you want alignment scoring for that sector.

### Changing the screener criteria

The composite score weights and pass-1 filters are in `screener.py`. The `_disqualifiers()` function in the same file defines negative screens (high short interest, earnings flags, etc.). Adjust thresholds there. The 8-hour cache means changes take effect on the next cold run.

### Running locally

```bash
cp .env.example .env
# Fill in .env with real keys

pip install -r requirements.txt
python -m src.main
open output/index.html
```

The screener cache at `cache/screener.json` is reused across local runs. Delete it to force a fresh screener pass.
