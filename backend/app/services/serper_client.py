import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

SERPER_SHOPPING_URL = "https://google.serper.dev/shopping"

# Serper's own published rate at the time this was written -- ~$0.30 per
# 1000 queries at the cheapest prepaid tier, up to ~$1.00 on smaller
# packs. The higher figure is used here deliberately so a run's
# estimated cost errs on the side of over-reporting rather than
# flattering itself. Same "estimate, not ground truth" spirit as
# brave_search_client.COST_PER_1000_USD -- Serper has no billing
# endpoint this module queries, so this is arithmetic on a published
# price, not a real balance.
#
# For comparison: SerpApi's Developer plan works out at $15.00 per 1000
# ($75/month for 5,000 searches), i.e. 15-50x this.
COST_PER_1000_USD = 1.0

# Matches serpapi_client.DEFAULT_NUM_RESULTS so switching provider
# doesn't silently change how many candidates a caller gets to choose
# from.
DEFAULT_NUM_RESULTS = 15

# UNVERIFIED AGAINST A LIVE RESPONSE (2026-08-28). Everything below is
# written from Serper's public documentation, NOT from a real call --
# there was no API key available when this was written. The response
# shape is therefore handled defensively: each field is looked up under
# several plausible key names rather than one assumed name, and an
# unrecognised payload logs loudly instead of silently returning [].
#
# FIRST THING TO DO with a real key: run one query, print the raw JSON,
# and replace these tuples with the actual key names. Serper's free tier
# (2,500 queries, no card) covers this many times over. Leaving the
# fallbacks in afterwards is harmless but the confirmed name should be
# listed first.
RESULTS_KEYS = ("shopping", "shoppingResults", "results")
TITLE_KEYS = ("title", "name")
SOURCE_KEYS = ("source", "seller", "merchant", "store")
LINK_KEYS = ("link", "productLink", "url")
THUMBNAIL_KEYS = ("imageUrl", "thumbnail", "image")
PRICE_KEYS = ("price", "priceString", "extractedPrice")


def _first(result: dict, keys: tuple, default=""):
    """
    First non-empty value among `keys`. Exists because the exact key
    names are unconfirmed (see above) -- once they're known this can
    collapse to plain .get() calls.
    """
    for key in keys:
        value = result.get(key)
        if value not in (None, ""):
            return value
    return default


def extract_price(raw) -> float | None:
    """
    Parses Serper's price into a float.

    This is the one REAL behavioural difference from serpapi_client:
    SerpApi returns a numeric `extracted_price` alongside the display
    string, Serper (per its docs) returns only a display string like
    "£24.99". Every caller downstream -- OaLookupService.search_candidates,
    OaSourceDiscoveryService.run_batch -- relies on extracted_price being
    a usable number, so the parsing has to happen here rather than
    leaking a provider difference into the OA engine.

    Returns None for anything that can't be read as a single price, which
    the caller treats exactly the way serpapi_client already treats a
    missing extracted_price: skip the result. That deliberately includes
    price RANGES ("£20.00 - £30.00") -- a range is not a price you can
    compute an ROI against, and quietly taking the low end would invent a
    profitable-looking lead that isn't buyable at that figure.
    """
    if isinstance(raw, (int, float)):
        return float(raw) or None

    if not isinstance(raw, str):
        return None

    # A range, or two prices jammed together -- refuse rather than guess.
    if raw.count("-") >= 1 and len(re.findall(r"\d+[.,]?\d*", raw)) > 1:
        return None

    # Strip currency symbols/codes and thousands separators, keep the
    # decimal point.
    cleaned = raw.replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)

    if not match:
        return None

    try:
        value = float(match.group())
    except ValueError:
        return None

    return value or None


def search_uk_shopping(query: str, num: int = DEFAULT_NUM_RESULTS) -> list:
    """
    Google Shopping UK results for `query` via Serper. Drop-in
    replacement for serpapi_client.search_uk_shopping -- SAME signature,
    SAME return shape:

        [{title, source, price, extracted_price, thumbnail, product_link}]

    Returns [] if the key is missing, the request fails, or nothing came
    back -- never raises past this function, same defensive style as
    serpapi_client and brave_search_client.

    NOT a barcode/EAN lookup, for the same reason recorded in
    serpapi_client's own docstring: a shopping engine matches against a
    structured catalog it already holds, so a bare EAN as free text
    returns unrelated products. `query` should be a product
    title/description. (Brave's plain WEB search is where exact-phrase
    EAN queries genuinely work -- see brave_search_client.)
    """
    api_key = os.getenv("SERPER_API_KEY")

    if not api_key:
        print("SERPER_API_KEY not found -- skipping OA shopping search.")
        return []

    try:
        response = requests.post(
            SERPER_SHOPPING_URL,
            json={"q": query, "gl": "gb", "hl": "en", "num": num},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"Serper shopping search failed for query {query!r}: {exc}")
        return []

    results = None
    for key in RESULTS_KEYS:
        if isinstance(data.get(key), list):
            results = data[key]
            break

    if results is None:
        # Loud rather than silent: an empty list here is
        # indistinguishable from "no results", and a quietly-wrong key
        # name would look like Serper simply finding nothing for every
        # query -- exactly the kind of failure that would wrongly
        # discredit the provider during the A/B comparison.
        print(
            f"Serper response had no recognised results array for {query!r} "
            f"-- top-level keys were {sorted(data.keys())}. "
            f"Update RESULTS_KEYS in serper_client."
        )
        return []

    candidates = []

    for result in results[:num]:
        if not isinstance(result, dict):
            continue

        raw_price = _first(result, PRICE_KEYS, default=None)
        extracted_price = extract_price(raw_price)

        # Same rule serpapi_client already applies -- a result with no
        # usable price can't be priced through FeeEngine, so it isn't a
        # candidate.
        if not extracted_price:
            continue

        candidates.append({
            "title": _first(result, TITLE_KEYS),
            "source": _first(result, SOURCE_KEYS),
            "price": raw_price if isinstance(raw_price, str) else str(raw_price),
            "extracted_price": extracted_price,
            "thumbnail": _first(result, THUMBNAIL_KEYS),
            "product_link": _first(result, LINK_KEYS),
        })

    return candidates


def get_account_status() -> dict:
    """
    Present only so this module is interface-compatible with
    serpapi_client (see shopping_search).

    Serper is PREPAID CREDITS, not a fixed monthly quota, so there is no
    equivalent of SerpApi's "searches left this month" to report and no
    cliff to reserve against. Returning {} makes
    OaSourceDiscoveryService.run_batch read remaining quota as None,
    which its existing code already handles as "unknown -- don't enforce
    the buffer". That's the correct behaviour here, not a degraded one:
    the quota buffer exists to protect a monthly allowance that Serper
    doesn't have.
    """
    return {}
