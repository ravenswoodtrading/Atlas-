"""
STANDALONE PROTOTYPE / BENCHMARK ONLY -- 2026-09-05.

Tests whether Claude Sonnet 5 + Anthropic's web_search/web_fetch server
tools can materially outperform Atlas's current SerpAPI-based OA
(online arbitrage) source matching -- see OaSourceDiscoveryService /
SourcingClassifier.classify_shopping_match in the real app for what
this is being compared against.

THIS SCRIPT DOES NOT TOUCH ATLAS. It does not import any app.* module,
does not open atlas.db, makes no Keepa/SP-API calls, runs no scheduler
or scan, and writes nothing to any database. The 10 benchmark cases
below were read out of the real Atlas database by hand (read-only,
separately from this script) and are hardcoded here as plain data --
this script's only network calls are to the Anthropic API.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... python oa_research_agent_prototype.py

Cost controls (per Tamara's own explicit instruction):
- Sonnet 5 only, never Opus/Fable.
- Max 5 web searches per product (tool's own max_uses).
- Web fetch capped too (max_uses) -- used only for promising
  candidates, per the system prompt's own instruction (the model
  decides when a candidate is "promising", not this harness).
- Exactly the 10 products below. Does not continue further on its own
  -- rerun manually with a bigger list only if the 10-product results
  justify it.

Output: a single JSON report file (oa_research_agent_benchmark_report.json)
plus a printed human-readable summary table. No other file, no DB
write, nothing sent back into Atlas.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-5"
MAX_SEARCHES_PER_PRODUCT = 5
MAX_FETCHES_PER_PRODUCT = 6
MAX_TOKENS = 8192
MAX_PAUSE_TURN_CONTINUATIONS = 4  # safety cap, not expected to be hit at this search budget

WEB_SEARCH_COST_PER_SEARCH_USD = 0.01
# Sonnet 5 pricing confirmed current 2026-09-05: $2/$10 per MTok (2026-08 permanent pricing).
SONNET5_INPUT_PER_MTOK_USD = 2.00
SONNET5_OUTPUT_PER_MTOK_USD = 10.00

# =============================================================================
# The 10 benchmark cases -- all real ASINs read directly out of Atlas's
# own oa_source_candidates table (read-only query, separate from this
# script). Includes the two previously-documented failure cases plus
# the 8 recent real SerpAPI mismatches Tamara named. serpapi_* fields
# are exactly what Atlas's CURRENT pipeline already found/decided for
# each -- the baseline this benchmark is measured against.
# =============================================================================
BENCHMARK_CASES = [
    {
        "asin": "B08LNXXW38", "title": "RYOBI 18V ONE+ 150mm Circular Saw (Battery & Charger Excluded)",
        "brand": "RYOBI", "ean": "4892210141071", "mpn": "R18CSP-0",
        "amazon_price_gbp": 77.00, "target_price_gbp": 44.84,
        "serpapi_retailer": "Ryobi Tools", "serpapi_price": 69.99, "serpapi_match_tier": "brand_title",
        "known_issue": "SerpAPI's retailer_title reads 'R18CS115-0' -- a DIFFERENT model number to the Amazon MPN 'R18CSP-0'.",
    },
    {
        "asin": "B08P3TCMT2", "title": "Blackmagic OB02429 Micro Converter BiDirect SDI/HDMI 3G PSU",
        "brand": "Blackmagic Design", "ean": "9338716006995", "mpn": "BM-CONVBDC/SDI/HDMI03G/PS",
        "amazon_price_gbp": 73.99, "target_price_gbp": 43.40,
        "serpapi_retailer": "MPB", "serpapi_price": 19.00, "serpapi_match_tier": "brand_title",
        "known_issue": "MPB is a used/secondhand camera-gear marketplace; retailer_title is generic ('HDMI to SDI Micro Converter') with no model confirmation, and £19 vs Amazon's £73.99 is suspiciously cheap for a genuine match.",
    },
    {
        "asin": "B0CBLKS51N", "title": "Skullcandy Hesh ANC Noise Cancelling Wireless Headphones True Black",
        "brand": "Skullcandy", "ean": "0810045689524", "mpn": "S6HHW-B957",
        "amazon_price_gbp": 65.57, "target_price_gbp": 37.99,
        "serpapi_retailer": "skullcandy.co.uk", "serpapi_price": 39.99, "serpapi_match_tier": "brand_title",
        "known_issue": "Retailer title is 'Hesh 360' -- a DIFFERENT Skullcandy headphone line to the Amazon listing's 'Hesh ANC'.",
    },
    {
        "asin": "B0094B2G9E", "title": "White Barn Candle Bath & Body Works White Barn 3-Wick Candle Mahogany Teakwood",
        "brand": "Bath & Body Works", "ean": "0667531111461", "mpn": "SYNCHKG045699",
        "amazon_price_gbp": 39.95, "target_price_gbp": 21.92,
        "serpapi_retailer": "London Loves Beauty", "serpapi_price": 27.95, "serpapi_match_tier": "brand_title",
        "known_issue": "Retailer title is 'Cuddle Weather' -- a DIFFERENT candle scent/variant to the Amazon listing's 'Mahogany Teakwood'.",
    },
    {
        "asin": "B07DN5KWHX", "title": "Guinot Night Logic Night Cream 50 ml",
        "brand": "Guinot", "ean": "3500465074600", "mpn": "402026",
        "amazon_price_gbp": 50.99, "target_price_gbp": 29.58,
        "serpapi_retailer": "Notino.co.uk", "serpapi_price": 59.24, "serpapi_match_tier": "brand_title",
        "known_issue": "Retailer title is 'Newhite Brightening Night Cream' -- a DIFFERENT Guinot product line to 'Night Logic'.",
    },
    {
        "asin": "B00864DTJG", "title": "Guinot Longue Vie Cou Neck Cream 30ml (Salon Size)",
        "brand": "Guinot", "ean": "0799457823422", "mpn": "3500464425748",
        "amazon_price_gbp": 40.90, "target_price_gbp": 24.28,
        "serpapi_retailer": "Rackhams", "serpapi_price": 96.06, "serpapi_match_tier": "brand_title",
        "known_issue": "Retailer title is 'Longue Vie Youth Renewing Serum' -- a DIFFERENT Guinot product (serum, not neck cream) to the Amazon listing.",
    },
    {
        "asin": "B0D3WQQH89", "title": "Ultimate Ears BOOM 4 Portable Waterproof Bluetooth Speaker With 360-Degree",
        "brand": "Ultimate Ears", "ean": "5099206125315", "mpn": "984-002011",
        "amazon_price_gbp": 82.99, "target_price_gbp": 48.99,
        "serpapi_retailer": "LambdaTek", "serpapi_price": 94.44, "serpapi_match_tier": "brand_title",
        "known_issue": "Retailer title is 'WONDERBOOM 4' -- a DIFFERENT, smaller/cheaper UE speaker line to 'BOOM 4'.",
    },
    {
        "asin": "B0C7YKDLGD", "title": "Huel Black Edition High Protein Complete Meal Replacement, 17 Meals, Coffee Caramel Flavour",
        "brand": "Huel", "ean": "5060495115318", "mpn": "",
        "amazon_price_gbp": 35.98, "target_price_gbp": 19.66,
        "serpapi_retailer": "ean-search.org", "serpapi_price": None, "serpapi_match_tier": "ean",
        "known_issue": "ean-search.org is an EAN LOOKUP DATABASE, not a retailer -- no purchasable offer, no real price, yet it was recorded as a 'candidate_found'.",
    },
    {
        "asin": "B0DV6CDXKT", "title": "UbiQuiti USW-FLEX-2.5G-8-POE",
        "brand": "Ubiquiti", "ean": "0810084695968", "mpn": "USW-Flex-2.5G-8-PoE",
        "amazon_price_gbp": 202.36, "target_price_gbp": None,
        "serpapi_retailer": "Optdex", "serpapi_price": 114.00, "serpapi_match_tier": "brand_title",
        "known_issue": "Previously documented real incident: auto-matched to a cheaper SIBLING model in the same switch family, not the exact 8-port 2.5G unit.",
    },
    {
        "asin": "B0BTZB7F88", "title": "AMD Ryzen 7 7800X3D Processor with 3D V-Cache Technology (integrated Radeon Graphics, 8 cores/16 threads, AM5 Socket)",
        "brand": "AMD", "ean": "0730143314930", "mpn": "AMD Ryzen 7 7800X3D",
        "amazon_price_gbp": 265.00, "target_price_gbp": None,
        "serpapi_retailer": "eBay", "serpapi_price": 12.94, "serpapi_match_tier": "brand_mpn",
        "known_issue": "Previously documented real incident: matched an eBay listing for an EMPTY BOX, no CPU included -- brand/MPN text present on the listing despite selling no actual processor.",
    },
]


SYSTEM_PROMPT = f"""You are Atlas's OA (online arbitrage) sourcing research agent. Your ONLY job: given one Amazon product, find the cheapest REAL, VERIFIED retail source selling the exact same product for less than Amazon, or honestly report that none was found. A false positive (claiming a source is verified when it isn't the same product) is worse than finding nothing.

