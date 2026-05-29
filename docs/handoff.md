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
Step 8  run_analysis()            Claude 6-prompt pipeline
Step 9  render_dashboard()        Assemble output/index.html from template
```

Each step returns a plain dict. Steps are independent except that Step 8 receives the outputs of Steps 1–7, and Step 9 receives all outputs.

At the start of Step 8, before calling Claude, `main.py` reads `cache/dismissed_actions.json` (if it exists) and filters to active dismissals (those whose `snoozed_until` date is today or future). These are passed to `run_analysis()` so `todays_actions()` can suppress snoozed items.

At the end of Step 9, `main.py` writes today's final actions list to `cache/last_actions.json` for use by the next run's `is_new` badge logic.

---

## 2. Repository Layout

```
src/
  main.py                     Entry point; orchestrates all 9 steps
  analysis/
    claude_analyst.py         Six Claude prompts + run_analysis() orchestrator
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
    breakout_screener.py      S&P 500 accumulation/breakout screen (5-signal, weighted /10, regime-aware)
    institutional.py          (unused in main pipeline; available for extension)

netlify/
  functions/
    dismiss-action.js         Serverless function: snooze an Action Board row

netlify.toml                  Points Netlify build to netlify/functions/

config/
  sectors.json                Ticker → sector, ticker → holding_type, pie labels

cache/                        Written at runtime, gitignored except where noted
  last_portfolio.json         T212 portfolio snapshot (fallback if API down)
  pie_positions.json          Raw T212 pie positions (4h TTL)
  screener.json               Screener results (8h TTL)
  breakout_screener.json      Breakout screener results (8h TTL)
  sec_company_tickers.json    EDGAR CIK map (7-day TTL)
  last_actions.json           Today's final actions list (force-added to git by CI)
  last_breakout_scores.json   Breakout composite scores from last run (force-added
                              to git by CI; used for score_delta on next run)
  dismissed_actions.json      Snoozed action IDs with expiry dates (written by
                              Netlify function via GitHub Contents API; force-added
                              by Netlify function, never by CI)

output/
  index.html                  Generated dashboard

.github/workflows/
  analyse.yml                 workflow_dispatch only — triggered externally by cron-job.org

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
  - `GET /equity/history/orders` — paginated order history (used by Regret Tracker)
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
- **Two model constants** (in `claude_analyst.py`):
  - `MODEL_REASONING = "claude-opus-4-6"` — decision-grade prompts: `analyse_holdings`, `todays_verdict`, `todays_actions`
  - `MODEL_PROSE = "claude-sonnet-4-6"` — descriptive prompts: `macro_plain_english`, `sector_rotation_narrative`, `growth_opportunities`
- Breakout screener uses one additional batched call for candidate reasoning — always on `claude-sonnet-4-6` (hardcoded, do not change).
- Total token usage (main pipeline): ~4,000–7,000 input + ~3,000–5,000 output per run.

### GitHub Contents API (via Netlify function)
- Used only for action dismissals. The Netlify function reads and writes `cache/dismissed_actions.json` directly to the `main` branch via `PUT /repos/{owner}/{repo}/contents/{path}`.
- Auth: fine-grained PAT (`GH_CONTENTS_PAT`) stored as a Netlify environment variable — **never embedded in the dashboard HTML**.
- The PAT requires: Repository access → `stevegerrard100/portfolio-analyst`, Permissions → Contents → Read and write.

---

## 4. Module Reference

### 4.0 `src/data/regret_tracker.py`

Processes already-fetched order history to build the Regret Tracker tab.

**`get_exited_tickers(order_history, current_tickers)`**
Called in `main.py` immediately after Step 1 (portfolio fetch). Returns a list of ticker symbols that have at least one SELL order from 2026-01-01 onwards and are no longer in the live portfolio. These tickers are added to the `extra` list passed to `fetch_market_data()` (Step 2) so their current prices are included in the main yfinance batch — no separate price fetch is needed.

**`build_regret_tracker(order_history, current_tickers, market_data)`**
Called in `main.py` after Step 7, before Claude analysis. Returns a sorted list of dicts ready for the renderer:

| Field | Description |
|-------|-------------|
| `ticker` | Normalised ticker symbol |
| `company_name` | From the T212 `instrument.fullName` field |
| `sell_date` | YYYY-MM-DD of most recent sell |
| `sell_price` | Per-share fill price in the stock's native currency |
| `current_price` | From `market_data[ticker]["current_price"]` |
| `pct_diff` | `(current / sell − 1) × 100`, rounded to 1 dp |

**Filtering rules:**
- Only orders where `side.upper() == "SELL"`
- Only orders where `filled_at[:10] >= "2026-01-01"`
- Only tickers not present in the current portfolio (fully exited)
- Where a ticker was sold multiple times, keeps the most recent sell by `filled_at` timestamp
- Sorted by `pct_diff` descending (biggest regret first — current price far above sell price)

**`_CUTOFF = "2026-01-01"`** — module-level constant. Changing it will affect both `get_exited_tickers` and `build_regret_tracker`.

**No caching**: current prices come from the main `market_data` dict (already cached at the yfinance level). The order history itself is cached as part of `last_portfolio.json`.

---

### 4.1 `src/analysis/claude_analyst.py`

Six-prompt pipeline, all using the same `SYSTEM_PROMPT` (non-expert investor persona):

