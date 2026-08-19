import os
from pathlib import Path

import requests
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Brave's own stated pricing for the tier this was chosen for -- ~$5
# per 1000 queries, with free monthly credits (see the user's own OA
# Source Discovery spec). Used only to ESTIMATE a run's cost
# (OaSourceRun.estimated_cost_usd) from the exact number of calls this
# module actually made -- not fetched from any Brave billing API (no
# such endpoint checked yet), so treat this as approximate, same
# "estimate, not ground truth" spirit as everywhere else a cost is
# shown in Atlas.
COST_PER_1000_USD = 5.0

DEFAULT_COUNT = 10


def search(query: str, count: int = DEFAULT_COUNT) -> list:
    """
    General web search via Brave's Search API. Returns a list of
    {title, url, description} dicts, or [] if the key is missing, the
    request fails, or nothing came back -- never raises past this
    function, same defensive style as
    serpapi_client.search_uk_shopping.

    Deliberately a PLAIN WEB search, not Brave's shopping/product
    endpoint -- an exact-phrase EAN/MPN query here searches real page
    text across the open web, which is a genuinely different search
    mode from the EAN-as-query failure already confirmed against
    SerpApi's Google SHOPPING results (see that module's own
    docstring): a shopping engine matches against a structured
    catalog it already has, a web search matches against whatever
    text actually appears on a page, including a spec sheet or
    product listing that happens to print the EAN/MPN verbatim. Not
    assumed to work equally well -- this is exactly the open question
    the OA Source Discovery MVP test batch is meant to answer.

    Wrap the query in double quotes yourself (e.g. '"1234567890123"')
    when you want Brave to treat it as an exact phrase -- this
    function passes `query` through untouched.
    """
    api_key = os.getenv("BRAVE_SEARCH_API_KEY")

    if not api_key:
        print("BRAVE_SEARCH_API_KEY not found -- skipping OA source search.")
        return []

    try:
        response = requests.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "country": "GB", "count": count},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"Brave search failed for query {query!r}: {exc}")
        return []

    results = (data.get("web") or {}).get("results") or []

    return [
        {
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "description": r.get("description") or "",
        }
        for r in results[:count]
    ]
