"""
OA Source Intelligence research worker -- "Atlas -- OA Source
Intelligence & Competitor Opportunity Engine" brief, 2026-09-05.

SHADOW MODE ONLY. This module writes OaResearchFinding rows and nothing
else -- no save_opportunity, no Review Queue write, no SellerNewListing
mutation, no Keepa/SP-API call, no scheduler wiring. See that model's
own docstring and the approved plan for the full safety rationale.

Reuses, unchanged:
  - OaSourceDiscoveryService._eligible_candidate_rows/_within_window
    for candidate selection (same "OA / unclear" + has-potential gate
    Competitor Watch's own OA tab already uses).
  - FeeEngine.max_source_cost for the ONLY economics calculation --
    never reimplemented here.
  - brave_search_client.search for the fallback path.
  - oa_domain_classifier for retailer/marketplace/catalogue
    classification vocabulary.

Verification logic is adapted from the validated benchmark scripts
(oa_research_agent_prototype.py / oa_research_agent_cost_benchmark.py --
see oa_research_agent_20case_benchmark_report.json for the numbers that
justified this architecture), hardened per that benchmark's own
findings (search/fetch budgets widened, retry-on-failure added) and
extended with:
  - a CURRENT vs HISTORICAL evidence split in the output schema (never
    conflated -- see OaResearchFinding's own docstring)
  - a bounded historical-evidence pass, run only when no current
    viable price was found
  - a hard requirement to use hedged inference language ("likely
    source", never "competitor bought this from X")
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta

import anthropic

from app.database.database import SessionLocal
from app.database.models import OaResearchFinding, OaSourceCandidate, SellerNewListing
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.fee_engine import FeeEngine
from app.services import brave_search_client
from app.models.product import Product

MODEL = "claude-sonnet-5"
SONNET5_INPUT_PER_MTOK_USD = 2.00
SONNET5_OUTPUT_PER_MTOK_USD = 10.00
WEB_SEARCH_COST_PER_SEARCH_USD = 0.01
BRAVE_COST_PER_1000_USD = brave_search_client.COST_PER_1000_USD

MAX_A2_SEARCHES = 6   # widened again (was 5) after the first live shadow batch's
MAX_A2_FETCHES = 6    # Stihl BGA 45 miss (id=4): the model correctly retried ONE
                      # retailer (pteshop.co.uk) twice and gave up rather than
                      # trying a second candidate -- UK Planet Tools, the retailer
                      # a human found at £73.99, was never even searched for. More
                      # budget alone doesn't fix that (see the A2_PREAMBLE rewrite
                      # below, which now requires trying a SECOND candidate before
                      # giving up), but headroom is needed for it to be possible.
MAX_BRAVE_QUERIES = 3
MAX_BRAVE_FETCHES = 4
MAX_HISTORICAL_SEARCHES = 2
MAX_HISTORICAL_FETCHES = 2
MAX_TOKENS = 8192
MAX_LOOP_ITERATIONS = 12  # was 10 -- headroom for the second-candidate requirement below

# =============================================================================
# Shared identity/verification rules -- same content validated across two
# prior benchmarks, extended with the hedged-attribution requirement.
# =============================================================================
SHARED_RULES_BLOCK = """
HARD REJECTION RULES -- these are absolute. A fuzzy title similarity NEVER overrides a hard identifier conflict:
- EAN/GTIN conflict -> reject.
- MPN/model conflict -> reject (e.g. the page's own model number differs from the Amazon MPN, even if brand and general title look similar -- this is the single most common real failure mode you are being tested against).
- Wrong pack quantity -> reject.
- Wrong size/colour/variant -> reject.
- Compatible/accessory-only product (e.g. a case, cable, or "works with X" item that is not X itself) -> reject.
- Empty box / parts-only / used / refurbished when the Amazon listing is for a new unit -> reject.
- A catalogue/specification/EAN-lookup site with no actual purchasable offer -> this is NOT a retailer source, classify it as CATALOGUE_DATABASE and do not treat it as a candidate retailer.
- Retailer wording differing from Amazon's own title text is NOT a rejection reason by itself -- brands routinely market the same exact product under slightly different names at different retailers. Only reject on an actual identifier/spec/variant/condition conflict, never on wording alone. If uncertain, do not reject -- pass to verification.

RETAILER CLASSIFICATION -- classify every candidate as exactly one of:
- RETAILER (a real shop selling this item directly, with a purchasable offer)
- MARKETPLACE (e.g. eBay, a marketplace where a THIRD PARTY seller lists it, not the retailer itself -- may still be returned but must never be treated as equivalent to a normal retailer)
- CATALOGUE_DATABASE (a spec sheet, EAN lookup, or database page with no purchasable offer)
- COMPARISON_SEARCH (a price comparison or search-aggregator page, not a direct seller)
- OTHER
Only RETAILER candidates should normally become a "verified source". A MARKETPLACE candidate may be listed separately with classification "MARKETPLACE" if you are confident of the match, but must be clearly labelled as such, never presented as an ordinary retailer. Amazon itself, on any marketplace, is NEVER a valid OA source -- classify as OTHER and reject if a candidate resolves to an amazon.* domain or seller name.

CURRENT vs HISTORICAL EVIDENCE -- these are DIFFERENT things, report them separately, never conflate:
- CURRENT: a live, purchasable price on the retailer's page today.
- HISTORICAL: evidence the exact product was sold at a specific price in the past (a "was £X now £Y" page, a dated promotion, an out-of-stock page still showing its last price, a cached/indexed page with a visible date). Historical evidence is valuable even when there is no current viable price -- do not discard it just because today's price (or lack of stock) makes it non-actionable right now.

ATTRIBUTION LANGUAGE -- you are never able to prove where a competitor actually bought their stock. Always use hedged language: "likely source", "possible source", "consistent with". NEVER state or imply certainty such as "the competitor bought this from X" unless you have direct, explicit evidence of that specific transaction (which will essentially never be available from public retailer pages).

PRICE VERIFICATION -- for each candidate you actually fetch and verify, record: current item price, currency, VAT status if identifiable, delivery cost if identifiable, effective cost (price + delivery), stock/availability, the product URL, and that you checked it today. Do NOT assume a search-result snippet's price is still correct if you were able to fetch the actual page -- always prefer the fetched page's own price when you have it.
"""

OUTPUT_SCHEMA_BLOCK = """
OUTPUT -- respond with ONLY a single fenced ```json code block (no other text before or after it) containing exactly this structure:
{
  "asin": "<the ASIN you were given>",
  "candidates": [{"retailer": "...", "url": "...", "title_seen": "...", "price": <number or null>, "currency": "GBP", "classification": "RETAILER|MARKETPLACE|CATALOGUE_DATABASE|COMPARISON_SEARCH|OTHER", "fetched": true|false}],
  "rejected_candidates": [{"retailer": "...", "url": "...", "reason": "EAN_CONFLICT|MPN_CONFLICT|WRONG_VARIANT|WRONG_PACK|WRONG_SIZE_COLOUR|ACCESSORY_ONLY|USED_REFURBISHED|NOT_A_RETAILER|OTHER", "detail": "..."}],
  "verified_current_source": {"retailer": "...", "url": "...", "classification": "RETAILER|MARKETPLACE", "price": <number>, "currency": "GBP", "delivery_cost": <number or null>, "effective_cost": <number>, "stock_status": "in_stock|out_of_stock|unclear", "evidence": "why you believe this is the exact same product"} or null,
  "verified_historical_source": {"retailer": "...", "url": "...", "price": <number>, "observed_note": "e.g. sale ended ~5 days ago / listed 28 Aug", "evidence": "..."} or null,
  "source_confidence": "HIGH|MEDIUM|LOW",
  "atlas_inference": "1-2 sentences, hedged language only, e.g. 'Currys sold the exact product at a price consistent with...'",
  "stage_classification": "<see the stage-specific instruction above>"
}
If nothing was found or verified, set the relevant field(s) to null and use hedged confidence "LOW" rather than omitting them.
"""

BRAVE_SEARCH_TOOL = {
    "name": "brave_search",
    "description": "Search the open web via Brave Search (a plain web search, not Google Shopping). Returns up to 10 results with title/url/description.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query."}},
        "required": ["query"],
    },
}


def extract_json_block(text: str) -> dict | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        match = re.search(r"(\{.*\})", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _amazon_product_block(asin: str, record) -> str:
    return (
        f"ASIN: {asin}\n"
        f"Amazon title: {record.title}\n"
        f"Brand: {record.brand}\n"
        f"EAN/GTIN: {record.ean or 'unknown'}\n"
        f"Amazon current price (GBP): {record.buy_box_now}\n"
    )


def _existing_evidence_block(candidate_row) -> str:
    if candidate_row is None:
        return "No existing SerpAPI/OA Source Discovery evidence on file for this ASIN -- this is a fresh investigation."
    return (
        f"Existing OA Source Discovery evidence (from a prior automated run):\n"
        f"Retailer name: {candidate_row.retailer_domain or 'none found'}\n"
        f"Title seen: {candidate_row.retailer_title or 'n/a'}\n"
        f"Price seen: {'£' + str(candidate_row.retailer_price_gbp) if candidate_row.retailer_price_gbp is not None else 'no price'}\n"
        f"MPN on file: {candidate_row.mpn or 'unknown'}\n"
        f"Match tier assigned: {candidate_row.match_tier or 'none'}\n"
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

    def merge(self, other: "UsageAccumulator"):
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.searches += other.searches
        self.fetches += other.fetches
        self.brave_queries += other.brave_queries


# =============================================================================
# STAGE A1 -- zero-cost text triage. Validated at 0% false-reject rate
# across 10 independently-verified known-good cases (20-case benchmark).
# =============================================================================
A1_PREAMBLE = """You are Atlas's OA (online arbitrage) sourcing research agent, running in TEXT-TRIAGE MODE.

You have NOT searched or fetched anything yet. Decide, from the text alone, whether the existing evidence already reveals a disqualifying conflict (wrong MPN/model, wrong variant/pack/colour, an obvious marketplace/comparison/catalogue site), or whether this needs real verification. You have no fetch access in this stage, so NEVER claim a verified source here.

Set "stage_classification" to "TEXT_REJECT" (put the conflict in rejected_candidates), "TEXT_PASS" (nothing disqualifies it from the text alone, but nothing is verified yet either), or "NO_USEFUL_SERPAPI_EVIDENCE" (too thin to judge). When in doubt, TEXT_PASS -- the cost of this stage being wrong is a wasted verification call, not a lost opportunity.
"""


def run_stage_a1(client, asin, record, candidate_row) -> dict:
    system = A1_PREAMBLE + SHARED_RULES_BLOCK + OUTPUT_SCHEMA_BLOCK
    user_message = _amazon_product_block(asin, record) + "\n" + _existing_evidence_block(candidate_row)
    usage = UsageAccumulator()
    response = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=[{"role": "user", "content": user_message}])
    usage.add(response)
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return {"parsed": extract_json_block(text), "usage": usage}


# =============================================================================
# STAGE A2 -- selective verification. Hardened 2026-09-05 after the
# first live shadow batch: the Stihl BGA 45 case (B072NBFYMK) found one
# real candidate (pteshop.co.uk), retried its fetch once as instructed,
# got truncated navigation-only content both times, and gave up --
# never even searching for a SECOND retailer. A human then found a
# genuinely viable source (UK Planet Tools, £73.99, exact SKU) that the
# worker never looked for. The verification STANDARD is unchanged --
# a snippet is still never sufficient, only a confirmed page fetch
# counts as VERIFIED_CURRENT -- what's hardened is RECOVERY: don't stop
# at the first candidate's fetch trouble, try a second one.
# =============================================================================
A2_PREAMBLE = f"""You are Atlas's OA (online arbitrage) sourcing research agent, running in VERIFICATION MODE.

SEARCH STRATEGY -- use web_search (up to {MAX_A2_SEARCHES} searches) with a MIX of query types, not near-duplicates of the same phrasing:
1. Exact MPN (from "MPN on file" above, if known) + "buy UK", to locate the retailer's own product page directly.
2. Exact EAN/GTIN, if MPN alone is weak or missing.
3. Exact Amazon title (or the most distinctive part of it) + "buy UK" / a specific retailer name, if one is already known from existing evidence.
4. Brand + model, as a broader fallback.
SerpAPI/Google Shopping links are redirects and cannot be fetched directly -- you must locate the retailer's OWN page via search.

FETCH + RETRY PROTOCOL -- use web_fetch (up to {MAX_A2_FETCHES} fetches):
- If a fetch fails or returns truncated/navigation-only content with no price, RETRY the same URL once.
- If it still fails, try an ALTERNATE URL for the same retailer if one is available (e.g. a category/listing page that also shows the price, as corroborating evidence -- but a confirmed price still requires the product's own page wherever possible).
- If the FIRST candidate retailer cannot be confirmed after the above, you MUST search for and attempt to verify a SECOND, different candidate retailer before concluding FETCH_FAILED or SEARCH_EXHAUSTED -- do not stop at one candidate's fetch trouble. This is the single most important change here: a fetch problem with retailer A is not evidence that no retailer sells this.
- Only after a genuine second attempt (different retailer or, if none was found at all, a second distinct search strategy) still yields nothing confirmable should you give up for this stage.

None of this lowers the verification bar -- a search snippet alone is NEVER sufficient for "VERIFIED_CURRENT" regardless of how many retailers you tried; it must come from an actually-fetched page (or, at minimum, multiple independent same-domain indexed pages showing the identical figure, as your own evidence field should state plainly if that's the basis).

Set "stage_classification" to "VERIFIED_CURRENT" (a live price actually confirmed via fetch), "FETCH_FAILED" (found one or more real candidate pages, could not load/confirm any of them after retrying and trying a second candidate -- there is LIKELY a real source here, it just couldn't be confirmed this pass), "SEARCH_EXHAUSTED" (ran out of search budget without finding any credible candidate retailer at all), "NO_MATCH" (verified this is NOT the same product), or "CONFLICTING_MATCH" (direct identifier conflict found). FETCH_FAILED and SEARCH_EXHAUSTED are meaningfully different from NO_MATCH -- they mean "inconclusive", not "wrong" -- so never use NO_MATCH just because verification proved difficult.
"""


def run_stage_a2(client, asin, record, candidate_row) -> dict:
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_A2_SEARCHES},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_A2_FETCHES, "citations": {"enabled": False}, "max_content_tokens": 3000},
    ]
    system = A2_PREAMBLE + SHARED_RULES_BLOCK + OUTPUT_SCHEMA_BLOCK
    user_message = _amazon_product_block(asin, record) + "\n" + _existing_evidence_block(candidate_row)
    return _run_tool_loop(client, system, user_message, tools)


# =============================================================================
# BRAVE FALLBACK -- bounded, used only when A2 didn't confirm a current
# source. Reuses Atlas's own existing brave_search_client.
# =============================================================================
BRAVE_PREAMBLE = f"""You are Atlas's OA (online arbitrage) sourcing research agent, running in FALLBACK DISCOVERY MODE.

The existing candidate did not hold up, or A2 could not confirm it (which may itself have been a fetch/search problem, not proof no source exists -- see the evidence you were given). Use brave_search (up to {MAX_BRAVE_QUERIES} queries -- try exact EAN, exact MPN/brand+model, and brand+title as needed) to find a genuinely different retailer, then web_fetch (up to {MAX_BRAVE_FETCHES} fetches) to verify it. If a fetch fails, retry once, then try a second candidate retailer before giving up -- same discipline as verification mode: one retailer's fetch trouble is not evidence no source exists.

Set "stage_classification" to "VERIFIED_CURRENT" (confirmed via fetch), "FETCH_FAILED" (found a real candidate, could not confirm it after retrying), "SEARCH_EXHAUSTED" (no credible candidate found within budget), or "NO_MATCH" (verified wrong product). FETCH_FAILED means "likely a real source, unconfirmed" -- never relabel it NO_MATCH just because it was hard to confirm.
"""


def run_stage_brave_fallback(client, asin, record) -> dict:
    tools = [
        BRAVE_SEARCH_TOOL,
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_BRAVE_FETCHES, "citations": {"enabled": False}, "max_content_tokens": 3000},
    ]
    system = BRAVE_PREAMBLE + SHARED_RULES_BLOCK + OUTPUT_SCHEMA_BLOCK
    user_message = _amazon_product_block(asin, record) + "\nNo current SerpAPI candidate held up. Find a genuinely different verified source if one exists."
    return _run_tool_loop(client, system, user_message, tools)


# =============================================================================
# HISTORICAL PASS -- new (brief §12), bounded, run ONLY when no current
# viable price was found by A2 or Brave fallback.
# =============================================================================
HISTORICAL_PREAMBLE = f"""You are Atlas's OA (online arbitrage) sourcing research agent, running in HISTORICAL EVIDENCE MODE.

No current viable source was found for this product. Before giving up, use brave_search (up to {MAX_HISTORICAL_SEARCHES} queries) specifically for HISTORICAL evidence: "was £X now £Y" pages, recently-ended promotions, an out-of-stock retailer page still showing its last price, or any dated evidence this exact product was recently sold cheaper. Use web_fetch (up to {MAX_HISTORICAL_FETCHES} fetches) to confirm anything promising. This is a narrow, bounded search -- if nothing turns up quickly, stop and report nothing found rather than searching exhaustively.

Set "stage_classification" to "VERIFIED_HISTORICAL" (found and confirmed historical evidence) or "NO_MATCH" (nothing found).
"""


def run_historical_pass(client, asin, record) -> dict:
    tools = [
        BRAVE_SEARCH_TOOL,
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_HISTORICAL_FETCHES, "citations": {"enabled": False}, "max_content_tokens": 3000},
    ]
    system = HISTORICAL_PREAMBLE + SHARED_RULES_BLOCK + OUTPUT_SCHEMA_BLOCK
    user_message = _amazon_product_block(asin, record) + "\nLook specifically for HISTORICAL price/promotion evidence now."
    return _run_tool_loop(client, system, user_message, tools)


def _run_tool_loop(client, system, user_message, tools) -> dict:
    """Mixed server-tool (web_fetch/web_search) + client-tool (brave_search) loop -- unchanged pattern from the validated benchmark scripts."""
    messages = [{"role": "user", "content": user_message}]
    usage = UsageAccumulator()
    final_text = ""
    error = None

    for _ in range(MAX_LOOP_ITERATIONS):
        response = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages, tools=tools)
        usage.add(response)

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "brave_search":
                    results = brave_search_client.search(block.input.get("query", ""), count=8)
                    usage.brave_queries += 1
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(results) if results else "No results found."})
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

    return {"parsed": parsed, "usage": usage, "error": error}


# =============================================================================
# Orchestration
# =============================================================================

def get_shadow_batch(limit: int = 10) -> list:
    """
    Top `limit` real "OA / unclear" competitor candidates ranked by
    opportunity value (FeeEngine.max_source_cost ceiling -- the most a
    genuine source could cost and still hit target ROI), restricted to
    a recent window so this reflects "top opportunities found from
    competitors per day" rather than an arbitrary slice of the whole
    backlog. Widens the window (1 -> 3 -> 7 -> None days) only as far
    as needed to reach `limit` candidates.
    """
    rows = OaSourceDiscoveryService._eligible_candidate_rows()

    def value(row) -> float:
        rec = row["record"]
        return FeeEngine.max_source_cost(rec.buy_box_now, rec.category_name, rec.fba_fee, FeeEngine.OA_TARGET_ROI_PCT)

    for since_days in (1, 3, 7, None):
        windowed = OaSourceDiscoveryService._within_window(rows, since_days) if since_days else rows
        if len(windowed) >= limit or since_days is None:
            return sorted(windowed, key=value, reverse=True)[:limit]
    return []


def _latest_candidate_row(db, asin: str):
    return (
        db.query(OaSourceCandidate)
        .filter(OaSourceCandidate.asin == asin)
        .order_by(OaSourceCandidate.created_at.desc())
        .first()
    )


def _existing_pipeline_outcome(candidate_row) -> str:
    if candidate_row is None:
        return "no_prior_oa_source_discovery_run"
    if candidate_row.added_to_review_queue:
        return "promoted_to_review_queue"
    if candidate_row.outcome == "candidate_found":
        return f"candidate_found_not_promoted (tier={candidate_row.match_tier or 'none'})"
    return "no_retailer_found"


def _classify_current_status(record, price: float | None, stock_status: str | None) -> str:
    if price is None:
        return "UNKNOWN"
    if stock_status == "out_of_stock":
        return "OUT_OF_STOCK"
    if price >= record.buy_box_now:
        return "ABOVE_AMAZON"
    breakeven = FeeEngine.max_source_cost(record.buy_box_now, record.category_name, record.fba_fee, target_roi_pct=0.0)
    return "VIABLE" if price <= breakeven else "NOT_VIABLE"


def _recommend_outcome(record, current_status: str, price: float | None) -> tuple:
    """
    HARD RULE (Tamara, 2026-09-05): no viable current price -> ALWAYS
    SOURCE_INTELLIGENCE, never BUY_NOW/BORDERLINE, regardless of how
    strong historical evidence is. Returns (outcome, profit, roi).
    """
    if current_status != "VIABLE" or price is None:
        return "SOURCE_INTELLIGENCE", None, None

    # Same "UK-OA" hypothetical-Product convention every other OA path in
    # this codebase uses (see OaSourceDiscoveryService.save_manual_source) --
    # never a separate profit/ROI formula.
    hypothetical = Product(
        asin=record.asin, title=record.title, brand=record.brand, category="",
        ean=record.ean, buy_box_now=record.buy_box_now, fba_fee=record.fba_fee,
        best_source_marketplace="UK-OA", best_source_cost_gbp=price,
    )
    fees = FeeEngine.calculate(hypothetical, category_name=record.category_name)
    target = FeeEngine.max_source_cost(record.buy_box_now, record.category_name, record.fba_fee, FeeEngine.OA_TARGET_ROI_PCT)
    outcome = "BUY_NOW" if price <= target else "BORDERLINE"
    return outcome, round(fees.profit, 2), round(fees.roi, 1)


def _extract_verified_evidence(stage_classification: str, parsed: dict) -> tuple:
    """
    Real bug found reading a live shadow result (Stihl BGA 45,
    2026-09-05): the model set stage_classification="FETCH_FAILED" but
    STILL populated verified_current_source with a plausible, snippet-
    corroborated (not page-fetch-confirmed) price -- honest of the
    model to report what it found, but this field must NEVER be
    trusted for economics/status unless the stage's OWN classification
    actually says VERIFIED_CURRENT/VERIFIED_HISTORICAL. Trusting it
    regardless of stage would let unconfirmed evidence feed the hard
    BUY_NOW/BORDERLINE rule. Returns (current, historical).
    """
    current = parsed.get("verified_current_source") if stage_classification == "VERIFIED_CURRENT" else None
    historical = parsed.get("verified_historical_source") if stage_classification in ("VERIFIED_CURRENT", "VERIFIED_HISTORICAL") else None
    return current, historical


def run_research_for_candidate(client, row: dict) -> dict:
    listing = row["listing"]
    record = row["record"]
    asin = listing.asin

    db = SessionLocal()
    try:
        candidate_row = _latest_candidate_row(db, asin)
        existing_outcome = _existing_pipeline_outcome(candidate_row)
    finally:
        db.close()

    total_usage = UsageAccumulator()
    reasoning = {}

    a1 = run_stage_a1(client, asin, record, candidate_row)
    total_usage.merge(a1["usage"])
    a1_parsed = a1["parsed"] or {}
    a1_class = a1_parsed.get("stage_classification") or ("TEXT_REJECT" if a1_parsed.get("rejected_candidates") else "TEXT_PASS")
    reasoning["a1"] = a1_parsed

    result_state = None
    current = None
    historical = None
    confidence = "LOW"
    inference = ""

    if a1_class == "TEXT_REJECT":
        result_state = "NO_MATCH"
        reasoning["a1_rejection"] = a1_parsed.get("rejected_candidates")
    else:
        a2 = run_stage_a2(client, asin, record, candidate_row)
        total_usage.merge(a2["usage"])
        a2_parsed = a2["parsed"] or {}
        reasoning["a2"] = a2_parsed
        stage = a2_parsed.get("stage_classification", "SEARCH_EXHAUSTED" if a2["error"] else "NO_MATCH")
        final_parsed = a2_parsed

        if stage != "VERIFIED_CURRENT":
            brave = run_stage_brave_fallback(client, asin, record)
            total_usage.merge(brave["usage"])
            brave_parsed = brave["parsed"] or {}
            reasoning["brave"] = brave_parsed
            stage = brave_parsed.get("stage_classification", "SEARCH_EXHAUSTED" if brave["error"] else "NO_MATCH")
            final_parsed = brave_parsed  # brave ran last -- its own read of the outcome wins, whatever it is

        result_state = stage
        current, historical = _extract_verified_evidence(stage, final_parsed)
        confidence = final_parsed.get("source_confidence", "LOW")
        inference = final_parsed.get("atlas_inference", "")

        if current is None:
            hist_pass = run_historical_pass(client, asin, record)
            total_usage.merge(hist_pass["usage"])
            hist_parsed = hist_pass["parsed"] or {}
            reasoning["historical_pass"] = hist_parsed
            if hist_parsed.get("stage_classification") == "VERIFIED_HISTORICAL" and hist_parsed.get("verified_historical_source"):
                historical = hist_parsed["verified_historical_source"]
                result_state = "VERIFIED_HISTORICAL"
                confidence = hist_parsed.get("source_confidence", confidence)
                inference = hist_parsed.get("atlas_inference", inference) or inference

    current_price = current.get("price") if current else None
    current_stock = (current.get("stock_status") if current else None)
    current_status = _classify_current_status(record, current_price, current_stock)
    recommended_outcome, profit, roi_pct = _recommend_outcome(record, current_status, current_price)

    if current is None and historical is None:
        # FETCH_FAILED is deliberately kept out of NO_USEFUL_SOURCE (Tamara,
        # 2026-09-05): it means "a likely real candidate exists, verification
        # was inconclusive" -- worth a recheck, not the same as NO_MATCH/
        # SEARCH_EXHAUSTED genuinely finding nothing. Routed to Source
        # Intelligence so it isn't silently dropped, exactly like the real
        # Stihl BGA 45 case this hardening pass was built to catch.
        recommended_outcome = "SOURCE_INTELLIGENCE" if result_state == "FETCH_FAILED" else "NO_USEFUL_SOURCE"

    finding = {
        "seller_new_listing_id": listing.id,
        "asin": asin,
        "result_state": result_state or "NO_MATCH",
        "current_retailer": (current or {}).get("retailer", ""),
        "current_url": (current or {}).get("url", ""),
        "current_price_gbp": current_price,
        "current_stock_text": (current or {}).get("stock_status", ""),
        "current_checked_at": datetime.now(timezone.utc) if current else None,
        "historical_retailer": (historical or {}).get("retailer", ""),
        "historical_price_gbp": (historical or {}).get("price"),
        "historical_observed_note": (historical or {}).get("observed_note", ""),
        "historical_checked_at": datetime.now(timezone.utc) if historical else None,
        "current_source_status": current_status,
        "historical_source_status": "FOUND" if historical else "NOT_FOUND",
        "sourcing_classification": "OA",  # this worker only runs against the "OA / unclear" pool by construction
        "source_confidence": confidence,
        "atlas_inference": inference,
        "recommended_outcome": recommended_outcome,
        "estimated_profit_gbp": profit,
        "estimated_roi_pct": roi_pct,
        "existing_pipeline_outcome": existing_outcome,
        "queries_used_json": json.dumps(reasoning.get("a2", {}).get("search_strategies_used", []) if isinstance(reasoning.get("a2"), dict) else []),
        "reasoning_json": json.dumps(reasoning, default=str)[:20000],
        "cost_usd": round(total_usage.cost_usd(), 4),
    }
    return finding


def save_finding(finding: dict) -> int:
    db = SessionLocal()
    try:
        row = OaResearchFinding(**finding)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def record_human_verdict(finding_id: int, verdict: str, notes: str = "") -> None:
    db = SessionLocal()
    try:
        row = db.query(OaResearchFinding).filter(OaResearchFinding.id == finding_id).first()
        if row:
            row.human_verdict = verdict
            row.human_notes = notes
            row.reviewed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def run_shadow_batch(limit: int = 10) -> dict:
    """
    THE shadow entry point. Writes OaResearchFinding rows; makes NO
    other write anywhere in Atlas. Returns a summary dict + the list of
    finding ids/dicts for reporting.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    client = anthropic.Anthropic(api_key=api_key)

    batch = get_shadow_batch(limit)
    results = []
    for row in batch:
        finding = run_research_for_candidate(client, row)
        finding_id = save_finding(finding)
        finding["id"] = finding_id
        finding["title"] = row["record"].title
        results.append(finding)

    total_cost = sum(r["cost_usd"] for r in results)
    summary = {
        "n_candidates": len(results),
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_asin_usd": round(total_cost / len(results), 4) if results else 0,
        "by_outcome": {
            outcome: sum(1 for r in results if r["recommended_outcome"] == outcome)
            for outcome in set(r["recommended_outcome"] for r in results)
        },
        "by_result_state": {
            state: sum(1 for r in results if r["result_state"] == state)
            for state in set(r["result_state"] for r in results)
        },
    }
    return {"results": results, "summary": summary}