| # | Function | Input | Output | max_tokens | Model |
|---|----------|-------|--------|-----------|-------|
| 1 | `macro_plain_english` | FRED macro dict | 2–3 paragraphs | 400 | Sonnet |
| 2 | `sector_rotation_narrative` | Finviz perf + ETF RS | 1–2 paragraphs | 350 | Sonnet |
| 3 | `analyse_holdings` | All positions (batched) | `[{ticker, signal, analysis}]` | max(2000, n×120) | Opus |
| 4 | `growth_opportunities` | Screener top 10 | 2–3 paragraphs | 700 | Sonnet |
| 5 | `todays_actions` | Holdings analysis + breakout + macro + sector | `[{priority, action_type, text, id}]` JSON | 2000 | Opus |
| 6 | `todays_verdict` | Summaries of 1–4 | 1 paragraph ≤100 words | 250 | Opus |

Prompts 1 and 2 run first (independent). Prompt 3 is independent. Prompt 4 uses macro context. Prompt 5 uses all pipeline signals. Prompt 6 synthesises 1–4.

**Holdings parser**: Claude returns one line per holding: `[TICKER] SIGNAL — assessment`. Signals: `HOLD / WATCH / REDUCE / ADD / EXIT`. Any unparsed holding gets a `HOLD` placeholder.

**`todays_actions()` — action types and rules:**

| action_type | Trigger condition | priority |
|-------------|------------------|---------|
| `sell` | EXIT signal, stop loss breached, thesis failed | high |
| `trim` | REDUCE signal, overextended position, sizing risk | medium |
| `add` | Strong ADD signal, pullback to support | low |
| `buy` | High-conviction breakout from screener (stage transition + VCP/accumulation) | low |
| `danger` | VIX > 25 OR HY spread > 500bps OR sharp yield curve inversion OR 3+ macro indicators simultaneously red | high |

Strict exclusions: no "keep watching" / "monitor" items, no sector rotation observations, no general portfolio comments. Maximum 8 items. Output order: danger → sell → trim → buy/add.

Each action has an `id` field (`{ticker}-{action_type}`, e.g. `RGTI-sell`) used for dismissal keying. Actions matching `dismissed_entries` IDs are filtered out before being returned. Critical override: a dismissed action is reinstated (marked `priority: "critical"`) if the position has fallen a further 15% from the `snoozed_price` or the stop loss has been breached.

The function returns a JSON array. The validator drops any item whose `action_type` is not in `{sell, trim, add, buy, danger}`.

### 4.2 `src/data/trading212.py`

**Ticker normalisation** — T212 uses internal identifiers; `normalise_ticker()` converts them:
- `AAPL_US_EQ` → `AAPL` (strip `_US_EQ`)
- `SEMIl_EQ` → `SEMI.L` (lowercase letter = exchange suffix: `l`=`.L`, `d`=`.DE`, `p`=`.PA`)
- `BRK_B_US_EQ` → `BRK-B` (override dict for special cases)

Hard overrides in `_TICKER_OVERRIDES` at the top of the file handle renames and special cases. After normalisation, `_apply_merger_overrides()` applies a second pass of overrides from `ticker_resolver._TICKER_OVERRIDES` — this resolves post-merger SPAC tickers (e.g. DMYI→IONQ, XPOA→QBTS, SNII→RGTI, VACQ→RKLB, NPA→ASTS). The module-level import `from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES` is critical — the import must be at module level, not inside the function, to avoid a silent lazy-import failure pattern.

**Order history endpoint**: `GET /equity/history/orders` returns a cursor-paginated response:
```json
{
  "items": [
    {
      "order": {
        "ticker": "AAPL_US_EQ",
        "side": "sell",
        "filledQuantity": "10.0",
        "status": "FILLED",
        "instrument": { "fullName": "Apple Inc" }
      },
      "fill": {
        "price": "182.50",
        "filledAt": "2026-02-14T10:30:00.000Z",
        "walletImpact": { "netValue": "1825.00", "realisedProfitLoss": "234.50", "fxRate": "0.792" }
      }
    }
  ],
  "nextCursor": null
}
```
Query parameters: `limit` (1–50, default 50), `cursor` (pagination), `ticker` (filter by ticker), `dateCreatedFrom` (ISO8601 — inclusive lower bound). `get_order_history()` passes `dateCreatedFrom=2026-01-01T00:00:00Z` and follows `nextCursor` until it is `null`. `side` values are lowercase (`"buy"` / `"sell"`). `status` is uppercase (`"FILLED"`). `fill.price` is in the stock's native trading currency (USD for US stocks, GBp for LSE stocks).

**Pie cache**: Before fetching pie positions from T212, `fetch_portfolio()` checks `cache/pie_positions.json`. If the file exists and is less than 4 hours old, it's used directly. This avoids the 30s-per-pie rate limit during repeated local runs.

**Position merging**: `_merge_raw_positions()` aggregates direct + pie positions by ticker. If a position dict is missing a `ticker` key, it logs a warning and skips.

**Fallback cache**: If the T212 API is unreachable, `fetch_portfolio()` falls back to `cache/last_portfolio.json`.

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

**Defunct ticker handling**: When `process_ticker()` returns `None`, `fetch_market_data()` calls `resolve_ticker()` from `ticker_resolver.py` and retries with the resolved ticker.

### 4.4 `src/data/ticker_resolver.py`

Dynamic resolution for defunct or renamed tickers. Called only when yfinance returns no data for a ticker.

**Strategy 1 — yfinance redirect**: `yf.Ticker(old).info["symbol"]` sometimes returns the successor ticker.

**Strategy 2 — EDGAR CIK rename**: If the ticker appears in EDGAR's company_tickers.json, fetches the CIK's current submissions to check for a new symbol.

