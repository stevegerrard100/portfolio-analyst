# Breakout Screener — Improvement Requirements Specification

## Context

You are working on an automated daily investment analysis system. The full system
architecture and module reference is documented in `docs/handoff.md` — read that
first before making any changes.

The scope of this task is limited to six specific improvements to the breakout
screener pipeline and its dashboard presentation. Do not refactor unrelated code.

---

## Improvement 1 — Cache Schema Versioning

### Problem

`cache/breakout_screener.json` and `cache/screener.json` are invalidated silently
when their result dict schemas change. The current process requires manual deletion
of these files after any code change. This is error-prone and undocumented at
runtime.

### Requirements

**R1.1** Add an integer constant `CACHE_SCHEMA_VERSION` at module level in both
`src/data/breakout_screener.py` and `src/data/screener.py`. Start both at `2` (to
distinguish from pre-versioned caches which have no version field).

**R1.2** When writing a cache file, include `"schema_version": CACHE_SCHEMA_VERSION`
as a top-level key in the JSON dict before writing.

**R1.3** When loading a cache file, after JSON parsing, compare the loaded
`schema_version` value against the module constant. If absent or mismatched, log
an info-level message — `"Breakout cache schema version mismatch (found {x},
expected {y}) — discarding"` — and return `None` so the caller triggers a fresh
run. Do not raise an exception.

**R1.4** The version check must happen before any other processing of the loaded
dict. It must not silently fall through on a KeyError.

**R1.5** Increment `CACHE_SCHEMA_VERSION` in `breakout_screener.py` as part of
this task, since improvements 2–5 below all change the result dict schema.

**R1.6** Do not add schema versioning to any other cache files in this task
(`screener.json` versioning is included; all other caches are out of scope).

---

## Improvement 2 — Earnings-Risk Candidates Labelled, Not Excluded

### Problem

The Finnhub earnings gate in `breakout_screener.py` currently excludes any stock
with an earnings event within 21 days. For a human investor, these are often the
most interesting candidates — they just carry event risk that should be visible,
not hidden.

### Requirements

**R2.1** Remove the hard exclusion of earnings-risk tickers from the candidate
list. Tickers with an earnings event within 21 days must be retained in the output.

**R2.2** Add two fields to each candidate dict:
- `earnings_soon: bool` — `True` if an earnings event exists within 21 days of
  the run date, `False` otherwise.
- `earnings_date: str | None` — ISO date string of the nearest upcoming earnings
  event if one exists within the window, otherwise `None`.

**R2.3** These fields must be populated for all candidates, including those that
reach the final list without passing through the Finnhub gate (i.e. if the gate
is skipped because `FINNHUB_API_KEY` is absent, both fields default to `False`
and `None` respectively).

**R2.4** The Finnhub API call structure and rate limiting (1.1s sleep per call)
must not change. Only the action taken on a positive result changes — from
exclusion to field annotation.

**R2.5** In `src/dashboard/template.html`, on the Breakout Watch List table, add
an `⚠ Earnings` badge to any row where `earnings_soon` is `True`. The badge
should be amber/yellow, consistent with the existing warning colour palette.
Hovering the badge should show a tooltip with the `earnings_date` value.

**R2.6** The existing regime warning banner logic above the breakout table must
not be affected.

---

## Improvement 3 — Bear Market Leaders Shown as Watchlist, Not Suppressed

### Problem

In bear market regime, all High Conviction flags are suppressed. This hides stocks
that are showing genuine relative strength leadership before the market turns —
exactly the names a forward-looking investor wants to monitor.

### Requirements

**R3.1** In `run_breakout_screener()`, add a new boolean field `regime_watchlist`
to each candidate dict. This field is `True` when all of the following apply:
- The candidate scores ≥ 7.0
- The candidate has both `rs_leading` and `stage_transition` signals
- The current market regime is `"bear"`

In all other cases `regime_watchlist` is `False`.

**R3.2** `high_conviction` must remain `False` for all candidates when regime is
`"bear"`. The `regime_watchlist` flag is additive — it does not alter
`high_conviction` logic.

**R3.3** In `template.html`, on the Breakout Watch List table:
- Rows where `regime_watchlist` is `True` receive a distinct visual treatment:
  a `Leadership Watch` badge in a muted blue or grey colour (not green, to avoid
  implying actionability).
