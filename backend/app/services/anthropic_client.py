import os
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# -> Atlas/.env, same location app/keepa/client.py loads KEEPA_API_KEY from.
# Real bug fixed 2026-09-06: this was parents[2] (-> backend/.env, a
# DIFFERENT, near-empty file), one level too shallow versus keepa/
# client.py's own (correct) parents[3] -- despite the comment above
# claiming parity. Silently worked until now purely because keepa/
# client.py's own load_dotenv(root .env) happens to run first at import
# time and populates the process environment, which load_dotenv here
# then found already set (it never overrides existing env vars) --
# fragile, import-order-dependent, and would have silently broken
# GOOGLE_API_KEY (added here, not in backend/.env) if left uncorrected.
env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

# The user's own editable buying-criteria doc (sourcing agent brief
# step 8) -- same directory as .env above, i.e. the repo root next to
# criteria.md. Read fresh on every verdict, not cached: the whole point
# is that editing this file changes what Claude weighs with no restart.
CRITERIA_DOC_PATH = Path(__file__).resolve().parents[2] / "criteria.md"

MODEL = "claude-sonnet-5"

VERDICT_VALUES = ("BUY", "WATCH", "AVOID")

# Safety net only -- the prompt is explicit about the exact three words,
# but Claude occasionally reaches for a natural synonym instead (seen live:
# "PASS" for AVOID). Normalizing a close, unambiguous synonym is safer than
# hard-failing an otherwise-good analysis over word choice; anything not in
# this map still raises, since a genuinely wrong word shouldn't be guessed.
VERDICT_SYNONYMS = {
    "PASS": "AVOID", "SKIP": "AVOID", "NO": "AVOID", "REJECT": "AVOID",
    "YES": "BUY", "GO": "BUY",
    "HOLD": "WATCH", "MAYBE": "WATCH", "CONSIDER": "WATCH", "MONITOR": "WATCH",
}

# Cached singleton -- same reasoning as app/keepa/client.py's get_keepa_client:
# avoid constructing a fresh client (and its underlying HTTP connection pool)
# on every verdict call.
_cached_client = None


def get_anthropic_client():
    global _cached_client

    if _cached_client is not None:
        return _cached_client

    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not found")

    _cached_client = anthropic.Anthropic(api_key=api_key)

    return _cached_client


# Gemini fallback for generate_verdict (2026-09-06) -- see that
# function's own docstring. Free-tier model; cached singleton, same
# reasoning as _cached_client above.
GEMINI_MODEL = "gemini-3.6-flash"
_cached_gemini_client = None


def get_gemini_client():
    global _cached_gemini_client

    if _cached_gemini_client is not None:
        return _cached_gemini_client

    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not found -- cannot fall back to Gemini")

    from google import genai
    _cached_gemini_client = genai.Client(api_key=api_key)

    return _cached_gemini_client