**Strategy 3 — Claude AI fallback**: `_resolve_via_ai(ticker, company_name)` calls Claude with `max_tokens=10`. Returns `None` if the response doesn't match `[A-Z]{1,5}(-[A-Z])?`.

`_TICKER_OVERRIDES: dict[str, str]` is a module-level dict that accumulates resolved mappings. Imported by `trading212.py` and `main.py` at module level as `_MERGER_OVERRIDES`.

**Hard-coded overrides**: DMYI→IONQ, XPOA→QBTS, SNII→RGTI, IIVI→COHR, VACQ→RKLB, NPA→ASTS, UTX→RTX.

### 4.5 `src/data/screener.py`

Three-pass S&P 500 growth screen. Results cached for 8 hours (`cache/screener.json`).

**Pass 1**: RS > 0 AND direction rising. Typically ~120–150 survivors.

**Pass 2**: OHLCV + fundamentals. Composite score from RS, revenue growth, P/S ratio. Captures chart data and reasoning.

**Pass 3**: CIK-based deduplication (GOOG/GOOGL share a CIK; higher-RS ticker wins).

Portfolio tickers are excluded from output.

**Cache schema versioning**: `CACHE_SCHEMA_VERSION = 2`. Written as `"schema_version"` in the result dict. On read, if the found version ≠ 2, the cache is discarded and a fresh screen runs automatically — no manual deletion needed.

### 4.6 `src/data/breakout_screener.py`

Five-signal accumulation/early-breakout screen over the S&P 500. Weighted composite score out of 10. Results cached for 8 hours (`cache/breakout_screener.json`).

**Gate sequence (R1)** — runs in this order to minimise API quota usage:
1. Weekly pre-filter — batch yfinance download (~500 tickers, fast)
2. Daily signal check — per-ticker yfinance (~60 tickers, moderate)
3. Finnhub earnings gate — per-survivor only (~25–40 tickers, slow)

**Five signals and weights:**

| Signal key | Description | Points |
|-----------|-------------|--------|
| `stage_transition` | Price above rising 150-day SMA + RS crossed above zero | 2.0 |
| `rs_leading` | RS within 8% of 52w high while price is >5% below its own 52w high | 2.0 |
| `vcp` | Four 20-day windows with contracting swings, declining volume, and 4-day final-window vol avg ≥20% below 50-day vol SMA | 1.5 |
| `volume_accumulation` | Up-day vol / down-day vol ratio ≥ 1.5× over trailing 20 sessions, within flat base (range ≤15%) | 1.0 |
| `pivot_proximity` | Price within 5% below to 0.5% above the Base Pivot High | 1.0 |

**Base quality bonus**: +0.5 pts if base_weeks ≥ 6 AND base_depth_pct ≤ 30%.

**Scoring**: `(raw_score + bonus) / 8.0 * 10.0`, rounded to 1dp, capped at 10.

**Key computed values:**

- **Base Pivot High (BPH)**: highest weekly close over the past 26 weeks. Used by pivot_proximity signal and base stats.
- **Base stats** (surfaced in dashboard table):
  - `base_weeks` — weeks since the BPH was set
  - `base_depth_pct` — (BPH − base_low) / BPH × 100
  - `base_tightness` — std-dev / mean of weekly closes in the base (lower = tighter)

**Market regime** (computed once per run, stored in result dict):
- SPY 200-day SMA + ^VIX (yfinance) + HY spread (FRED BAMLH0A0HYM2, if key available)
- `bear`: SPY below 200d SMA OR VIX ≥ 35 OR HY spread ≥ 500 bps
- `bull`: SPY above 200d SMA AND VIX < 25 AND HY spread < 400 bps
- `caution`: everything else; also the fallback on data errors

**High Conviction flag**: score ≥ 7.0 AND `rs_leading` AND `stage_transition` AND regime ≠ `"bear"`. High Conviction is suppressed entirely in bear regime. Computed in `run_breakout_screener()` before cache write — not at render time.

**Leadership Watch flag (regime_watchlist)**: score ≥ 7.0 AND `rs_leading` AND `stage_transition` AND regime == `"bear"`. Candidates meeting this criterion were previously suppressed in bear regime; they are now included with a muted-blue "Leadership Watch" badge instead of a green "High Conviction" badge.

**Finnhub earnings gate**: calls `earnings_calendar(_from=today, to=today+21d, symbol=ticker)` for each yfinance survivor. Tickers with an upcoming earnings event are **annotated** (not excluded) with `earnings_soon=True` and `earnings_date` (YYYY-MM-DD string). Displayed in the dashboard as an amber `⚠ Earnings` badge with a date tooltip. 1.1s sleep per call (free tier: 60/min). Gate skipped gracefully if `FINNHUB_API_KEY` absent. All candidates always have `earnings_soon` and `earnings_date` keys; defaults are `False` / `None`.

**Score delta**: `LAST_SCORES_CACHE = CACHE_DIR / "last_breakout_scores.json"`. At the start of `run_breakout_screener()`, loads a `{ticker: score}` dict from this file (if it exists). `score_delta = round(current_score - prev_score, 1)` per candidate, `None` if ticker was not in the previous run. The snapshot is written by `main.py` (after the `if fast / else` block, before Step 8) unconditionally — covers both fresh runs and cache hits.