METHODOLOGY -- follow these steps in order:
1. Establish the exact product identity from the supplied Amazon data (title, brand, EAN/GTIN, MPN/model, pack size/variant/colour if inferable from the title).
2. Generate multiple search strategies as needed: exact MPN, exact EAN/GTIN, brand + MPN, brand + model, product title, title + model. You do not need to run all of them -- use judgement, but do not stop after one search if the first result set is weak or ambiguous.
3. Use the web_search tool (you have at most {MAX_SEARCHES_PER_PRODUCT} searches -- spend them deliberately, not on near-duplicate queries).
4. Treat every search result as a CANDIDATE only, never a verified source.
5. Use web_fetch to open the actual product page for any candidate that looks promising enough to be worth verifying (you have at most {MAX_FETCHES_PER_PRODUCT} fetches -- do not fetch every candidate, only ones worth checking).
6. On the fetched page, verify the candidate against: EAN/GTIN, MPN/model, brand, product title, variant, colour, size, pack quantity, condition (new vs used/refurbished).
7. Reject obvious mismatches -- see HARD REJECTION RULES below.

HARD REJECTION RULES -- these are absolute. A fuzzy title similarity NEVER overrides a hard identifier conflict:
- EAN/GTIN conflict -> reject.
- MPN/model conflict -> reject (e.g. the page's own model number differs from the Amazon MPN, even if brand and general title look similar -- this is the single most common real failure mode you are being tested against).
- Wrong pack quantity -> reject.
- Wrong size/colour/variant -> reject.
- Compatible/accessory-only product (e.g. a case, cable, or "works with X" item that is not X itself) -> reject.
- Empty box / parts-only / used / refurbished when the Amazon listing is for a new unit -> reject.
- A catalogue/specification/EAN-lookup site with no actual purchasable offer -> this is NOT a retailer source, classify it as CATALOGUE_DATABASE and do not treat it as a candidate retailer.

RETAILER CLASSIFICATION -- classify every candidate as exactly one of:
- RETAILER (a real shop selling this item directly, with a purchasable offer)
- MARKETPLACE (e.g. eBay, a marketplace where a THIRD PARTY seller lists it, not the retailer itself -- may still be returned but must never be treated as equivalent to a normal retailer)
- CATALOGUE_DATABASE (a spec sheet, EAN lookup, or database page with no purchasable offer)
- COMPARISON_SEARCH (a price comparison or search-aggregator page, not a direct seller)
- OTHER
Only RETAILER candidates should normally become a "verified source". A MARKETPLACE candidate may be listed separately in verified_sources with classification "MARKETPLACE" if you are confident of the match, but must be clearly labelled as such, never presented as an ordinary retailer.

PRICE VERIFICATION -- for each candidate you actually fetch and verify, record: current item price, currency, VAT status if identifiable, delivery cost if identifiable, effective cost (price + delivery), stock/availability, the product URL, and that you checked it today. Do NOT assume a search-result snippet's price is still correct if you were able to fetch the actual page -- always prefer the fetched page's own price when you have it.

OUTPUT -- after you finish researching, respond with ONLY a single fenced ```json code block (no other text before or after it) containing exactly this structure:
{{
  "asin": "<the ASIN you were given>",
  "product_identity": {{"title": "...", "brand": "...", "ean": "...", "mpn": "...", "key_identifying_features": ["..."]}},
  "search_strategies_used": ["..."],
  "candidates": [
    {{"retailer": "...", "url": "...", "title_seen": "...", "price": <number or null>, "currency": "GBP", "classification": "RETAILER|MARKETPLACE|CATALOGUE_DATABASE|COMPARISON_SEARCH|OTHER", "fetched": true|false}}
  ],
  "rejected_candidates": [
    {{"retailer": "...", "url": "...", "reason": "EAN_CONFLICT|MPN_CONFLICT|WRONG_VARIANT|WRONG_PACK|WRONG_SIZE_COLOUR|ACCESSORY_ONLY|USED_REFURBISHED|NOT_A_RETAILER|OTHER", "detail": "..."}}
  ],
  "verified_sources": [
    {{"retailer": "...", "url": "...", "classification": "RETAILER|MARKETPLACE", "price": <number>, "currency": "GBP", "vat_status": "...", "delivery_cost": <number or null>, "effective_cost": <number>, "stock_status": "...", "date_checked": "{datetime.now(timezone.utc).date().isoformat()}", "evidence": "why you believe this is the exact same product -- cite the matching identifier(s)"}}
  ],
  "best_verified_source": <one of the verified_sources objects, or null if none>,
  "confidence": "high|medium|low|none",
  "research_summary": "2-4 sentences on what you found and why you did or didn't verify a source"
}}

If you cannot verify ANY real source with confidence, set verified_sources to an empty list, best_verified_source to null, and confidence to "none" -- do NOT force a low-confidence guess into best_verified_source. Returning "no verified source found" is a correct, valuable answer, not a failure."""


def build_user_message(case: dict) -> str:
    return (
        f"ASIN: {case['asin']}\n"
        f"Amazon title: {case['title']}\n"
        f"Brand: {case['brand']}\n"
        f"EAN/GTIN: {case['ean'] or 'unknown'}\n"
        f"MPN/model: {case['mpn'] or 'unknown'}\n"
        f"Amazon current price (GBP): {case['amazon_price_gbp']}\n"
        f"Target buy price (GBP, for reference only -- not a constraint on what you verify): "
        f"{case['target_price_gbp'] if case['target_price_gbp'] is not None else 'not set'}\n\n"
        f"Find the cheapest verified retail source for this exact product, or report none found."
    )


def extract_json_block(text: str) -> dict | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def run_one_case(client: anthropic.Anthropic, case: dict) -> dict:
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES_PER_PRODUCT},
        {
            "type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_FETCHES_PER_PRODUCT,
            "citations": {"enabled": False},
            # Real cost bug caught on the smoke test (2026-09-05): with
            # no cap, a single product run hit 535K input tokens / $1.17
            # -- Anthropic's own docs note there is NO default limit
            # here, so a handful of large retailer pages compound fast,
            # especially once resent on every pause_turn continuation.
            # 3000 tokens is well above the ~2500 Anthropic cites for a
            # typical 10kB page -- plenty to read price/EAN/MPN/stock
            # without needing the whole page (nav, footers, reviews).
            "max_content_tokens": 3000,
        },
    ]
    messages = [{"role": "user", "content": build_user_message(case)}]

    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_searches = 0
    total_fetches = 0
    final_text = ""
    error = None

    for _ in range(MAX_PAUSE_TURN_CONTINUATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
        )

        usage = response.usage
        total_input_tokens += usage.input_tokens
        total_output_tokens += usage.output_tokens
        total_cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use:
            total_searches += getattr(server_tool_use, "web_search_requests", 0) or 0
            total_fetches += getattr(server_tool_use, "web_fetch_requests", 0) or 0

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason == "refusal":
            error = "Model refused this request."
            break

        final_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        break
    else:
        error = f"Hit MAX_PAUSE_TURN_CONTINUATIONS ({MAX_PAUSE_TURN_CONTINUATIONS}) without finishing."

    parsed = extract_json_block(final_text) if final_text else None
    if parsed is None and error is None:
        error = "Could not parse a JSON block from the model's final response."

    cost_usd = (
        (total_input_tokens / 1_000_000) * SONNET5_INPUT_PER_MTOK_USD
        + (total_output_tokens / 1_000_000) * SONNET5_OUTPUT_PER_MTOK_USD
        + total_searches * WEB_SEARCH_COST_PER_SEARCH_USD
    )

    return {
        "asin": case["asin"],
        "parsed": parsed,
        "raw_final_text": final_text,
        "error": error,
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cache_read_input_tokens": total_cache_read_tokens,
            "web_searches": total_searches,
            "web_fetches": total_fetches,
        },
        "cost_usd": round(cost_usd, 4),
    }


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    results = []
    print(f"Running OA Research Agent prototype against {len(BENCHMARK_CASES)} real benchmark cases...\n")

    for i, case in enumerate(BENCHMARK_CASES, 1):
        print(f"[{i}/{len(BENCHMARK_CASES)}] {case['asin']} -- {case['title'][:60]}...")
        result = run_one_case(client, case)
        result["case"] = case
        results.append(result)

        if result["error"]:
            print(f"    ERROR: {result['error']}")
        elif result["parsed"]:
            best = result["parsed"].get("best_verified_source")
            confidence = result["parsed"].get("confidence")
            if best:
                print(f"    -> VERIFIED: {best.get('retailer')} @ {best.get('currency', 'GBP')}{best.get('effective_cost')} (confidence: {confidence})")
            else:
                print(f"    -> NO VERIFIED SOURCE FOUND (confidence: {confidence})")
        print(f"    usage: {result['usage']} | cost: ${result['cost_usd']}")
        print()

    total_cost = sum(r["cost_usd"] for r in results)
    total_searches = sum(r["usage"]["web_searches"] for r in results)
    total_fetches = sum(r["usage"]["web_fetches"] for r in results)
    total_input = sum(r["usage"]["input_tokens"] for r in results)
    total_output = sum(r["usage"]["output_tokens"] for r in results)
    n = len(results)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "n_products": n,
        "results": results,
        "aggregate": {
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_asin_usd": round(total_cost / n, 4) if n else 0,
            "estimated_cost_per_100_asins_usd": round((total_cost / n) * 100, 2) if n else 0,
            "estimated_cost_per_1000_asins_usd": round((total_cost / n) * 1000, 2) if n else 0,
            "avg_searches_per_asin": round(total_searches / n, 2) if n else 0,
            "avg_fetches_per_asin": round(total_fetches / n, 2) if n else 0,
            "avg_input_tokens_per_asin": round(total_input / n, 0) if n else 0,
            "avg_output_tokens_per_asin": round(total_output / n, 0) if n else 0,
        },
    }

    out_path = os.path.join(os.path.dirname(__file__), "oa_research_agent_benchmark_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("=" * 100)
    print(f"DONE. Full report written to: {out_path}")
    print(f"Total cost for {n} products: ${total_cost:.4f} "
          f"(avg ${total_cost/n:.4f}/ASIN, est. ${(total_cost/n)*100:.2f}/100 ASINs, ${(total_cost/n)*1000:.2f}/1000 ASINs)")
    print(f"Avg searches/ASIN: {total_searches/n:.2f} | Avg fetches/ASIN: {total_fetches/n:.2f}")
    print("=" * 100)


if __name__ == "__main__":
    main()