def _format_metrics_summary(metrics: dict) -> str:
    """
    Pre-formats VerdictService.compute_metrics' raw dict into a
    labelled, human-readable summary instead of handing Claude a bare
    JSON dump. Built after real mismatches were reported live: Claude
    calling a positive profit "not profitable", contradicting a stated
    16% margin with "margin isn't that high" in the same breath, and
    treating Amazon's mere presence on a listing the same as Amazon
    actually dominating the buy box. A raw dict forces Claude to
    re-derive which number means what on every call; naming each
    figure explicitly here removes that guesswork at the source rather
    than hoping the prompt's rules alone catch it downstream.
    """
    profit = metrics.get("keepa_estimate_profit")
    roi = metrics.get("keepa_estimate_roi")
    margin = metrics.get("keepa_estimate_margin")
    profit_90d = metrics.get("keepa_estimate_profit_90d")
    roi_90d = metrics.get("keepa_estimate_roi_90d")
    profit_peak = metrics.get("keepa_estimate_profit_peak")
    roi_peak = metrics.get("keepa_estimate_roi_peak")

    if profit is None:
        profitability = "No cost price was supplied, so no profit/ROI/margin estimate is available."
    else:
        profitability = (
            f"At today's UK price: profit £{profit:.2f}, ROI {roi:.1f}%, margin {margin:.1f}% of sale price.\n"
            f"At the 90-day average UK price: profit £{profit_90d:.2f}, ROI {roi_90d:.1f}%.\n"
            f"At the 90-day PEAK UK price (riskier -- only real if stock is sold while the price is actually up "
            f"there, not guaranteed): profit £{profit_peak:.2f}, ROI {roi_peak:.1f}%."
        )

        # Real gap found live, 2026-09-07 (Tamara): the old one-window
        # (90d), two-bar (25%/17%) pairing made a lead whose margin only
        # opened up recently read identically to one that was never once
        # viable -- both "0 of 90". Now two genuinely different windows:
        # a tighter bar (25%) over a shorter, more-recent window (30
        # days -- "is this working right now") alongside a lower bar
        # (20%) over the full 90 days ("has this cleared a real margin
        # recently at all"). See VerdictService.compute_metrics' own
        # comment for the full reasoning.
        viable_days = metrics.get("viable_days_90d")
        if viable_days and viable_days.get("priced_days_90d"):
            profitability += (
                f"\nDay-by-day (not the average): in the last 30 days, "
                f"{viable_days['days_at_25pct_30d']} of {viable_days['priced_days_30d']} priced days were at "
                f"25%+ ROI (a strong, recent buying window). Over the full last 90 days, "
                f"{viable_days['days_at_20pct_90d']} of {viable_days['priced_days_90d']} priced days cleared "
                f"at least 20%+ ROI. Use this, not just the average, to judge how often a good buying window "
                f"actually occurs -- and whether it's a recent pattern or one that's faded."
            )

    amazon_pct = metrics.get("amazon_buy_box_percentage") or 0
    is_amazon = metrics.get("is_amazon_on_listing")

    if not is_amazon:
        amazon_line = "Amazon is not on this listing."
    elif amazon_pct > 0:
        amazon_line = f"Amazon is on this listing AND currently holds {amazon_pct:.0f}% of the buy box."
    else:
        amazon_line = "Amazon is on this listing but is NOT currently winning the buy box."

    # amazon_pct/is_amazon above are a CURRENT-MOMENT snapshot, which
    # misses a real recent pattern whenever Amazon happens to be between
    # stock right now (real gap found live, 2026-09-07, Tamara re:
    # B0CFV7Z7SJ). This reads the actual 30-day buy-box-seller history instead.
    bbh = metrics.get("buy_box_holder_30d")
    if bbh and bbh.get("amazon_minutes", 0) > 0:
        if not bbh.get("third_party_ever_won"):
            amazon_line += (
                " Over the real last 30 days of buy-box history, no third-party seller has ever "
                "won it -- Amazon holds it exclusively whenever in stock. This is a red flag for "
                "sourcing: hard to compete against Amazon itself on this listing."
            )
        else:
            amazon_line += " Over the real last 30 days, third-party sellers have also won the buy box at times."

    review_count = metrics.get("review_count") or 0
    rating = metrics.get("rating")
    reviews_line = f"{review_count} reviews" + (f", {rating} star rating" if rating else ", no star rating")

    return (
        f"PROFITABILITY\n{profitability}\n\n"
        "DEMAND & COMPETITION\n"
        f"Monthly sales (Amazon-confirmed): {metrics.get('monthly_sales') or 'not confirmed by Amazon'} "
        f"(sales-rank drops in last 30d as a proxy: {metrics.get('sales_drops_30d')})\n"
        f"Offers: {metrics.get('offers_now')} now, {metrics.get('offers_90d_avg')} avg over 90d, trend: {metrics.get('offer_trend')}\n"
        f"FBA offer present: {'yes' if metrics.get('offers_fba_present') else 'no'}\n"
        f"{amazon_line}\n"
        f"Reviews: {reviews_line}\n"
        f"Out of stock: {'yes' if metrics.get('is_out_of_stock') else 'no'}\n\n"
        "PRICE HISTORY / STABILITY\n"
        f"Current £{metrics.get('buy_box_now')}, 90d avg £{metrics.get('price_avg_90d')}, "
        f"90d peak £{metrics.get('price_max_90d')}, all-time low £{metrics.get('price_min_ever')}, "
        f"all-time high £{metrics.get('price_max')}\n"
        f"Buy-box concentration (top seller over the window, not necessarily Amazon): {metrics.get('buy_box_percentage')}%\n"
        f"Price drops: {metrics.get('price_drop_count_30d')} in last 30d, {metrics.get('price_drop_count_90d')} in last 90d"
    )