- These rows must not receive the green High Conviction highlight.
- The existing `High Conviction` badge and green row highlight apply only when
  `high_conviction` is `True`, which cannot occur in bear regime — no change to
  that logic.

**R3.4** The bear-market regime warning banner above the breakout table must
remain. The addition of `regime_watchlist` rows does not suppress or modify it.

---

## Improvement 4 — Score Delta Over Time

### Problem

The composite score for each breakout candidate is static per run. A candidate
improving from 4.0 to 7.5 over two weeks is more interesting than one sitting
static at 7.5. There is currently no way to see this momentum within the screen.

### Requirements

**R4.1** At the end of each pipeline run, after `run_breakout_screener()` returns
its result dict, `src/main.py` must write a score snapshot file to
`cache/last_breakout_scores.json`. The format is a flat JSON object mapping ticker
symbol to composite score:
```json
{"AAPL": 7.2, "NVDA": 8.5, "MSFT": 6.1}
```
This file must be written whether or not the breakout screener ran fresh or from
cache.

**R4.2** At the start of `run_breakout_screener()`, before any processing, load
`cache/last_breakout_scores.json` if it exists. If it does not exist, log
`"No previous breakout scores found — delta unavailable"` and continue with an
empty dict.

**R4.3** For each candidate in the final list, compute `score_delta` as:
`round(current_score - previous_score, 1)` where `previous_score` is the score
for that ticker from the loaded snapshot. If the ticker is not present in the
snapshot (new entrant to the screen), set `score_delta` to `None`.

**R4.4** Add `score_delta: float | None` to each candidate dict.

**R4.5** In `template.html`, in the Breakout Watch List score column, display the
delta alongside the score in a compact format:
- `score_delta > 0`: show `↑ +{delta}` in green text
- `score_delta < 0`: show `↓ {delta}` in red text  
- `score_delta == 0.0`: show nothing additional
- `score_delta is None`: show nothing additional

**R4.6** In `.github/workflows/analyse.yml`, add `cache/last_breakout_scores.json`
to the "Persist actions cache" step alongside `cache/last_actions.json`, using the
same `git add -f` pattern. Use the same `[skip ci]` commit message guard.

**R4.7** `cache/last_breakout_scores.json` must be added to `.gitignore` if it is
not already covered by the `cache/` rule. Verify this — do not add a redundant
rule.

---

## Improvement 5 — Sector Confirmation on Breakout Candidates

### Problem

The system already computes Mansfield RS for SPDR sector ETFs in
`src/data/sector_flows.py`. This data is not connected to individual breakout
candidates. A stock breaking out within a leading sector is materially more
meaningful than an isolated chart in a lagging sector.

### Requirements

**R5.1** In `src/dashboard/renderer.py`, when building the breakout candidate
list for the dashboard data dict, enrich each candidate with a `sector_rs_signal`
field. This field has three possible string values: `"leading"`, `"neutral"`, or
`"lagging"`.

**R5.2** Derive `sector_rs_signal` by:
1. Looking up the candidate's sector name. Use the `ticker_to_sector` mapping in
   `config/sectors.json` first; fall back to the sector already present in the
   candidate dict if available; fall back to `"Unknown"` if neither is available.
2. Mapping the sector name to its SPDR ETF ticker using the existing
   `SECTOR_TO_ETF` dict in `src/data/sector_flows.py`. Import this dict in
   `renderer.py` — do not duplicate it.
3. Looking up the ETF's Mansfield RS signal from the `sector_flows` dict already
   passed into `render_dashboard()`. The existing rotation signal keys
   (`early_rotation`, `momentum_building`, `rotation_peaking`) map to `"leading"`;
   absence of a signal or a neutral reading maps to `"neutral"`; a negative RS
   value maps to `"lagging"`.
4. If the sector cannot be mapped (ETF not found, or sector is `"Unknown"` or
   `"ETF"`), set `sector_rs_signal` to `"neutral"` — do not error.

**R5.3** `sector_rs_signal` must be computed in `renderer.py` only — do not
modify `breakout_screener.py` for this feature.

**R5.4** In `template.html`, add a `Sector RS` column to the Breakout Watch List
table. Display as a compact indicator:
- `"leading"` → green upward arrow or `▲ Leading`
- `"neutral"` → grey dash or `— Neutral`  
- `"lagging"` → red downward arrow or `▼ Lagging`

**R5.5** The new column must be hidden on mobile (consistent with existing
rightmost-column hiding behaviour on mobile viewports).