**AI reasoning** (structured output): Single batched Claude API call on `claude-sonnet-4-6` (hardcoded — do not change). `max_tokens = min(8192, 280 * len(candidates))`. The prompt requests structured output with `###TICKER` blocks containing SETUP / RISK / MATURITY labeled sections. The parser uses `re.split(r"\n?###([A-Z][A-Z0-9\-]{0,5})\n", text)` to split per ticker, then targeted regex per field. Parse failures populate `setup_strength`, `key_risk`, `maturity` as `None` (no exception raised). These fields are always present in every candidate dict.

**Cache schema versioning**: `CACHE_SCHEMA_VERSION = 3`. Written as `"schema_version"` in the result dict. On read, if the found version ≠ 3, the cache is discarded and a fresh screen runs automatically — no manual deletion needed. The `--fast` mode in `main.py` also checks the version and exits with an error if the cache is stale.

**Result dict keys**: `schema_version`, `candidates`, `screened_at`, `universe_size`, `initial_count`, `qualified_count`, `regime`.

**Per-candidate dict keys** (all always present): `ticker`, `company_name`, `sector`, `mansfield_rs`, `composite_score`, `score_delta`, `high_conviction`, `regime_watchlist`, `earnings_soon`, `earnings_date`, `signals`, `reasoning`, `setup_strength`, `key_risk`, `maturity`, `base_weeks`, `base_depth_pct`, `base_tightness`, `stop_loss`, `ohlcv_daily`, `ohlcv_weekly`, `mrs_daily`, `mrs_weekly`.

### 4.7 `src/data/fundamentals.py`

| Field | Source |
|-------|--------|
| FCF, FCF yield | yfinance cashflow statement |
| Revenue growth (YoY) | yfinance financials |
| Net debt/EBITDA | yfinance balance sheet |
| Short interest % | yfinance info |
| Forward PE, P/B, PEG, ROE, gross margin | Finnhub basic financials |
| Insider sentiment (MSPR, 90-day) | Finnhub insider transactions |
| Earnings surprises (last 4) | Finnhub company earnings |

yfinance calls: `ThreadPoolExecutor(max_workers=min(8, n_tickers))`. Finnhub: sequential, 1.0s sleep.

### 4.8 `src/data/macro.py`

Fetches 7 FRED series. Derives:
- **Yield curve status**: `positive` (>50bps), `flat` (0–50bps), `inverted` (<0bps)
- **HY regime**: `tight` (<300bps), `normal` (300–500bps), `stress` (>500bps)
- **Rate trajectory**: `easing` / `on hold` / `tightening` (based on 12m Fed Funds change)
- **Credit stress flag**: True if HY spread > 500bps

### 4.9 `src/data/sector_flows.py`

Two inputs → one output dict:
1. **Finviz sector performance** — 11 S&P sectors: 1d/1w/1m/3m/6m/1y
2. **SPDR ETF Mansfield RS** — from pre-fetched `market_data` dict. Rotation signals: `early_rotation`, `momentum_building`, `rotation_peaking`.

Also computes a **portfolio alignment score**. The `finviz_performance` list drives the Today's Brief sector heatmap.

### 4.10 `src/dashboard/renderer.py`

Assembles the `data` dict that replaces `{{DASHBOARD_DATA}}` in `template.html`.

**`_macro_pills(macro)`** — converts FRED series data into `{label, value, status}` dicts:

| Pill | Green | Amber | Red |
|------|-------|-------|-----|
| 10yr Yield | < 3.5% | 3.5–5% | > 5% |
| 2yr Yield | < 4% | 4–5% | > 5% |
| Yield Curve | positive | flat | inverted |
| HY Spread | < 300 bps | 300–500 bps | > 500 bps |
| Fed Funds | < 3% | 3–5% | > 5% |
| VIX | < 15 | 15–25 | > 25 |
| CPI YoY | < 2.5% | 2.5–4% | > 4% |

**`_sector_heatmap(sector_flows)`** — weekly returns → sorted `{sector, change_1w}` list.

**`_build_today_actions(raw_actions, market_data)`** — adds `color` field to each action (`sell/trim/danger → "red"`, `add/buy → "green"`). Also computes `is_new` flag by comparing each action's `id` against `cache/last_actions.json` from the previous run.

**`_dismiss_url`** — embedded into the data dict from the `NETLIFY_DISMISS_URL` environment variable. If the env var is not set (e.g. local runs), the value is an empty string and all snooze buttons are hidden in the dashboard.

**`_ACTION_COLORS`**: `sell → red`, `trim → red`, `add → green`, `buy → green`, `danger → red`.

**`_TICKER_TO_SECTOR`** — module-level dict loaded from `config/sectors.json` at import time. Used by `_sector_rs_signal()` to resolve sector names for breakout candidates.

**`_sector_rs_signal(ticker, candidate_sector, sector_flows)`** — returns `"leading"`, `"neutral"`, or `"lagging"` based on the Mansfield RS of the candidate's sector SPDR ETF proxy. Lookup order: `_TICKER_TO_SECTOR[ticker]` → `candidate_sector` → `"Unknown"`. The `SECTOR_TO_ETF` mapping is imported lazily inside the function (`from src.data.sector_flows import SECTOR_TO_ETF`) to avoid a circular import at module load time. Returns `"neutral"` if sector is unknown, unmapped, or ETF data is absent.

Full function signature:
```python
def render_dashboard(
    analysis, portfolio, market_data, screener, breakout,
    macro, sector_flows, regret_tracker,
    output_path="output/index.html"
) -> None
```

Log line at the top of `render_dashboard()` confirms the value of `NETLIFY_DISMISS_URL` for debugging CI runs.

