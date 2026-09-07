"""
STANDALONE PROTOTYPE / COST-REDUCTION BENCHMARK -- 2026-09-05.

Follow-up to oa_research_agent_prototype.py (the original 10-product
benchmark: 10/10 known SerpAPI false positives avoided, $0.61/ASIN
average, ~253K input tokens/ASIN). That benchmark proved the JUDGMENT
approach works; this one tests whether the same judgment can run for
much less by changing HOW evidence reaches Claude, not the verification
rules themselves.

CRITICAL CONSTRAINT (Tamara's own explicit instruction): the hard
rejection rules / retailer classification / price verification / output
schema / confidence logic must be BYTE-IDENTICAL to the original
benchmark -- only the evidence-delivery pipeline changes. This script
enforces that by IMPORTING the original SYSTEM_PROMPT and slicing out
the shared rules block programmatically (see SHARED_RULES_BLOCK below)
rather than retyping it, so there is no possibility of silent drift
between the two benchmarks' judgment logic.

Real technical finding that reshaped Test A (reported and approved
before this was written): Anthropic's web_fetch tool refuses to open
google.com/google.co.uk (error_code: url_not_allowed) -- confirmed
live. Since SerpAPI's product_link is ALWAYS a Google Shopping redirect,
never the retailer's own URL, "fetch SerpAPI's candidate URL directly"
is not possible. Test A is therefore two stages:

  Test A1 -- zero-cost text triage: Claude sees ONLY the SerpAPI
  evidence (retailer name, title-seen, price) as plain text, NO tools
  at all. Can it reject known-bad matches just by reading the same
  snippet SerpAPI's own string-matching already had?

  Test A2 -- selective verification: ONLY for candidates that survive
  A1 (no hard conflict visible from text alone). Claude gets ONE
  web_search (to locate the retailer's real page, since we can't fetch
  the Google redirect) + web_fetch to verify properly.

Test B extends this with a Brave Search fallback (Atlas's own existing,
already-paid Brave integration, imported read-only -- see
app/services/brave_search_client.py) when the SerpAPI candidate is
rejected or too thin to judge at all -- Tamara's stated preferred
production architecture if the numbers work.

THIS SCRIPT DOES NOT TOUCH ATLAS'S DATABASE. It imports
brave_search_client.search (a pure HTTP GET wrapper, no DB access) and
the read-only BENCHMARK_CASES data from the original prototype file.
No Keepa/SP-API calls, no scheduler, no scan, no Atlas DB writes.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... python oa_research_agent_cost_benchmark.py
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from app.services import brave_search_client  # noqa: E402 -- Atlas's own existing, read-only search client

sys.path.insert(0, os.path.dirname(__file__))
from oa_research_agent_prototype import (  # noqa: E402
    SYSTEM_PROMPT as ORIGINAL_SYSTEM_PROMPT,
    BENCHMARK_CASES as _ORIGINAL_CASES,
    MODEL, SONNET5_INPUT_PER_MTOK_USD, SONNET5_OUTPUT_PER_MTOK_USD,
    WEB_SEARCH_COST_PER_SEARCH_USD, extract_json_block,
)

BRAVE_COST_PER_1000_USD = brave_search_client.COST_PER_1000_USD  # Atlas's own stated ~$5/1000, same estimate convention

# =============================================================================
# Shared verification rules -- SLICED OUT of the original, already-
# reported-on SYSTEM_PROMPT rather than retyped, so this is provably
# byte-identical: hard rejection rules, retailer classification, price
# verification, output schema, confidence logic. The only thing that
# ever differs between benchmarks is the METHODOLOGY preamble each
# stage prepends to this same block.
# =============================================================================
_SPLIT_MARKER = "\nHARD REJECTION RULES -- these are absolute."
_split_at = ORIGINAL_SYSTEM_PROMPT.index(_SPLIT_MARKER) + 1  # +1 to drop the leading newline
SHARED_RULES_BLOCK = ORIGINAL_SYSTEM_PROMPT[_split_at:]

# retailer_title added (2026-09-05) -- the exact SerpAPI snippet text,
# needed for the text-only triage stage. Pulled by hand from the real
# oa_source_candidates rows (read-only query, separate from this
# script) for the same 10 ASINs the original benchmark used --
# unchanged products, per Tamara's own instruction.
_RETAILER_TITLES = {
    "B08LNXXW38": "Ryobi ONE+ 115mm Circular Saw 18V R18CS115-0",
    "B08P3TCMT2": "Blackmagic Design HDMI to SDI Micro Converter",
    "B0CBLKS51N": "Skullcandy Hesh 360 Over-Ear Wireless Headphones",
    "B0094B2G9E": "Bath & Body Works White Barn Cuddle Weather 3-Wick Candle, 14.5 oz | 411 g",
    "B07DN5KWHX": "Guinot Newhite Brightening Night Cream 50ml",
    "B00864DTJG": "Guinot Longue Vie Youth Renewing Serum 30ml",
    "B0D3WQQH89": "Logitech Ultimate Ears WONDERBOOM 4 Stereo portable speaker Grey, Yellow",
    "B0C7YKDLGD": "EAN-Search.org: 5000000000000 and higher (Page 38172)",
    "B0DV6CDXKT": "Ubiquiti Networks Ubiquiti UniFi Switch USW-FLEX",
    "B0BTZB7F88": "Amd Ryzen 7 7800x3d Box Only No Cpu",
}

BENCHMARK_CASES = []
for _case in _ORIGINAL_CASES:
    _c = dict(_case)
    _c["serpapi_retailer_title"] = _RETAILER_TITLES.get(_case["asin"], "")
    BENCHMARK_CASES.append(_c)


MAX_A2_SEARCHES = 3  # locate the retailer's real page only -- not open-ended discovery
MAX_A2_FETCHES = 4
MAX_BRAVE_QUERIES = 3
MAX_BRAVE_FETCHES = 4
MAX_TOKENS = 8192
MAX_LOOP_ITERATIONS = 8  # safety cap across pause_turn / client-tool rounds combined

BRAVE_SEARCH_TOOL = {
    "name": "brave_search",
    "description": "Search the open web via Brave Search (NOT Google Shopping -- a plain web search). Returns up to 10 results with title/url/description. Use this to find a retailer's own product page when you need a genuinely different candidate than the one already supplied.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query."}},
        "required": ["query"],
    },
}


def _amazon_product_block(case: dict) -> str:
    return (
        f"ASIN: {case['asin']}\n"
        f"Amazon title: {case['title']}\n"
        f"Brand: {case['brand']}\n"
        f"EAN/GTIN: {case['ean'] or 'unknown'}\n"
        f"MPN/model: {case['mpn'] or 'unknown'}\n"
        f"Amazon current price (GBP): {case['amazon_price_gbp']}\n"
    )


def _serpapi_evidence_block(case: dict) -> str:
    price = case["serpapi_price"]
    return (
        f"Retailer name (as returned by SerpAPI): {case['serpapi_retailer']}\n"
        f"Title SerpAPI matched against: {case['serpapi_retailer_title']}\n"
        f"Price SerpAPI reported: {'£' + str(price) if price is not None else 'no price returned'}\n"
        f"(Note: SerpAPI's own link for this result is a Google Shopping search-results redirect, "
        f"not the retailer's own page -- it cannot be fetched directly.)"
    )


class UsageAccumulator:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.searches = 0
        self.fetches = 0
        self.brave_queries = 0

    def add(self, response):
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        stu = getattr(response.usage, "server_tool_use", None)
        if stu:
            self.searches += getattr(stu, "web_search_requests", 0) or 0
            self.fetches += getattr(stu, "web_fetch_requests", 0) or 0

    def cost_usd(self) -> float:
        return (
            (self.input_tokens / 1_000_000) * SONNET5_INPUT_PER_MTOK_USD
            + (self.output_tokens / 1_000_000) * SONNET5_OUTPUT_PER_MTOK_USD
            + self.searches * WEB_SEARCH_COST_PER_SEARCH_USD
            + self.brave_queries * (BRAVE_COST_PER_1000_USD / 1000)
        )

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "web_searches": self.searches, "web_fetches": self.fetches,
            "brave_queries": self.brave_queries, "cost_usd": round(self.cost_usd(), 4),
        }


# =============================================================================
# STAGE A1 -- zero-cost text triage. No tools at all.
# =============================================================================
A1_PROMPT_PREAMBLE = """You are Atlas's OA (online arbitrage) sourcing research agent, running in TEXT-TRIAGE MODE.