def _format_source_check_summary(metrics: dict) -> str | None:
    """
    Renders VerdictService.check_source_marketplace's result -- whether
    an EU A2A lead can actually be BOUGHT on its source marketplace --
    as its own labelled section. Returns None when no source check ran
    (a domestic OA lead, or one whose source marketplace couldn't be
    resolved), so generate_verdict simply omits the section rather than
    telling Claude "unknown" and inviting it to speculate.

    Spelled out in words rather than passed as raw flags for the same
    reason _format_metrics_summary exists: the distinction that matters
    here (Amazon out of stock = park and revisit, FBM-only = dead) is
    exactly the kind of thing a bare boolean loses.
    """
    check = metrics.get("source_check")

    if not check:
        return None

    marketplace = check.get("marketplace")
    lines = [f"SOURCE MARKETPLACE CHECK (Amazon {marketplace} -- where this lead would be bought)"]
    lines.append(check.get("note") or "")

    price = check.get("buy_box_price")
    if price:
        price_gbp = check.get("buy_box_price_gbp")
        gbp_part = f" (about GBP {price_gbp:.2f})" if price_gbp else ""
        lines.append(f"Current {marketplace} buy-box price: {price} {check.get('currency')}{gbp_part}")

    blocker = check.get("blocker")

    if blocker == "amazon_oos":
        # `note` above already states exactly this -- repeating it reads
        # as two separate findings rather than one.
        pass
    elif check.get("amazon_on_listing"):
        lines.append(
            f"Amazon {marketplace} sells this listing and is "
            f"{'IN stock' if check.get('amazon_in_stock') else 'OUT OF stock'} right now."
        )
    else:
        lines.append(f"Amazon {marketplace} does not sell this listing itself.")

    if check.get("offer_count_fba") is not None:
        lines.append(f"FBA offers on {marketplace}: {check['offer_count_fba']}")

    if blocker == "amazon_oos":
        lines.append(
            "VERDICT IMPACT: not buyable right now, but this is a RESTOCK case -- "
            "say so plainly so it can be parked and revisited, not written off."
        )
    elif blocker in ("fbm_only", "no_buy_box", "not_listed"):
        lines.append(
            "VERDICT IMPACT: not buyable at all -- this lead cannot be purchased "
            "in a way that produces a reclaimable Amazon VAT invoice."
        )
    elif blocker in ("unverified", "check_failed"):
        lines.append(
            "VERDICT IMPACT: buyability could NOT be confirmed -- treat as unverified, "
            "do not assume either way."
        )

    return "\n".join(line for line in lines if line)


def _format_deep_dive_summary(metrics: dict) -> str | None:
    """
    Renders VerdictService.compute_metrics' deep_dive=True fields
    (2026-08-23, sourcing-agent brief section 5) -- sp_api_live_check --
    as a labelled section, same "name the figure explicitly" reasoning
    as _format_metrics_summary. Returns None when this metrics dict
    wasn't a deep-dive pass at all (the common case -- most
    bulk-checked ASINs never get this far), so generate_verdict can
    skip the section entirely rather than print a block of "not
    available" noise on every ordinary verdict.

    Used to also cover competitor_stock_levels (per-seller Keepa stock)
    -- dropped 2026-08-26 along with the field itself, see
    VerdictService.compute_metrics' docstring for why.
    """
    sp_check = metrics.get("sp_api_live_check")

    if not metrics.get("deep_dive") or not sp_check:
        # deep_dive is only ever set True alongside a real sp_check --
        # see VerdictService.add_deep_dive -- but check both explicitly
        # rather than assume the invariant holds forever.
        return None

    price = sp_check.get("price")
    price_str = f"£{price:.2f}" if price is not None else "no live buyable offer found"
    lines = [
        f"Live price cross-check via SP-API (free, real-time, independent of Keepa's own possibly-stale "
        f"snapshot above): {price_str}, {sp_check.get('offer_count')} total offers, status "
        f"{sp_check.get('status')}."
    ]

    return (
        "DEEP DIVE -- this lead already looked promising on an initial pass, so extra evidence was "
        "pulled before this final verdict:\n" + "\n".join(lines)
    )


