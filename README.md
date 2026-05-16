# Portfolio Analyst

Automated investment analysis system — personal financial co-pilot.

Pulls live portfolio data, fetches market data and fundamentals, uses the
Claude API as the intelligence layer, and outputs a dark-mode HTML dashboard
deployed to [stevegerrard.org/markets](https://stevegerrard.org/markets).

Runs on a daily schedule (weekday mornings, 7:00am UTC) via GitHub Actions,
or can be triggered manually from the Actions tab.

**Read/analyse only. Never places, modifies, or cancels any orders.**

## Setup

Copy `.env.example` to `.env` and fill in your API keys:

```
T212_API_KEY        — Trading 212 app → Settings → API Beta
T212_API_SECRET     — Same, shown once only
ANTHROPIC_API_KEY   — console.anthropic.com
FINNHUB_API_KEY     — finnhub.io (free tier)
FRED_API_KEY        — fred.stlouisfed.org (free, instant)
```

Add the same keys as GitHub repository secrets for Actions to use them.

## Local run

```bash
pip install -r requirements.txt
python src/main.py
```

Output is written to `output/index.html`.

## Data sources

| Layer | Source |
|---|---|
| Portfolio | Trading 212 API (read-only) |
| Price + technicals | yfinance + pandas-ta |
| Fundamentals + short interest | Finnhub (free tier) |
| Sector rotation | Finviz + SPDR ETFs |
| Macro | FRED API |
| Institutional flows | SEC EDGAR 13F (no key needed) |
| AI analysis | Anthropic Claude API |

## Architecture

```
src/
├── data/           # All data fetching modules
├── analysis/       # Claude API prompt functions
├── dashboard/      # HTML renderer and template
└── main.py         # Orchestrator
```