You have NOT searched or fetched anything. You have been given exactly one candidate retailer match, as originally returned by SerpAPI's Google Shopping search -- nothing more. Your job in this stage is narrow: decide, from this text ALONE, whether the candidate can already be confidently REJECTED under the hard rejection rules below, or whether it needs real verification (a fetch of the actual page) before anyone could trust it.

You have no fetch access in this stage, so you can NEVER mark something as a genuinely verified source here -- "verified" requires checking the live page, which is stage 2's job, not this one. In this stage:
- If the supplied text ALONE already reveals a disqualifying conflict (wrong MPN/model visible in the title, wrong variant/pack/colour, obviously a marketplace/comparison/catalogue site by name, or similar), reject it now -- set stage_classification to "TEXT_REJECT" and record it in rejected_candidates. Do not wait for a fetch to reject something the text already disqualifies.
- If nothing in the text disqualifies it, but you also have not verified it against a real page, set stage_classification to "TEXT_PASS" -- leave verified_sources empty (you have not checked the live page, so you cannot claim a verified source yet).
- If the supplied evidence is too thin to judge either way (e.g. no real title or price at all, or a bare database/catalogue reference with nothing else), set stage_classification to "NO_USEFUL_SERPAPI_EVIDENCE".

"""


def _derive_stage_classification(parsed: dict | None, stage: str) -> str:
    """
    Claude reliably fills in the SHARED schema (candidates/rejected_
    candidates/verified_sources/confidence -- proven across the whole
    original benchmark), but an EXTRA field appended on top of "respond
    with EXACTLY this structure" is not reliable -- confirmed live: the
    A1 smoke test did all the right reasoning but omitted
    stage_classification entirely, presumably because the shared rules
    block's own "exactly this structure" instruction wins over an
    appended addition. Deriving it here from the fields Claude DOES
    reliably produce is more robust than trusting a bolted-on field --
    if Claude ever does include stage_classification explicitly, that
    is trusted first; this is the fallback, not a replacement.
    """
    if not parsed:
        return "NO_USEFUL_SERPAPI_EVIDENCE"

    explicit = parsed.get("stage_classification")
    if explicit:
        return explicit

    if stage == "a1":
        if parsed.get("rejected_candidates"):
            return "TEXT_REJECT"
        if parsed.get("candidates"):
            return "TEXT_PASS"
        return "NO_USEFUL_SERPAPI_EVIDENCE"

    # a2 / brave verification stages
    if parsed.get("best_verified_source") or parsed.get("verified_sources"):
        return "VERIFIED"
    if stage == "brave":
        return "REJECTED_AFTER_FETCH" if parsed.get("rejected_candidates") else "FALSE_NEGATIVE_MISSED"
    return "REJECTED_AFTER_FETCH"


def run_stage_a1(client: anthropic.Anthropic, case: dict) -> dict:
    system = A1_PROMPT_PREAMBLE + SHARED_RULES_BLOCK
    user_message = (
        _amazon_product_block(case) + "\n" + _serpapi_evidence_block(case) +
        "\n\nBased on this text alone, triage this one candidate. Respond with the JSON structure specified, "
        "plus one extra top-level field \"stage_classification\": \"TEXT_REJECT\" | \"TEXT_PASS\" | \"NO_USEFUL_SERPAPI_EVIDENCE\"."
    )
    usage = UsageAccumulator()
    response = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    usage.add(response)
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    parsed = extract_json_block(text)
    return {"parsed": parsed, "raw_text": text, "usage": usage}


# =============================================================================
# STAGE A2 -- selective verification (only for TEXT_PASS candidates).
# One web_search (to locate the retailer's real page) + web_fetch.
# =============================================================================
A2_PROMPT_PREAMBLE = f"""You are Atlas's OA (online arbitrage) sourcing research agent, running in VERIFICATION MODE.