def _format_rejected_on(reviewed_at: str | None) -> str:
    """
    "26 Aug 2026" rather than the raw ISO stamp
    get_similar_rejections carries. How long ago a rejection was is
    the part that matters when judging whether its reason still holds
    against today's figures, and a bare timestamp with microseconds
    reads as noise.
    """
    if not reviewed_at:
        return "previously"

    try:
        return datetime.fromisoformat(reviewed_at).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return "previously"


def _format_rejection_history_summary(similar_rejections: list[dict] | None) -> str | None:
    """
    Renders VerdictService.get_similar_rejections' output (sourcing
    agent brief step 7) as a labelled section -- previous rejections
    of THIS EXACT ASIN, in the user's own words. Returns None when
    there's nothing to show (the common case today: Lead.decision_reason
    only started being captured 2026-08-23 and the review queue hasn't
    built up history yet), so generate_verdict can skip the section.

    Same-brand and same-category entries no longer reach here at all
    (2026-08-27 -- see get_similar_rejections for why they were bad
    signal). The match_reason branch is gone with them: every entry is
    this ASIN resurfacing, so the label can just say so.
    """
    if not similar_rejections:
        return None

    lines = [
        f"- rejected {_format_rejected_on(r.get('reviewed_at'))}: \"{r['decision_reason']}\""
        for r in similar_rejections
    ]

    return (
        "PAST REJECTIONS OF THIS EXACT ASIN -- the user has rejected this same product "
        "before, in their own words. Weigh whether the reason still applies to today's "
        "figures (a price or competition change can genuinely resolve it) rather than "
        "treating it as an automatic AVOID:\n"
        + "\n".join(lines)
    )


def _format_brand_gating_summary(brand_gating: dict | None) -> str | None:
    """
    Renders VerdictService.get_brand_gating's output -- Atlas being
    gated on the brand, which unlike per-ASIN economics genuinely does
    carry from one ASIN to another.

    Stated as a hard fact rather than as "past rejection" context,
    because that's what it is: a row on the user's own Gated Brands
    list, not an inference from something they once typed into a
    reject prompt. Returns None when the brand isn't gated, so the
    section is skipped.
    """
    if not brand_gating:
        return None

    scope = (
        f"in {brand_gating['category_name']}"
        if brand_gating.get("category_name") else "across all categories"
    )

    return (
        f"GATED BRAND -- Atlas is currently gated on {brand_gating['brand']} {scope}: "
        "Amazon approval to sell it has not been obtained, so this stock could not be "
        "listed today however good the numbers are. Say so plainly in the rationale and "
        "let it drive the verdict -- a strong-looking gated lead is at best a WATCH "
        "(worth tracking to judge whether pursuing ungating is worthwhile), never a BUY."
    )


