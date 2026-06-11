"""
Fetch current macro news headlines using Anthropic's web search tool.

Runs four sequential searches and returns a list of headline/summary dicts.
Non-fatal: individual search failures are skipped; the function never raises.
"""

import json
import logging
import re
import time

import anthropic

log = logging.getLogger(__name__)

_QUERIES = [
    "Federal Reserve interest rate decision outlook 2026",
    "US nonfarm payrolls jobs report latest",
    "US CPI inflation data latest 2026",
    "US GDP economic growth outlook 2026",
]

_SYSTEM = (
    "You are a financial news summariser. For the given search query, "
    "return ONLY a JSON object with two keys: "
    '"headline" (string, max 15 words, the most relevant finding) and '
    '"summary" (string, max 40 words, a concise description of the finding). '
    "No markdown, no explanation — just the raw JSON object."
)


def fetch_macro_news() -> list[dict]:
    """
    Run four web searches and return [{query, headline, summary}].
    Never raises — skips failed or unparseable individual searches.
    """
    from src.analysis.claude_analyst import MODEL_PROSE

    client = anthropic.Anthropic()
    results: list[dict] = []

    for i, query in enumerate(_QUERIES):
        if i > 0:
            time.sleep(1)
        try:
            msg = client.messages.create(
                model=MODEL_PROSE,
                max_tokens=300,
                system=_SYSTEM,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": query}],
            )
            text = ""
            for block in msg.content:
                if hasattr(block, "text"):
                    text += block.text

            json_match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
            if not json_match:
                log.warning("macro_news: no JSON found for query %r", query)
                continue

            parsed = json.loads(json_match.group())
            headline = str(parsed.get("headline", "")).strip()
            summary = str(parsed.get("summary", "")).strip()
            if headline and summary:
                results.append({"query": query, "headline": headline, "summary": summary})
            else:
                log.warning("macro_news: missing headline/summary for query %r", query)

        except Exception as exc:
            log.warning("macro_news: search failed for query %r: %s", query, exc)

    log.info("macro_news: collected %d/%d results", len(results), len(_QUERIES))
    return results