A candidate from SerpAPI already passed an initial text-only triage (nothing in the snippet text disqualified it outright), so it is worth a real check. You do NOT have the retailer's actual URL (SerpAPI's own link is a Google Shopping redirect that cannot be fetched) -- use web_search (at most {MAX_A2_SEARCHES} searches) to locate the retailer's own product page, then use web_fetch (at most {MAX_A2_FETCHES} fetches) to verify it against the hard rejection rules below. Do not do open-ended product discovery beyond locating and checking THIS specific candidate -- if it turns out to be wrong, reject it; you are not being asked to keep searching for a different retailer in this stage.

"""


def run_stage_a2(client: anthropic.Anthropic, case: dict) -> dict:
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_A2_SEARCHES},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_A2_FETCHES, "citations": {"enabled": False}, "max_content_tokens": 3000},
    ]
    system = A2_PROMPT_PREAMBLE + SHARED_RULES_BLOCK
    user_message = (
        _amazon_product_block(case) + "\n" + _serpapi_evidence_block(case) +
        "\n\nThis candidate passed text-only triage. Verify or reject it properly now. Respond with the JSON structure "
        "specified, plus one extra top-level field \"stage_classification\": \"VERIFIED\" | \"REJECTED_AFTER_FETCH\"."
    )
    return _run_tool_loop(client, system, user_message, tools, use_brave=False)


# =============================================================================
# BRAVE FALLBACK -- used by Test B when SerpAPI's candidate is rejected
# or too thin (TEXT_REJECT / NO_USEFUL_SERPAPI_EVIDENCE at A1). A
# genuinely different candidate, found via Atlas's own existing Brave
# integration, then verified the same way.
# =============================================================================
BRAVE_PROMPT_PREAMBLE = f"""You are Atlas's OA (online arbitrage) sourcing research agent, running in FALLBACK DISCOVERY MODE.