def _load_criteria_doc() -> str | None:
    """
    Reads criteria.md fresh on every call -- the user's own editable
    buying criteria (hard floors already enforced in code, restated for
    transparency, plus judgment notes that don't reduce to a single
    number). Returns None if the file is missing or empty so
    generate_verdict can skip the section entirely.
    """
    try:
        text = CRITERIA_DOC_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def generate_verdict(
    metrics: dict, va_financials: dict | None = None, similar_rejections: list[dict] | None = None,
    brand_gating: dict | None = None,
) -> tuple[str, str]:
    """
    Calls Claude with the Keepa metric set (see VerdictService.compute_metrics)
    and, when present, the VA/SAS-verified profit figures as ground truth --
    Keepa is never asked to recompute or second-guess those (see Lead's
    docstring in app/database/models.py). Returns (verdict, rationale).

    Raises ValueError if Claude declines (safety refusal), AND the Gemini
    fallback below also fails/declines, or neither's response starts with
    a recognized verdict token -- callers should treat this the same as a
    Keepa-side analysis failure (see LeadAnalysisService.MAX_ANALYSIS_
    ATTEMPTS).

    Gemini fallback (2026-09-06, Tamara's own request after Atlas's own
    ANTHROPIC_API_KEY ran out of credits and every lead analysis started
    failing silently): if the Claude call fails for ANY reason -- out of
    credits, rate limited, a transient outage -- this falls back to
    Gemini's free tier (see _call_gemini_for_verdict) using the EXACT
    SAME prompt, so a lead still gets a real verdict instead of sitting
    unrated. The rationale is prefixed to say so plainly when this
    happens -- a Gemini-generated verdict should never look identical to
    a Claude one, since it's a different model's judgment on the same
    money-relevant question.
    """
    financials_block = (
        "VA/SAS-verified figures (ground truth -- treat as fact, do not "
        f"second-guess against the Keepa estimate below if one is present):\n{va_financials}\n"
        if va_financials else
        "No VA/SAS-verified profit figures for this lead -- rely on the "
        "Keepa-derived estimate below, if present, and flag clearly that "
        "it's an estimate, not a verified number.\n"
    )

    source_check_summary = _format_source_check_summary(metrics)
    source_check_block = f"\n{source_check_summary}\n" if source_check_summary else ""

    deep_dive_summary = _format_deep_dive_summary(metrics)
    deep_dive_block = f"\n{deep_dive_summary}\n" if deep_dive_summary else ""

    rejection_history_summary = _format_rejection_history_summary(similar_rejections)
    rejection_history_block = f"\n{rejection_history_summary}\n" if rejection_history_summary else ""

    brand_gating_summary = _format_brand_gating_summary(brand_gating)
    brand_gating_block = f"\n{brand_gating_summary}\n" if brand_gating_summary else ""

    criteria_doc = _load_criteria_doc()
    criteria_block = (
        f"\nUSER'S CRITERIA DOC (their own words, edited directly by them -- read carefully, "
        f"this is a more specific/authoritative source of judgment than generic reasoning):\n{criteria_doc}\n"
        if criteria_doc else ""
    )

    prompt = (
        "You are assessing an Amazon FBA sourcing lead (OA or A2A) for a "
        "reseller deciding whether to buy stock.\n\n"
        f"{criteria_block}"
        f"{financials_block}\n"
        f"Keepa-derived metrics:\n{_format_metrics_summary(metrics)}\n"
        f"{source_check_block}"
        f"{deep_dive_block}"
        f"{brand_gating_block}"
        f"{rejection_history_block}\n"
        "Ground every bullet in a specific figure above, and follow these "
        "rules exactly -- each one fixes a real mistake seen in a previous "
        "verdict:\n"
        "1. Never call a lead 'not profitable' (or similar) if the profit "
        "figure above is positive -- state the actual profit/ROI/margin "
        "instead of a vague judgment.\n"
        "2. Don't state a percentage and then contradict it with a vague "
        "qualifier (e.g. don't say a margin 'isn't that high' right after "
        "giving a specific number) -- state the figure and, if useful, "
        "compare it to a concrete reference point instead: 17% ROI is the "
        "bare minimum this app treats as viable at all -- a lead also needs "
        "13%+ margin (profit as a % of sale price, NOT the same number as "
        "ROI), £2+ absolute profit per unit, and a £10+ sale price (a "
        "genuinely cheap item doesn't leave enough absolute headroom for "
        "fees to be worth sourcing, however good its ROI/margin numbers "
        "look); all four together, not ROI alone. A sub-£10 sale price is "
        "an automatic AVOID regardless of how strong every other figure "
        "is -- state that plainly rather than treating it as just one "
        "caution among several. 25%+ ROI is what it treats as a strong "
        "lead. Report the actual margin % and profit £ plainly against "
        "those floors rather than editorializing.\n"
        "3. Judge Amazon's presence by its buy-box SHARE, not by whether "
        "it's merely present. Amazon being on a listing is not itself a "
        "reason to mark a lead down -- only call it out as a competitive "
        "risk when it currently holds a high share of the buy box (the "
        "summary above states this explicitly when true).\n"
        "4. State the review count exactly as given -- never say 'no "
        "reviews' unless the review count above is genuinely 0.\n"
        "5. When a DEEP DIVE section is present above, weigh it as real "
        "extra evidence, not a footnote -- if the SP-API live "
        "cross-check disagrees materially with Keepa's snapshot price "
        "above, flag that explicitly rather than silently picking one.\n"
        "6. When a PAST REJECTIONS section is present above, treat it as "
        "the user's own stated preferences, not a rule to apply blindly -- "
        "if THIS EXACT ASIN was rejected before, say so plainly and weigh "
        "that reason heavily unless something concrete has genuinely "
        "changed since (price, stock, competition). A same-brand or "
        "same-category rejection is softer evidence of a pattern worth "
        "naming, not an automatic AVOID on its own.\n"
        "7. When a SOURCE MARKETPLACE CHECK section is present above, it "
        "overrides the profitability figures: this reseller can only buy an "
        "EU A2A lead from Amazon itself or from an FBA seller, because only "
        "those come with a reclaimable Amazon VAT invoice. If that section "
        "says the lead is not buyable, the verdict is AVOID no matter how "
        "good the ROI is -- and say WHICH it is, because they mean different "
        "things: Amazon being out of stock is a restock/park case, an "
        "FBM-only buy box is dead. If it says buyability could not be "
        "confirmed, say so as a caution rather than assuming either way.\n\n"
        "Reply using EXACTLY this format -- the first line must be one of "
        "these three words and nothing else: BUY, WATCH, or AVOID (not a "
        "synonym like 'Pass' or 'Skip', not punctuation, not a sentence -- "
        "just that one word). Then 3-5 short bullet lines, each stating ONE "
        "concrete reason grounded in a specific figure above. No "
        "paragraphs, no extra commentary before or after the bullets.\n\n"
        "Mark each bullet's OWN polarity, not the overall verdict -- a "
        "WATCH or AVOID can still have a genuinely positive bullet (e.g. "
        "strong sales), and a BUY can still carry a real caveat worth "
        "flagging. Every bullet must start with exactly one of these two "
        "markers, nothing else:\n"
        "- '+ ' when the bullet is a factor SUPPORTING the lead "
        "(profitable, in demand, low competition, stable price, etc).\n"
        "- '! ' when the bullet is a RISK or CAUTION (thin margin, heavy "
        "competition, Amazon dominant, volatile price, unconfirmed sales, "
        "etc).\n\n"
        "Example shape (write your own content, don't reuse this wording):\n"
        "WATCH\n"
        "! Profit is thin at 4% ROI, leaving little room for fee drift\n"
        "! Amazon holds 68% of the buy box, adding real competition\n"
        "+ Sales rank drops suggest steady demand"
    )

    try:
        text = _call_claude_for_verdict(prompt)
        provider_note = None
    except Exception as exc:
        print(f"Claude verdict call failed, falling back to Gemini: {exc}")
        text = _call_gemini_for_verdict(prompt)
        provider_note = "[Generated via Gemini fallback -- Claude was unavailable]"

    verdict, rationale = _parse_verdict_response(text)

    if provider_note:
        rationale = f"{provider_note}\n{rationale}"

    return verdict, rationale