**Breakout candidate whitelist** — `renderer.py` explicitly names every field it passes from the breakout cache to the dashboard data dict. Fields not listed are silently dropped. Current whitelist: `ticker`, `company_name`, `sector`, `mansfield_rs`, `composite_score`, `score_delta`, `reasoning`, `signals`, `high_conviction`, `regime_watchlist`, `earnings_soon`, `earnings_date`, `sector_rs_signal` (computed by `_sector_rs_signal()`), `setup_strength`, `key_risk`, `maturity`, `base_weeks`, `base_depth_pct`, `base_tightness`, `stop_loss`, `ohlcv_daily`, `ohlcv_weekly`, `mrs_daily`, `mrs_weekly`. When adding new fields to `breakout_screener.py`, add them here too or they will not reach the template.

### 4.11 `netlify/functions/dismiss-action.js`

Serverless POST handler deployed to Netlify. Accepts `{id, days, snoozed_price?}` from the browser, updates `cache/dismissed_actions.json` in the GitHub repo via the Contents API, and returns `{ok: true, id, snoozed_until}`.

The PAT (`GH_CONTENTS_PAT`) lives only in Netlify's environment — it is never embedded in the dashboard HTML.

CORS headers (`Access-Control-Allow-Origin: *`) allow cross-origin calls from the GitHub Pages domain. Handles `OPTIONS` preflight.

**Setup required** (Netlify → Site settings → Environment variables):
- `GH_CONTENTS_PAT` — fine-grained PAT, Contents read+write on this repo only

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
| `NETLIFY_DISMISS_URL` | renderer.py | Full URL of dismiss-action Netlify function |
| `GH_CONTENTS_PAT` | Netlify env only (not in CI) | Fine-grained PAT for dismissed_actions.json writes |

`NETLIFY_DISMISS_URL` example: `https://your-site.netlify.app/.netlify/functions/dismiss-action`. If not set, `dismiss_url` in the dashboard data is an empty string and all snooze buttons are hidden.

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

### Tab Layout (4 tabs)

**Tab 1 — Today's Brief** (default landing tab):
- **Action Board** card — appears first, hidden entirely if `today_actions` is empty. See Action Board section below.
- **Today's Verdict** card — Claude's one-paragraph overall assessment
- **Macro Environment** card — 7 colour-coded data pills above the macro narrative text
- **Sector Rotation** card — 11-sector heatmap grid above the sector narrative text
- **Top Momentum Picks** — top 3 screener candidates as teaser cards with 30-day SVG sparkline charts and a "See all N opportunities →" link to Tab 3

**Tab 2 — My Portfolio**:
- **Sector Allocation** — donut chart (Chart.js) + allocation table, lazy-initialised on first portfolio tab visit
- **Account stats bar** — Total Value, Unrealised P&L, Realised P&L, Position count
- **Holdings list** — compact table, one row per stock (see Holdings List below)
- Signal filter buttons (ALL / ADD / WATCH / HOLD / REDUCE / EXIT) with counts

**Tab 4 — Regret Tracker**:
- Single card with a description line and a plain table
- Columns: Ticker, Company, Sell Date, Sell Price, Current Price, % Change
- % Change cell: red (`neg` class) if current price > sell price (sold too early), green (`pos` class) if current price < sell price (sold well)
- Sorted by % Change descending — biggest regret at the top
- Empty state: short message shown if `DATA.regret_tracker` is empty
- No charts, no Claude narrative — pure data table
- Prices are in each stock's native trading currency (no conversion applied)

**Tab 3 — Opportunities**:
- **Growth Opportunities** — full Claude opportunities narrative (no truncation)
- **Momentum Opportunities** table — top 10 screener candidates with expandable chart rows
- **Breakout Watch List** — top 15 breakout candidates with expandable chart rows:
  - Regime warning banner (red in bear, amber in caution, hidden in bull) appears above the table
  - Table columns (9 total): Ticker, Company, Sector, Mansfield RS, Signals (badge per signal), Score /10, Base Wks, Depth %, Sector RS
  - Columns 7–9 (Base Wks, Depth %, Sector RS) are hidden on mobile via `.bk-table th:nth-child(n+7), .bk-table td:nth-child(n+7) { display: none }`
  - **High Conviction** badge (green): score ≥ 7.0 + `rs_leading` + `stage_transition` + regime ≠ bear — row highlighted green
  - **Leadership Watch** badge (muted blue, `.bk-lw-badge`): score ≥ 7.0 + `rs_leading` + `stage_transition` + regime = bear — these candidates were previously suppressed; now shown with a muted-blue badge, no green highlight
  - **⚠ Earnings** badge (amber, `.bk-earn-badge`): `earnings_soon=True`; hover/title shows `earnings_date` (YYYY-MM-DD)
  - **Score delta**: `↑ +N` (green) or `↓ −N` (red) rendered inline next to the score when `score_delta != null && score_delta !== 0`; `null` means the ticker is new since the last run (no delta available)
  - **Expanded row** (colspan="9") shows structured AI reasoning:
    - Primary reasoning: `setup_strength` field if non-null, else falls back to `reasoning` (plain technical text)
    - Key risk: amber `⚠ Risk:` label + `key_risk` text (hidden if `key_risk` is null)
    - Maturity badge: Early (blue) / Developing (amber) / Extended (muted) — hidden if `maturity` is null
    - Base stats, stop loss, chart (daily/weekly toggle) as before

### Action Board

The Action Board card appears at the top of Tab 1 whenever `DATA.today_actions` is non-empty.

