import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# -> Atlas/.env, same location app/keepa/client.py loads KEEPA_API_KEY from.
env_path = Path(__file__).resolve().parents[2] / ".env"
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

        viable_days = metrics.get("viable_days_90d")
        if viable_days and viable_days.get("priced_days"):
            profitability += (
                f"\nDay-by-day over the actual last 90 days (not the average): "
                f"{viable_days['days_at_target_roi']} of {viable_days['priced_days']} priced days were at "
                f"25%+ ROI (strong), {viable_days['days_at_min_roi']} of {viable_days['priced_days']} were "
                f"at least 17%+ ROI (bare minimum). Use this, not just the average, to judge how often a "
                f"good buying window actually occurs."
            )

    amazon_pct = metrics.get("amazon_buy_box_percentage") or 0
    is_amazon = metrics.get("is_amazon_on_listing")

    if not is_amazon:
        amazon_line = "Amazon is not on this listing."
    elif amazon_pct > 0:
        amazon_line = f"Amazon is on this listing AND currently holds {amazon_pct:.0f}% of the buy box."
    else:
        amazon_line = "Amazon is on this listing but is NOT currently winning the buy box."

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


def _format_deep_dive_summary(metrics: dict) -> str | None:
    """
    Renders VerdictService.compute_metrics' deep_dive=True fields
    (2026-08-23, sourcing-agent brief section 5) -- competitor_stock_levels
    and sp_api_live_check -- as a labelled section, same "name the
    figure explicitly" reasoning as _format_metrics_summary. Returns
    None when this metrics dict wasn't a deep-dive pass at all (the
    common case -- most bulk-checked ASINs never get this far), so
    generate_verdict can skip the section entirely rather than print a
    block of "not available" noise on every ordinary verdict.
    """
    if not metrics.get("deep_dive"):
        return None

    lines = []

    stock_levels = metrics.get("competitor_stock_levels")
    if stock_levels:
        parts = []
        for s in stock_levels[:8]:
            qty = "10+ (Keepa's own cap -- real number could be higher)" if s["stock"] >= 10 else f"{s['stock']}"
            who = "Amazon itself" if s["is_amazon"] else ("a competing FBA seller" if s["is_fba"] else "a competing non-FBA seller")
            parts.append(f"{who}: {qty} units")
        lines.append("Competitor stock levels, highest first: " + "; ".join(parts) + ".")
    else:
        lines.append("Competitor stock levels: no stock data available from Keepa for this listing right now.")

    sp_check = metrics.get("sp_api_live_check")
    if sp_check:
        price = sp_check.get("price")
        price_str = f"£{price:.2f}" if price is not None else "no live buyable offer found"
        lines.append(
            f"Live price cross-check via SP-API (free, real-time, independent of Keepa's own possibly-stale "
            f"snapshot above): {price_str}, {sp_check.get('offer_count')} total offers, status "
            f"{sp_check.get('status')}."
        )
    else:
        lines.append("Live SP-API price cross-check: not available (not configured, or the live call failed).")

    return (
        "DEEP DIVE -- this lead already looked promising on an initial pass, so extra evidence was "
        "pulled before this final verdict:\n" + "\n".join(lines)
    )


def _format_rejection_history_summary(similar_rejections: list[dict] | None) -> str | None:
    """
    Renders VerdictService.get_similar_rejections' output (sourcing
    agent brief step 7) as a labelled section -- related leads the
    user has already rejected, in their own words. Returns None when
    there's nothing to show (the common case today: Lead.decision_reason
    only started being captured 2026-08-23 and the review queue hasn't
    built up history yet), so generate_verdict can skip the section.
    """
    if not similar_rejections:
        return None

    lines = []
    for r in similar_rejections:
        if r["match_reason"] == "same_asin":
            tag = "THIS EXACT ASIN was rejected before"
        elif r["match_reason"] == "same_brand":
            tag = f"same brand ({r['brand']})"
        else:
            tag = f"same category ({r['category_name']})"
        lines.append(f"- {tag}, ASIN {r['asin']}: \"{r['decision_reason']}\"")

    return (
        "PAST REJECTIONS -- the user has previously rejected these related leads, in their own words:\n"
        + "\n".join(lines)
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
) -> tuple[str, str]:
    """
    Calls Claude with the Keepa metric set (see VerdictService.compute_metrics)
    and, when present, the VA/SAS-verified profit figures as ground truth --
    Keepa is never asked to recompute or second-guess those (see Lead's
    docstring in app/database/models.py). Returns (verdict, rationale).

    Raises ValueError if Claude declines (safety refusal) or its response
    doesn't start with a recognized verdict token -- callers should treat
    this the same as a Keepa-side analysis failure (see
    LeadAnalysisService.MAX_ANALYSIS_ATTEMPTS).
    """
    client = get_anthropic_client()

    financials_block = (
        "VA/SAS-verified figures (ground truth -- treat as fact, do not "
        f"second-guess against the Keepa estimate below if one is present):\n{va_financials}\n"
        if va_financials else
        "No VA/SAS-verified profit figures for this lead -- rely on the "
        "Keepa-derived estimate below, if present, and flag clearly that "
        "it's an estimate, not a verified number.\n"
    )

    deep_dive_summary = _format_deep_dive_summary(metrics)
    deep_dive_block = f"\n{deep_dive_summary}\n" if deep_dive_summary else ""

    rejection_history_summary = _format_rejection_history_summary(similar_rejections)
    rejection_history_block = f"\n{rejection_history_summary}\n" if rejection_history_summary else ""

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
        f"{deep_dive_block}"
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
        "ROI) and £2+ absolute profit per unit; all three together, not "
        "ROI alone. 25%+ ROI is what it treats as a strong lead. Report "
        "the actual margin % and profit £ plainly against those floors "
        "rather than editorializing.\n"
        "3. Judge Amazon's presence by its buy-box SHARE, not by whether "
        "it's merely present. Amazon being on a listing is not itself a "
        "reason to mark a lead down -- only call it out as a competitive "
        "risk when it currently holds a high share of the buy box (the "
        "summary above states this explicitly when true).\n"
        "4. State the review count exactly as given -- never say 'no "
        "reviews' unless the review count above is genuinely 0.\n"
        "5. When a DEEP DIVE section is present above, weigh it as real "
        "extra evidence, not a footnote -- high stock among competing "
        "FBA sellers is a genuine caution (they can sustain undercutting "
        "for longer), while low/no competing stock is a genuine "
        "supporting signal. If the SP-API live cross-check disagrees "
        "materially with Keepa's snapshot price above, flag that "
        "explicitly rather than silently picking one.\n"
        "6. When a PAST REJECTIONS section is present above, treat it as "
        "the user's own stated preferences, not a rule to apply blindly -- "
        "if THIS EXACT ASIN was rejected before, say so plainly and weigh "
        "that reason heavily unless something concrete has genuinely "
        "changed since (price, stock, competition). A same-brand or "
        "same-category rejection is softer evidence of a pattern worth "
        "naming, not an automatic AVOID on its own.\n\n"
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

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to generate a verdict for this lead")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    first_line, _, rest = text.partition("\n")
    verdict = first_line.strip().upper()

    if verdict not in VERDICT_VALUES:
        verdict = VERDICT_SYNONYMS.get(verdict, verdict)

    if verdict not in VERDICT_VALUES:
        raise ValueError(f"Claude did not return a recognized verdict: {text[:200]!r}")

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