def _call_claude_for_verdict(prompt: str) -> str:
    client = get_anthropic_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to generate a verdict for this lead")

    return "".join(block.text for block in response.content if block.type == "text").strip()


def _call_gemini_for_verdict(prompt: str) -> str:
    """
    Same prompt, Gemini's free tier instead of Claude -- see generate_
    verdict's own docstring for when/why this is used. Raises on any
    failure (including Gemini itself being unavailable/out of quota),
    same as the Claude path -- callers already treat any exception here
    as "this lead couldn't be analyzed right now", no separate handling
    needed for a double failure.
    """
    client = get_gemini_client()
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)

    text = (response.text or "").strip()
    if not text:
        raise ValueError("Gemini returned an empty response (possibly a safety block)")

    return text


def _parse_verdict_response(text: str) -> tuple[str, str]:
    first_line, _, rest = text.partition("\n")
    verdict = first_line.strip().upper()

    if verdict not in VERDICT_VALUES:
        verdict = VERDICT_SYNONYMS.get(verdict, verdict)

    if verdict not in VERDICT_VALUES:
        raise ValueError(f"Model did not return a recognized verdict: {text[:200]!r}")

    return verdict, _ensure_bulleted(rest.strip())


def _ensure_bulleted(rationale: str) -> str:
    """
    The prompt asks for '+ '/'! ' polarity-marked bullet lines (see
    generate_verdict), but Claude occasionally answers in prose anyway --
    fall back to splitting on sentence boundaries so the stored rationale
    is still bullet-formatted for the UI (see _verdict_metrics.html),
    rather than one dense paragraph.

    Fallback bullets deliberately get a plain '- ' prefix, not a '+'/'!'
    guess -- there's no reliable way to infer a sentence's polarity here,
    and _verdict_metrics.html already renders a '-'-prefixed (or
    unprefixed) bullet as neutral, same as it does for rationale saved
    before polarity markers existed.
    """
    if any(line.strip().startswith(("+", "!")) for line in rationale.split("\n")):
        return rationale

    sentences = [s.strip() for s in rationale.replace("\n", " ").split(". ")]
    sentences = [s if s.endswith((".", "!", "?")) else f"{s}." for s in sentences if s]

    return "\n".join(f"- {s}" for s in sentences)


