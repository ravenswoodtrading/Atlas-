import os
from pathlib import Path

import requests
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"

# SerpApi's free tier is 250 searches/month -- num=15 keeps each
# lookup to one search request while still giving enough candidates
# for a human to pick a real match from.
DEFAULT_NUM_RESULTS = 15


def search_uk_shopping(query: str, num: int = DEFAULT_NUM_RESULTS) -> list:
    """
    Searches Google Shopping's UK results (google.co.uk) for `query`
    via SerpApi. Returns a list of {title, source, price,
    extracted_price, thumbnail, product_link} dicts, or [] if the key
    is missing, the request fails, or nothing came back -- never
    raises past this function, same defensive style as
    ProductService.get_products.

    NOT a barcode/EAN lookup -- CONFIRMED via a live test that
    searching by bare EAN as query text returns unrelated products.
    `query` should be a product title/description.
    """
    api_key = os.getenv("SERPAPI_API_KEY")

    if not api_key:
        print("SERPAPI_API_KEY not found -- skipping OA search.")
        return []

    try:
        response = requests.get(
            SERPAPI_BASE_URL,
            params={
                "engine": "google_shopping",
                "q": query,
                "google_domain": "google.co.uk",
                "gl": "uk",
                "api_key": api_key,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"SerpApi search failed for query {query!r}: {exc}")
        return []

    results = data.get("shopping_results") or []

    candidates = []

    for result in results[:num]:
        extracted_price = result.get("extracted_price")

        if not extracted_price:
            continue

        candidates.append({
            "title": result.get("title") or "",
            "source": result.get("source") or "",
            "price": result.get("price") or "",
            "extracted_price": extracted_price,
            "thumbnail": result.get("thumbnail") or "",
            "product_link": result.get("product_link") or "",
        })

    return candidates


def get_account_status() -> dict:
    """
    Remaining monthly quota etc -- CONFIRMED this does not consume a
    search credit (checked twice in a row during testing, count didn't
    move). Returns {} on any failure rather than raising, since this
    is just a courtesy display, not load-bearing.
    """
    api_key = os.getenv("SERPAPI_API_KEY")

    if not api_key:
        return {}

    try:
        response = requests.get(
            "https://serpapi.com/account.json",
            params={"api_key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"SerpApi account status check failed: {exc}")
        return {}