**Card structure:**
- Header row: left-aligned date (`DATA.meta.date_str`), right-aligned summary ("N actions today · M high priority")
- One row per action, with a 3px coloured left border (red/green), an action-type badge (SELL / TRIM / ADD / BUY / DANGER), optional badges, and the Claude-written sentence

**Badges:**
- `NEW` (blue) — shown if `action.is_new` is true (action's `id` was absent from yesterday's `cache/last_actions.json`)
- `⚠ CRITICAL` (pulsing red) — shown if `action.priority === "critical"` (reinstated dismissed action where the position has fallen further or stop loss was breached). Critical actions have no snooze button.

**Snooze button (⏰):**
- Shown only if `DATA.dismiss_url` is non-empty and the action is not CRITICAL
- Dropdown options: 7 days / 30 days / 90 days / Permanently
- On selection, POSTs `{id, days, snoozed_price}` to `DATA.dismiss_url` (the Netlify function)
- On success, reads `snoozed_until` from the response, writes `{id: snoozed_until}` to `localStorage` under key `portfolioAnalyst_dismissed`, then fades the row out
- On page refresh: `renderActionBoard()` calls `_lsPrune()` to remove expired entries, then hides any row whose `id` is in the active localStorage map — **before the user sees the row**. This is the persistence mechanism for same-day refreshes. Server-side filtering via `dismissed_actions.json` applies from the next morning's pipeline run onward.

**Border and badge colours:**
- `sell`, `trim`, `danger` → red (`#ef4444`)
- `add`, `buy` → green (`#22c55e`)

### Holdings List

Each holding is a `<tr class="h-row">` in a `<table class="h-list-table">`. Columns: Ticker, Sector, P&L%, P&L£, Signal badge, RS score + inline 2px bar, SMA50 arrow, MACD arrow, Stop loss price, 52w distance from high.

Clicking a row expands a sibling `<div class="h-expand-content">` (rendered **outside** the scrollable table wrapper) containing the Claude analysis text and a candlestick chart.

Three subsections: **Individual Holdings**, **Pie Holdings** (grouped by pie name), **ETFs**.

Filter buttons toggle row visibility with `display: none` — preserves chart instances across filter changes.

### Charts

TradingView Lightweight Charts v4.1.3 (CDN). Three series per chart: candlestick, volume histogram, SMA50 line. Stop loss as a dashed horizontal price line.

**Holdings charts** use explicit `width`/`height` computed from the expand-content div's `clientWidth`. A `resize` event listener updates chart width on viewport changes.

**Screener/breakout charts** use `autoSize: true`.

Chart instances cached in `chartInstances`, `screenerChartInstances`, `breakoutChartInstances`.

### Mobile Support

`isMobile`: UA matches `/Mobi|Android|iPhone|iPad|iPod/i` OR `window.innerWidth < 768`. On mobile: 250px chart height, wider bar spacing, chart controls stack vertically, rightmost holdings columns hidden.

### Markdown Stripping

`md(text)` processes all Claude narrative text:
1. Strips entire sentences containing "informational purposes only" or "not financial advice" (sentence-level regex), re-adds one clean footer line per section
2. Strips `##/###` heading markers (keeps text)
3. Removes horizontal rules
4. Strips `**bold**` and `*italic*` markers

`firstSentences(text, n)` truncates the opportunities intro to 3 sentences.

### Sector Donut

Chart.js v4 (CDN). Lazy-initialised on first My Portfolio tab visit via `ensureSectorChart()`.

---

## 7. CI/CD

`.github/workflows/analyse.yml` triggers on:
- **`workflow_dispatch`** only — the daily run is fired externally by **cron-job.org** at **07:05 UTC on weekdays**, which calls the GitHub API to dispatch the workflow.

> ⚠️ **Do not add a `schedule:` block to `analyse.yml`.** GitHub's built-in cron and cron-job.org would both fire, causing duplicate runs, double API spend, and race conditions on the git push steps. The workflow intentionally has no `schedule:` trigger.

Steps:
1. Checkout repo
2. Set up Python 3.11
3. Cache pip packages (keyed on `requirements.txt` hash)
4. Install dependencies
5. **Get today's date** — writes UTC date to `$GITHUB_OUTPUT` for the screener cache key
6. **Restore screener cache** — `actions/cache@v4`, paths `cache/screener.json` + `cache/breakout_screener.json`, keyed on `screener-{os}-{YYYY-MM-DD}`. No `restore-keys` — strict date match ensures next-day runs always start fresh. Same-day re-runs (e.g. manual re-trigger) skip the 45-min screener entirely.
7. **Run analysis pipeline** — `python -m src.main` with secrets: `T212_API_KEY`, `T212_API_SECRET`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`, `NETLIFY_DISMISS_URL`
8. **Persist actions cache** — `git add -f cache/last_actions.json` and `git add -f cache/last_breakout_scores.json` (force-add bypasses `.gitignore`), commit with `[skip ci]` guard, `git pull --rebase origin main`, then push. The `-f` flag is required because `cache/` is in `.gitignore`. **Note**: `git stash` / `git stash pop` are NOT used here — the working tree is always clean after the commit (only staged files were changed), and `git stash pop` on a clean tree exits with code 1 ("No stash entries found"), which would fail the step.
9. **Deploy to GitHub Pages** — `peaceiris/actions-gh-pages@v4`, publishes `./output/` to `{repo}/markets/`, `keep_files: true`

`timeout-minutes: 120` covers T212 pie fetches (32s/pie) and the screener (45+ min).

**`cache/dismissed_actions.json`** is NOT written by CI. It is written by the Netlify function directly to the repo via the GitHub Contents API whenever a user snoozes an action. CI reads it on the next run.

**`cache/last_actions.json`** is written at the end of `src/main.py` and committed by the Persist actions cache step. It is used by the next run to compute `is_new` badges.

**`cache/last_breakout_scores.json`** is written by `src/main.py` immediately after the `if fast / else` block (before Step 8), unconditionally — covers both fresh runs and cache hits. Contains `{ticker: composite_score}` for every candidate in the current run. Committed by the same Persist actions cache step. Used at the start of the next `run_breakout_screener()` call to compute per-candidate `score_delta`.

Secrets required: `T212_API_KEY`, `T212_API_SECRET`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`, `NETLIFY_DISMISS_URL`. `GITHUB_TOKEN` is provided automatically.