def propose_criteria_amendments(rejections: list[dict]) -> list[str]:
    """
    Sourcing agent brief step 8, part 2 -- the /criteria/review page's
    on-demand analysis. Takes VerdictService.get_all_reasoned_rejections'
    output and asks Claude to name genuinely repeatable patterns worth
    adding to criteria.md's Judgment notes section, as plain one-line
    rules in the same voice a human would write there. Returns each
    proposed line as its own list entry -- purely a proposal, nothing is
    written to the file here; the route only appends whatever the user
    explicitly approves on that page.

    Returns [] if there's nothing worth proposing (too little history,
    or Claude genuinely finds no repeatable pattern) -- callers should
    treat an empty list as "no suggestions", not an error.
    """
    if not rejections:
        return []

    client = get_anthropic_client()

    lines = []
    for r in rejections:
        brand = r.get("brand") or "unknown brand"
        category = r.get("category_name") or "unknown category"
        lines.append(f"- ASIN {r['asin']} ({brand}, {category}): \"{r['decision_reason']}\"")

    prompt = (
        "Below are Amazon FBA sourcing leads a reseller has rejected, each with their own "
        "free-text reason. Find genuinely REPEATABLE patterns -- something that shows up "
        "across multiple rejections, or one clear, strongly-stated dealbreaker -- that would "
        "be worth adding as a standing rule to their buying-criteria doc. A single one-off "
        "reason that doesn't generalize is NOT a pattern; don't propose one for it.\n\n"
        f"Rejections:\n" + "\n".join(lines) + "\n\n"
        "Reply with EITHER the single word NONE (nothing else) if you don't find a genuine "
        "repeatable pattern, OR 1-5 bullet lines, each starting with '- ', stating ONE proposed "
        "rule in plain language a VA could follow -- the way a human would write a note to "
        "themselves, not a summary of the data. No other text before, between, or after the "
        "bullets.\n\n"
        "Example shape (write your own content, don't reuse this wording):\n"
        "- Avoid seasonal/Christmas-only product lines -- margin never survives past December\n"
        "- Treat single-seller-dominated listings (one seller >80% buy box) as a caution, not "
        "just a number"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to propose criteria amendments")

    text = "".join(block.text for block in response.content if block.type == "text").strip()

    if text.upper() == "NONE":
        return []

    return [line.strip()[2:].strip() for line in text.split("\n") if line.strip().startswith("- ")]