SerpAPI's own candidate for this product was rejected or too thin to trust. Use the brave_search tool (at most {MAX_BRAVE_QUERIES} queries -- try exact MPN, exact EAN, and brand+model as needed) to find a genuinely different retailer, then use web_fetch (at most {MAX_BRAVE_FETCHES} fetches) to verify any promising result against the hard rejection rules below.

"""


def run_stage_brave_fallback(client: anthropic.Anthropic, case: dict) -> dict:
    tools = [
        BRAVE_SEARCH_TOOL,
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_BRAVE_FETCHES, "citations": {"enabled": False}, "max_content_tokens": 3000},
    ]
    system = BRAVE_PROMPT_PREAMBLE + SHARED_RULES_BLOCK
    user_message = (
        _amazon_product_block(case) +
        "\n\nSerpAPI's own candidate for this ASIN did not hold up (or gave no useful evidence). "
        "Find a genuinely different, verified source if one exists. Respond with the JSON structure specified, "
        "plus one extra top-level field \"stage_classification\": \"VERIFIED\" | \"REJECTED_AFTER_FETCH\" | \"FALSE_NEGATIVE_MISSED\" "
        "(use FALSE_NEGATIVE_MISSED only if you were unable to find or verify anything within your search/fetch budget)."
    )
    return _run_tool_loop(client, system, user_message, tools, use_brave=True)


def _run_tool_loop(client, system, user_message, tools, use_brave: bool) -> dict:
    """
    Mixed server-tool (web_fetch, web_search) + client-tool (brave_search)
    agentic loop. Server tools auto-execute inside the API and can return
    stop_reason=="pause_turn" (resend assistant content unchanged to
    continue); brave_search is a CLIENT tool -- stop_reason=="tool_use"
    means WE run it and send back a tool_result.
    """
    messages = [{"role": "user", "content": user_message}]
    usage = UsageAccumulator()
    final_text = ""
    error = None

    for _ in range(MAX_LOOP_ITERATIONS):
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages, tools=tools,
        )
        usage.add(response)

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "brave_search":
                    query = block.input.get("query", "")
                    results = brave_search_client.search(query, count=8)
                    usage.brave_queries += 1
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps(results) if results else "No results found.",
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue
            error = "tool_use stop_reason but no recognised client tool call found."
            break

        if response.stop_reason == "refusal":
            error = "Model refused this request."
            break

        final_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        break
    else:
        error = f"Hit MAX_LOOP_ITERATIONS ({MAX_LOOP_ITERATIONS}) without finishing."

    parsed = extract_json_block(final_text) if final_text else None
    if parsed is None and error is None:
        error = "Could not parse a JSON block from the model's final response."

    return {"parsed": parsed, "raw_text": final_text, "usage": usage, "error": error}


# =============================================================================
# Orchestration -- Test A and Test B per case.
# =============================================================================

def run_test_a(client: anthropic.Anthropic, case: dict) -> dict:
    a1 = run_stage_a1(client, case)
    total_usage = UsageAccumulator()
    total_usage.input_tokens += a1["usage"].input_tokens
    total_usage.output_tokens += a1["usage"].output_tokens

    a1_class = _derive_stage_classification(a1["parsed"], "a1")

    if a1_class == "TEXT_PASS":
        a2 = run_stage_a2(client, case)
        total_usage.input_tokens += a2["usage"].input_tokens
        total_usage.output_tokens += a2["usage"].output_tokens
        total_usage.searches += a2["usage"].searches
        total_usage.fetches += a2["usage"].fetches
        a2_class = _derive_stage_classification(a2["parsed"], "a2")
        final_classification = (
            "TEXT_PASS_VERIFIED" if a2_class == "VERIFIED" else "TEXT_PASS_REJECTED_AFTER_FETCH"
        )
        final_result = a2["parsed"]
        return {
            "asin": case["asin"], "stage_a1": a1["parsed"], "stage_a2": a2["parsed"],
            "final_classification": final_classification, "final_result": final_result,
            "usage": total_usage.as_dict(), "error": a2.get("error"),
        }

    final_classification = a1_class
    return {
        "asin": case["asin"], "stage_a1": a1["parsed"], "stage_a2": None,
        "final_classification": final_classification, "final_result": a1["parsed"],
        "usage": total_usage.as_dict(), "error": None if a1["parsed"] else "Stage A1 failed to parse.",
    }


def run_test_b(client: anthropic.Anthropic, case: dict) -> dict:
    a1 = run_stage_a1(client, case)
    total_usage = UsageAccumulator()
    total_usage.input_tokens += a1["usage"].input_tokens
    total_usage.output_tokens += a1["usage"].output_tokens

    a1_class = _derive_stage_classification(a1["parsed"], "a1")

    if a1_class == "TEXT_PASS":
        a2 = run_stage_a2(client, case)
        total_usage.input_tokens += a2["usage"].input_tokens
        total_usage.output_tokens += a2["usage"].output_tokens
        total_usage.searches += a2["usage"].searches
        total_usage.fetches += a2["usage"].fetches
        a2_class = _derive_stage_classification(a2["parsed"], "a2")
        final_classification = (
            "TEXT_PASS_VERIFIED" if a2_class == "VERIFIED" else "TEXT_PASS_REJECTED_AFTER_FETCH"
        )
        return {
            "asin": case["asin"], "stage_a1": a1["parsed"], "stage_2": a2["parsed"], "stage_2_type": "a2_verify",
            "final_classification": final_classification, "final_result": a2["parsed"],
            "usage": total_usage.as_dict(), "error": a2.get("error"),
        }

    # TEXT_REJECT or NO_USEFUL_SERPAPI_EVIDENCE -- SerpAPI's candidate is a
    # dead end, escalate to Brave for a genuinely different candidate.
    brave = run_stage_brave_fallback(client, case)
    total_usage.input_tokens += brave["usage"].input_tokens
    total_usage.output_tokens += brave["usage"].output_tokens
    total_usage.fetches += brave["usage"].fetches
    total_usage.brave_queries += brave["usage"].brave_queries
    final_classification = _derive_stage_classification(brave["parsed"], "brave")
    return {
        "asin": case["asin"], "stage_a1": a1["parsed"], "stage_2": brave["parsed"], "stage_2_type": "brave_fallback",
        "final_classification": final_classification, "final_result": brave["parsed"],
        "usage": total_usage.as_dict(), "error": brave.get("error"),
    }


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "model": MODEL, "test_a": [], "test_b": []}

    print(f"Running Test A (text triage + selective verification) on {len(BENCHMARK_CASES)} cases...\n")
    for i, case in enumerate(BENCHMARK_CASES, 1):
        print(f"[A {i}/{len(BENCHMARK_CASES)}] {case['asin']}")
        result = run_test_a(client, case)
        result["case"] = case
        report["test_a"].append(result)
        print(f"    -> {result['final_classification']} | usage: {result['usage']}")
        if result.get("error"):
            print(f"    ERROR: {result['error']}")
        print()

    print(f"Running Test B (text triage + selective verification + Brave fallback) on {len(BENCHMARK_CASES)} cases...\n")
    for i, case in enumerate(BENCHMARK_CASES, 1):
        print(f"[B {i}/{len(BENCHMARK_CASES)}] {case['asin']}")
        result = run_test_b(client, case)
        result["case"] = case
        report["test_b"].append(result)
        print(f"    -> {result['final_classification']} | usage: {result['usage']}")
        if result.get("error"):
            print(f"    ERROR: {result['error']}")
        print()

    for test_name in ("test_a", "test_b"):
        results = report[test_name]
        n = len(results)
        total_cost = sum(r["usage"]["cost_usd"] for r in results)
        total_searches = sum(r["usage"]["web_searches"] for r in results)
        total_fetches = sum(r["usage"]["web_fetches"] for r in results)
        total_brave = sum(r["usage"]["brave_queries"] for r in results)
        total_input = sum(r["usage"]["input_tokens"] for r in results)
        total_output = sum(r["usage"]["output_tokens"] for r in results)
        n_verified = sum(1 for r in results if "VERIFIED" in r["final_classification"])
        report[f"{test_name}_aggregate"] = {
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_asin_usd": round(total_cost / n, 4) if n else 0,
            "estimated_cost_per_1000_asins_usd": round((total_cost / n) * 1000, 2) if n else 0,
            "avg_searches_per_asin": round(total_searches / n, 2) if n else 0,
            "avg_fetches_per_asin": round(total_fetches / n, 2) if n else 0,
            "avg_brave_queries_per_asin": round(total_brave / n, 2) if n else 0,
            "avg_input_tokens_per_asin": round(total_input / n, 0) if n else 0,
            "avg_output_tokens_per_asin": round(total_output / n, 0) if n else 0,
            "n_verified_sources": n_verified,
            "cost_per_verified_source_usd": round(total_cost / n_verified, 4) if n_verified else None,
            "classification_breakdown": {
                cls: sum(1 for r in results if r["final_classification"] == cls)
                for cls in set(r["final_classification"] for r in results)
            },
        }

    out_path = os.path.join(os.path.dirname(__file__), "oa_research_agent_cost_benchmark_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("=" * 100)
    for test_name in ("test_a", "test_b"):
        agg = report[f"{test_name}_aggregate"]
        print(f"{test_name.upper()}: total ${agg['total_cost_usd']} | avg ${agg['avg_cost_per_asin_usd']}/ASIN | "
              f"est. ${agg['estimated_cost_per_1000_asins_usd']}/1000 ASINs | "
              f"cost/verified source: ${agg['cost_per_verified_source_usd']}")
        print(f"   classification breakdown: {agg['classification_breakdown']}")
    print(f"\nFull report written to: {out_path}")
    print("=" * 100)


if __name__ == "__main__":
    main()
