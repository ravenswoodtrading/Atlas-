import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# -> Atlas/.env, same location app/keepa/client.py loads KEEPA_API_KEY from.
env_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(env_path)

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


def generate_verdict(metrics: dict, va_financials: dict | None = None) -> tuple[str, str]:
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

    prompt = (
        "You are assessing an Amazon FBA sourcing lead (OA or A2A) for a "
        "reseller deciding whether to buy stock.\n\n"
        f"{financials_block}\n"
        f"Keepa-derived metrics:\n{_format_metrics_summary(metrics)}\n"
        f"{deep_dive_block}\n"
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
        "explicitly rather than silently picking one.\n\n"
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