---

## 8. Key Constraints and Design Decisions

### Read-Only T212

**The system must never place, modify, or cancel any orders.** `trading212.py` only calls GET endpoints. This constraint must be preserved in any future changes.

### SEC EDGAR User-Agent

Every request to `*.sec.gov` must include `User-Agent: portfolio-analyst contact@stevegerrard.org`.

### Python Module Invocation

Must be run as `python -m src.main`, not `python src/main.py`. Running the file directly breaks all `from src.data...` absolute imports.

### Ticker Override Import — Must Be Module-Level

```python
from src.data.ticker_resolver import _TICKER_OVERRIDES as _MERGER_OVERRIDES
```
Must be at module level in `trading212.py` and `main.py`. Inside a function, Python's `except Exception: return ticker` will silently catch import errors.

### Breakout AI Batch Call — Token Cap

`max_tokens = min(8192, 280 * len(candidates))`. The model hard limit is 8,192 output tokens. The per-candidate budget is 280 tokens to accommodate the structured SETUP/RISK/MATURITY output format. With 29 candidates the cap just fits. If candidate count grows significantly, reduce the per-candidate budget. The breakout screener's AI call always uses `claude-sonnet-4-6` — do not change this to Opus.

### Breakout and Screener Cache — Schema Versioning

Both `breakout_screener.py` (`CACHE_SCHEMA_VERSION = 3`) and `screener.py` (`CACHE_SCHEMA_VERSION = 2`) embed a `"schema_version"` key in their result dicts. On the next run, the cache read block checks the found version against the expected constant. If they differ, the cache is discarded and a fresh screen runs automatically. **Manual cache deletion is no longer needed** after schema changes — bumping `CACHE_SCHEMA_VERSION` in the module is sufficient. The `--fast` mode in `main.py` also performs the same version check and exits with an error if the cached file is stale, so stale caches cannot be used in fast mode either.

### Finnhub Earnings Gate — Gate Ordering and Annotation

The Finnhub earnings gate in `breakout_screener.py` runs **after** all yfinance gates, not before. This is intentional (R1): yfinance gates reduce ~500 tickers to ~25–40 survivors, then Finnhub is called only for those survivors. Reversing the order would burn ~500 Finnhub calls per run against the free-tier 60/min limit.

The gate **annotates** candidates with upcoming earnings (within 21 days) rather than excluding them. Candidates get `earnings_soon=True` and `earnings_date` (YYYY-MM-DD). This is displayed as an amber `⚠ Earnings` badge in the dashboard. All candidates have both fields (defaults: `False` / `None`).

### Holdings Charts Outside the Scrollable Table

Expand content is a `<div class="h-expand-content">` sibling of the table wrapper, not inside `<td>`. This prevents TradingView charts from inheriting the table's full overflow width.

### Sector Donut — Lazy Init on Hidden Canvas

`new Chart(canvas)` on a hidden canvas produces a zero-size chart. Guarded by `sectorChartCreated` flag, initialised only when My Portfolio tab is first activated.

### Action Dismissal — Client-Side vs Server-Side

The static HTML has all actions baked into `DATA.today_actions` at render time. Server-side filtering (via `dismissed_actions.json` in `main.py`) only applies on the next morning's pipeline run. For same-day refresh persistence, dismissals are also written to `localStorage` (`portfolioAnalyst_dismissed` key, value `{id: snoozed_until}`). On page load, `_lsPrune()` removes expired entries, and any active dismissed ID has its row hidden before it becomes visible.

### PAT Never in HTML

The GitHub Contents API PAT (`GH_CONTENTS_PAT`) is stored only in Netlify's environment. The dashboard embeds only a `dismiss_url` pointing to the Netlify function. The function handles all GitHub API calls server-side.

### `[skip ci]` in Dismiss Commit Messages

The Netlify function's commit message for updating `dismissed_actions.json` includes `[skip ci]` to prevent triggering a new pipeline run. Similarly, the CI step that commits `last_actions.json` uses `[skip ci]`.

### `cache/` in `.gitignore` — Force-Add for Tracked Files

`cache/` is gitignored. `cache/last_actions.json` and `cache/last_breakout_scores.json` are committed by CI using `git add -f`. `cache/dismissed_actions.json` is committed by the Netlify function via the GitHub Contents API (which bypasses `.gitignore`). None of these files should be added to a `.gitignore` allowlist — force-add in CI is intentional.

---

## 9. Rate Limits and Timing