---

## Improvement 6 — Richer Per-Candidate AI Reasoning

### Problem

The existing batch Claude call in `breakout_screener.py` produces per-candidate
reasoning that describes what signals fired. For a human investor, more useful
output explains *why the setup is specifically attractive* and *what would
invalidate it*.

### Requirements

**R6.1** Modify the batch reasoning prompt in `breakout_screener.py` (the
`claude-sonnet-4-6` batch call) to request the following structure per candidate,
explicitly, in the system or user prompt:

For each candidate, produce three components:
1. **Setup strength** (1–2 sentences): What specifically makes this candidate
   attractive — focus on the interplay between RS, base quality, and volume
   behaviour. Avoid restating signal names mechanically.
2. **Key risk** (1 sentence): The single most important factor that would
   invalidate this setup.
3. **Maturity** (one word only): `Early`, `Developing`, or `Extended` — describing
   how far through the base-building process the candidate appears to be.

**R6.2** The token budget per candidate must be increased to accommodate the
richer output. Change the `max_tokens` calculation from `220 * len(candidates)`
to `280 * len(candidates)`, keeping the `min(..., 8192)` cap.

**R6.3** The response parser must be updated to extract `setup_strength`,
`key_risk`, and `maturity` fields from each candidate's AI response and store
them on the candidate dict. If parsing fails for a candidate, populate all three
fields with `None` — do not raise.

**R6.4** In `template.html`, in the expanded row detail of the Breakout Watch
List (the section that currently shows AI reasoning text), replace or supplement
the existing reasoning block with the structured output:
- Display `setup_strength` as the primary reasoning paragraph
- Display `key_risk` preceded by a `⚠ Risk:` label in amber
- Display `maturity` as a small badge: `Early` (blue), `Developing` (amber),
  `Extended` (grey)

**R6.5** If `setup_strength` is `None` (parse failure), fall back to displaying
the raw reasoning text as before. The dashboard must never show an empty reasoning
section.

---

## Cross-Cutting Constraints

**CC1 — Read-only Trading 212**: The system must never place, modify, or cancel
any orders. `trading212.py` calls only GET endpoints. Do not touch this.

**CC2 — Module invocation**: Always run as `python -m src.main`. Do not change
the entry point.

**CC3 — Breakout AI batch call model**: The batch reasoning call in
`breakout_screener.py` uses `claude-sonnet-4-6` hardcoded. Do not change this to
Opus.

**CC4 — Cache deletion**: After implementing these changes, the existing
`cache/breakout_screener.json` will be stale. The schema versioning in
Improvement 1 will handle this automatically on the next run — no manual
deletion step is needed in CI.

**CC5 — Template self-containment**: `src/dashboard/template.html` is a fully
self-contained single file with no external CSS or build step. All new UI changes
must follow this constraint — no new CDN imports unless absolutely necessary, and
no new files.

**CC6 — Mobile hiding**: Any new table columns added to the Breakout Watch List
must follow the existing pattern of hiding rightmost columns on mobile viewports
(`isMobile` flag, `display: none`).

**CC7 — Dark mode**: All new UI elements must use the existing CSS variable
palette from `template.html`. Do not introduce hardcoded hex colours.

**CC8 — No changes to the growth screener output**: `src/data/screener.py` schema
versioning (Improvement 1) is the only change to that file. The screener result
dict, Claude prompts, and dashboard presentation for the Momentum Opportunities
table are out of scope.

---

## Acceptance Criteria

The implementation is complete when:

- [ ] A fresh run with a stale cache auto-discards and re-runs without manual
      intervention
- [ ] Earnings-risk candidates appear in the breakout table with an amber badge
      rather than being absent
- [ ] In bear regime, candidates scoring ≥ 7.0 with RS + stage signals appear
      with a `Leadership Watch` badge rather than being absent
- [ ] The score column shows a green ↑ or red ↓ delta for returning candidates
      on the second and subsequent runs
- [ ] Each breakout candidate row shows a sector RS indicator column
- [ ] Expanded candidate rows show setup strength, key risk, and maturity badge
      rather than a generic reasoning paragraph
- [ ] `python -m src.main` completes without error on a cold run (empty cache)
- [ ] `python -m src.main` completes without error on a warm run (valid cache)
- [ ] The dashboard renders correctly on both desktop and mobile viewports
- [ ] No changes are made outside the files listed in each requirement