| Source | Limit | Enforced by |
|--------|-------|-------------|
| Trading 212 pies | 1 req / 30s | `trading212.py` sleep |
| yfinance | ~2 req/s practical | 0.5s sleep in `_download()` |
| Finnhub (free tier) | 60 req/min | 1.0s sleep in `fundamentals.py`; 1.1s sleep in `breakout_screener.py` earnings gate |
| FRED | 120 req/min | No sleep needed |
| Finviz | No stated limit | Single request per run |
| SEC EDGAR | 10 req/s | 0.3s sleep in `ticker_resolver.py` |
| GitHub Contents API | 5,000 req/hr (PAT) | One call per snooze — no concern |

**Typical step durations** (approximate):
- Step 1 (T212 portfolio): 5–15 min; 0 if pie cache warm
- Step 2 (market data): 2–5 min
- Steps 3–4 (sector + macro): ~1 min
- Step 5 (fundamentals): ~3 min
- Step 6 (screener): 45–75 min cold; 0 if cached
- Step 7 (breakout screener): 15–30 min cold (includes Finnhub earnings gate: ~1.1s × survivors); 0 if cached
- Step 8 (Claude analysis): ~45s (6 API calls + 1 batch breakout call)
- Step 9 (render): <1s

---

## 10. Known Quirks

**T212 ticker format**: Edge cases require entries in `_TICKER_OVERRIDES` in `trading212.py`. Current overrides include BRK-B, BRK-A, BF-B, META, X, AVAV, FP.PA, and post-merger SPACs.

**Finviz column inconsistency**: `Perf Week` returns `"-3.50%"` strings; other columns return decimal fractions. `_parse_perf_col()` normalises both.

**yfinance weekly resampling**: `df.resample("W")` anchors to Sunday. `reindex(..., method="ffill")` fills gaps when aligning with SPY.

**Mansfield RS sign convention**: Positive = outperforming SPY over 52 weeks.

**Holdings parser token budget**: `max(2000, n * 120)` tokens. With 40 holdings = 4,800. If Claude truncates mid-response, increase the multiplier.

**Dead SPAC positions**: Add new cases to `ticker_resolver._TICKER_OVERRIDES` at the top of the file.

**CPI YoY calculation**: FRED's `CPIAUCSL` is an index level, not a percentage. YoY computed as `(current / prior_12m - 1) * 100`.

**`md()` disclaimer regex is sentence-level**: `[^.!?\n]*(?:informational purposes only|not financial advice)[^.!?\n]*[.!?]?\s*` captures the entire surrounding sentence to prevent orphaned fragments.

**Action dismissals on first snooze**: `cache/dismissed_actions.json` does not exist until the first user snooze. `main.py` logs "not found — no active dismissals" for this case. The Netlify function creates the file on first write.

**Netlify function CORS**: The `Access-Control-Allow-Origin: *` header is intentionally broad because the calling origin (GitHub Pages domain) varies. If this is a concern, restrict to `https://stevegerrard100.github.io`.

---

## 11. Extending the System

### Adding a new data source

1. Create `src/data/yourmodule.py` returning a plain dict.
2. Import and call it in `src/main.py` as a new step.
3. Pass the result to `run_analysis()` and/or `render_dashboard()`.
4. Add any new API key to `.env.example`, GitHub Secrets, and the workflow `env:` block.

### Adding a new Claude prompt

Add a function to `claude_analyst.py` following the existing pattern. Call it from `run_analysis()` and include its output in the returned dict. Update `renderer.py` to pass the new field through, and add a section to `template.html`. Update the prompt count in the `run_analysis()` docstring and `main.py` step label.

### Adding a new action type

1. Add the type to the `todays_actions()` prompt text in `claude_analyst.py`
2. Add it to the `allowed_types` set in the validator
3. Add it to `_ACTION_COLORS` in `renderer.py`
4. Add its badge label to `BADGE_LABEL` in `template.html`

### Adding a new ticker override (T212 format or SPAC merger)

For T212 identifier quirks: add to `_TICKER_OVERRIDES` in `src/data/trading212.py`.
For post-merger SPAC resolution: add to `_TICKER_OVERRIDES` in `src/data/ticker_resolver.py`.

### Adding a new sector or holding type

Edit `config/sectors.json`. No code changes needed. Sector names must match `SECTOR_TO_ETF` in `sector_flows.py` for alignment scoring.

### Changing the screener criteria

Composite score weights and pass-1 filters are in `screener.py`. `_disqualifiers()` defines negative screens. 8h cache means changes take effect on the next cold run.

### Changing breakout signal thresholds

Each signal has its own function in `breakout_screener.py`. Signal weights are in `_SIGNAL_WEIGHTS`. High Conviction and Leadership Watch thresholds (currently 7.0) are in `run_breakout_screener()`. Regime thresholds are in `_assess_market_regime()`.

If you rename a signal key: update `BREAKOUT_SIGNAL_LABELS` in `template.html`, and the `high_conviction` / `regime_watchlist` computations in `run_breakout_screener()`. Both flags are computed in the screener before cache write — not at render time.

After any structural change (new fields, changed scoring formula, new result dict keys), bump `CACHE_SCHEMA_VERSION` in `breakout_screener.py`. The cache will be auto-discarded on the next run — no manual deletion needed.

### Running locally

```bash
cp .env.example .env
# Fill in .env — NETLIFY_DISMISS_URL is optional for local runs (snooze buttons hidden if unset)

pip install -r requirements.txt
python -m src.main
open output/index.html
```

Both screener caches are auto-invalidated when their `schema_version` key doesn't match the current module constant — no manual deletion needed after code changes. To force a fresh screen regardless (e.g. to re-run with updated data mid-day), delete `cache/screener.json` and/or `cache/breakout_screener.json`. Delete `cache/pie_positions.json` to force a fresh T212 fetch.
