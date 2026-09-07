"""
Opportunity Engine 2.0 -- Value / Evidence / Risk / Action.

Pure functions, no DB access, no Keepa/SP-API call -- takes the SAME
per-source lead dict ReviewQueueService._scan_lead_dict/
_competitor_lead_dict already build (profit/roi/roi_90d/monthly_sales/
sales_drops_30d/recommendation/parsed_report/freshness) and answers
four SEPARATE questions instead of one blended score:

  VALUE     "How attractive is this financially?"
  EVIDENCE  "How much do we trust the sales/recurrence data backing it?"
  RISK      "What could cause the economics to deteriorate?"
  ACTION    "Given those three, what should I actually do?"

Every threshold below is one that ALREADY existed elsewhere in this
codebase before this module -- none invented here (see each constant's
own comment for its source). The only genuinely NEW thing is the
COMBINATORIAL rule connecting them (see compute()'s own docstring),
not a new number.

CRITICAL DATA PRINCIPLE (carried over from the Opportunity Engine 2.0
design phase): missing sales evidence is never treated as evidence of
POOR demand -- see evidence_tier's own docstring. A RAM/PC-component
ASIN with monthly_sales=0 reads as "no evidence available", never as
"confirmed doesn't sell".
"""
# classify_offer_freshness's own STALE/UNAVAILABLE states (review_queue_
# service.STALE_FRESHNESS_STATES) -- inlined as a literal rather than
# imported from that module, since review_queue_service imports FROM
# this module (it calls compute() from _item_views) and Python doesn't
# allow the reverse import too. Same two values, unchanged.
STALE_FRESHNESS_STATES = {"STALE", "UNAVAILABLE"}

# ---- reused constants (all pre-existing elsewhere -- see comments) ----

# OpportunityEngine.MIN_VIABLE_ROI -- the hard floor a product must
# already have cleared (today or 90d) before it can be recommended at
# all. Anything reaching this module already cleared it (see
# ReviewQueueService.list_leads' own review_filter passes), so this is
# only used here for VALUE tiering, not as a fresh gate.
MIN_VIABLE_ROI = 17

# ScoringEngine._roi_factor's own "Strong ROI" / ScoringEngine.
# _profit_factor's own "Solid profit"/"Excellent profit" tiers.
STRONG_ROI = 35
SOLID_PROFIT = 8
EXCELLENT_PROFIT = 15

# ProductRepository.is_notable's own ROI bar.
IS_NOTABLE_ROI = 25

# ProductRepository.is_notable's own SALES_DROPS_NOTABLE_THRESHOLD.
SALES_DROPS_NOTABLE_THRESHOLD = 3

# ScoringEngine.NO_SALES_DATA_STRONG_RANK_DROPS / ScoringEngine's own
# "Good confirmed sales (20+/month)" tier.
STRONG_RANK_DROPS = 15
STRONG_MONTHLY_SALES = 20

# These WERE ConfidenceEngine's own price-swing/competition-surge
# trigger points (see that module's own comment for why they moved
# here, relabelled as RISK, not confidence -- Opportunity Engine 2.0
# point 3). Values unchanged.
PRICE_SWING_RISK_PCT = 15
COMPETITION_SURGE_RISK_PCT = 50

# The empirical cutoff identified in the Opportunity Engine 2.0
# simulation (and already live in RevisitPoolService.
# VERIFY_SOURCE_MATCH_ROI, Phase 3A) -- source-match/price-parsing
# errors concentrated almost exclusively above this ROI band in real
# data (a near-zero EU cost against a normal UK price -- the same
# "phantom cost" signature KeepaParser's own docstring documents
# fixing elsewhere).
VERIFY_SOURCE_MATCH_ROI = 500

# ---- Action vocabulary ----
ACTION_BUY_NOW = "BUY_NOW"
ACTION_PRICE_DROP_BUY_NOW = "PRICE_DROP_BUY_NOW"
ACTION_BUY_WITH_CAUTION = "BUY_WITH_CAUTION"
ACTION_HIGH_VALUE_LOW_CONFIDENCE = "HIGH_VALUE_LOW_CONFIDENCE"
ACTION_INVESTIGATE = "INVESTIGATE"
ACTION_WATCH = "WATCH"
ACTION_HISTORICAL_RECURRING = "HISTORICAL_RECURRING"
ACTION_BLOCKED = "BLOCKED"

ACTION_LABELS = {
    ACTION_BUY_NOW: "BUY NOW",
    ACTION_PRICE_DROP_BUY_NOW: "PRICE DROP — BUY NOW",
    ACTION_BUY_WITH_CAUTION: "BUY WITH CAUTION",
    ACTION_HIGH_VALUE_LOW_CONFIDENCE: "HIGH VALUE — LOW CONFIDENCE",
    ACTION_INVESTIGATE: "INVESTIGATE",
    ACTION_WATCH: "WATCH",
    ACTION_HISTORICAL_RECURRING: "HISTORICAL / RECURRING",
    ACTION_BLOCKED: "BLOCKED",
}

# Short "what this means" line for each action -- shown under the badge
# in the detail panel (WHAT SHOULD I DO section).
ACTION_DESCRIPTIONS = {
    ACTION_BUY_NOW: "Strong economics, solid evidence, no active risk flags.",
    ACTION_PRICE_DROP_BUY_NOW: "Strong economics -- the UK price has dropped, but the source opportunity is real. The drop is a risk flag, not a reason to wait.",
    ACTION_BUY_WITH_CAUTION: "Attractive economics with a real risk flag worth reading before you buy.",
    ACTION_HIGH_VALUE_LOW_CONFIDENCE: "Exceptional economics, but the evidence behind them isn't strong enough yet to fully trust. Worth investigating, not dismissing.",
    ACTION_INVESTIGATE: "Potentially attractive, but there isn't enough sales evidence to say more without a closer look.",
    ACTION_WATCH: "Modest opportunity, not currently a priority.",
    ACTION_HISTORICAL_RECURRING: "Was a real, evidenced opportunity -- not currently verified as buyable right now.",
    ACTION_BLOCKED: "Gated brand -- can't currently be sold regardless of the numbers.",
}

VALUE_TIERS = ("WEAK", "MODERATE", "STRONG", "EXCEPTIONAL")
EVIDENCE_TIERS = ("INSUFFICIENT", "MODERATE", "STRONG")

# Same 4-marketplace set already defined independently in
# eu_a2a_freshness_service.py -- duplicated here (not imported) since
# that module is a scheduler entry point, not a shared constants
# module, and this is a single stable literal.
EU_MARKETPLACES = ("DE", "FR", "ES", "IT")


def value_tier(profit: float, roi: float) -> str:
    """
    profit/roi: whichever of today's/90-day-typical is BETTER (same
    "effective" convention OpportunityEngine itself already uses for
    its own recommendation floor) -- used for TIERING only. The
    display layer (build_value below) always shows today AND 90d
    separately regardless (Opportunity Engine 2.0 point 7).
    """
    if roi >= 100 or profit >= EXCELLENT_PROFIT * 4:
        return "EXCEPTIONAL"
    if roi >= STRONG_ROI or profit >= EXCELLENT_PROFIT:
        return "STRONG"
    if roi >= MIN_VIABLE_ROI or profit >= SOLID_PROFIT:
        return "MODERATE"
    return "WEAK"


def evidence_tier(monthly_sales: int, sales_drops_30d: int) -> str:
    """
    "INSUFFICIENT" here means "no evidence available", NEVER "confirmed
    poor demand" -- Keepa returns the same 0 for both a genuinely
    unsold product and one it simply has no rank-drop history for
    (this is a real Keepa data-source limitation, not something this
    function can see past -- see the Opportunity Engine 2.0 design
    report's own "critical data principle").
    """
    if monthly_sales >= STRONG_MONTHLY_SALES or sales_drops_30d >= STRONG_RANK_DROPS:
        return "STRONG"
    if monthly_sales > 0 or sales_drops_30d >= SALES_DROPS_NOTABLE_THRESHOLD:
        return "MODERATE"
    return "INSUFFICIENT"


def _sales_evidence_fact(monthly_sales: int, sales_drops_30d: int) -> str:
    if monthly_sales > 0:
        return f"Confirmed sales ({monthly_sales}/month)"
    if sales_drops_30d >= SALES_DROPS_NOTABLE_THRESHOLD:
        return f"No confirmed sales figure -- estimated from {sales_drops_30d} rank drops in 30d"
    if sales_drops_30d > 0:
        return f"Thin evidence -- only {sales_drops_30d} rank drop(s) in 30d, below the {SALES_DROPS_NOTABLE_THRESHOLD}-drop bar"
    return "No sales evidence available -- Keepa has no confirmed sales figure or rank-drop history for this ASIN (not the same as confirmed low demand)"


def _freshness_fact(freshness: str) -> str:
    return {
        "FRESH": "Source checked recently",
        "AGING": "Source checked recently, approaching due for a recheck",
        "STALE": "Source not rechecked recently",
        "UNAVAILABLE": "Source confirmed unavailable at last check",
        "UNKNOWN": "Source freshness unknown",
    }.get(freshness, "Source freshness unknown")


MATCH_TIER_LABELS = {
    "ean": "EAN match", "brand_mpn": "Brand+MPN match", "mpn": "MPN match",
    "brand_title": "Brand+Title match", "title": "Title-only match (never treat as confirmed)",
}


def _source_confidence_fact(best_source_marketplace: str, match_tier: str, match_confidence_pct) -> tuple:
    """
    Returns (fact_text, is_unconfirmed_oa). EU A2A sources (DE/FR/ES/
    IT) are Keepa-confirmed live Amazon prices -- no match-tier concept
    applies, always trusted at face value. A UK-OA (or any non-A2A)
    source is only as trustworthy as its OA Source Discovery match tier
    (see help.html's own glossary: EAN > Brand+MPN > MPN > Brand+Title >
    Title-only) -- an EMPTY match_tier on a non-A2A source means no
    retailer listing was ever actually confirmed to match this ASIN,
    which is real, additional evidence-quality information distinct
    from sales evidence or freshness (Opportunity Engine 2.0 point 2:
    Evidence = "sales evidence, recurrence, SOURCE CONFIDENCE,
    freshness").
    """
    if not best_source_marketplace:
        return "No source identified", False

    if best_source_marketplace in EU_MARKETPLACES:
        return f"EU A2A source ({best_source_marketplace}) — confirmed live Amazon price", False

    if match_tier:
        pct = f" ({match_confidence_pct}%)" if match_confidence_pct else ""
        return f"OA source match: {MATCH_TIER_LABELS.get(match_tier, match_tier)}{pct}", False

    return (
        f"No confirmed source match for this {best_source_marketplace} figure — "
        f"no retailer listing has been verified yet (use Source Finder/OA Lookup to check)",
        True,
    )


def compute(lead: dict) -> dict:
    """
    lead: a per-source dict from ReviewQueueService._scan_lead_dict/
    _competitor_lead_dict (or an equivalent shape carrying the same
    keys) -- NOT a VA/Lead-sourced dict, which keeps its own separate
    BUY/WATCH/AVOID vocabulary in ReviewQueueService._item_views (that
    vocabulary is untouched by this module).

    THE COMBINATORIAL RULE (the one genuinely new piece of logic here,
    not a new number): a "notable" item (real recommendation=="BUY", or
    ROI>IS_NOTABLE_ROI with evidence that isn't INSUFFICIENT -- the
    SAME bar ProductRepository.is_notable already uses, just no longer
    excluding by recommendation label -- see that method's own comment
    on why LOW_CONFIDENCE/LOW_SCORE used to be excluded there) is only
    trusted enough to call BUY_NOW/PRICE_DROP_BUY_NOW/BUY_WITH_CAUTION
    when EITHER there's no active risk flag, OR the evidence backing it
    is fully STRONG (not just MODERATE). A risk flag stacked on top of
    only-moderate evidence reads as "not confident enough to say buy
    despite this" rather than "buy with a caution flag" -- it becomes
    HIGH_VALUE_LOW_CONFIDENCE instead, surfaced prominently, never
    hidden (Opportunity Engine 2.0's own core principle: risk must not
    hide a high-value opportunity, but it also must not be waved past
    on thin evidence).

    Freshness stays a HARD gate (Opportunity Engine 2.0 points 1/4) --
    but ONLY for the three "act now" states (BUY_NOW/PRICE_DROP_BUY_NOW/
    BUY_WITH_CAUTION), matching the exact scope the current production
    freshness gate already has (see STALE_FRESHNESS_STATES' own use in
    the OLD _item_views, which this replaces) -- HIGH_VALUE_LOW_
    CONFIDENCE/INVESTIGATE/WATCH were never freshness-gated before and
    still aren't now, since staleness isn't what makes them low-trust.
    """
    recommendation = lead.get("recommendation") or ""
    monthly_sales = lead.get("monthly_sales") or 0
    sales_drops_30d = lead.get("sales_drops_30d") or 0
    today_profit = lead.get("profit") or 0.0
    today_roi = lead.get("roi") or 0.0
    profit_90d = lead.get("profit_90d") or 0.0
    roi_90d = lead.get("roi_90d") or 0.0
    eff_profit = max(today_profit, profit_90d)
    eff_roi = max(today_roi, roi_90d)
    freshness = lead.get("freshness")

    parsed_report = lead.get("parsed_report") or {}
    trend = parsed_report.get("trend") or {}
    price_change = trend.get("price_change")
    offer_change = trend.get("offer_change")
    peak_roi = parsed_report.get("peak_roi") or 0
    peak_profit = parsed_report.get("peak_profit") or 0
    score_breakdown = parsed_report.get("score_breakdown") or []
    hazmat = any(f.get("label") == "Hazmat penalty" and f.get("passed") for f in score_breakdown)
    adult = any(f.get("label") == "Adult product penalty" and f.get("passed") for f in score_breakdown)

    best_source_marketplace = lead.get("best_source_marketplace") or ""
    match_tier = lead.get("match_tier") or ""
    match_confidence_pct = lead.get("match_confidence_pct") or 0
    source_fact, source_unconfirmed = _source_confidence_fact(best_source_marketplace, match_tier, match_confidence_pct)

    price_falling = price_change is not None and price_change < -PRICE_SWING_RISK_PCT
    price_rising = price_change is not None and price_change > PRICE_SWING_RISK_PCT
    competition_elevated = offer_change is not None and offer_change > COMPETITION_SURGE_RISK_PCT
    # An unconfirmed OA source is a genuine source-integrity risk, same
    # category as price/competition risk (Opportunity Engine 2.0 point
    # 2's "source-match anomalies") -- it also tightens the notable/
    # STRONG-evidence bar below via evidence_tier the same way any
    # other risk flag does.
    has_risk = price_falling or competition_elevated or source_unconfirmed

    # Real day-by-day recurrence evidence for a price-falling flag --
    # "how many of the last 90 days would this have cleared 25% ROI"
    # (Tamara's own ask, 2026-09-04), reusing FeeEngine.OA_TARGET_ROI_PCT,
    # the same bar "notable" already uses -- see Product.
    # days_at_25pct_roi_90d's own comment. None (not 0) for any
    # ProductRecord scanned BEFORE this field existed -- report_json
    # simply won't have the key, and that must read as "not computed
    # yet", never as a confirmed zero-recurrence result.
    days_at_25pct = parsed_report.get("days_at_25pct_roi_90d")
    priced_days = parsed_report.get("priced_days_90d")
    recurrence_known = days_at_25pct is not None and priced_days is not None and priced_days > 0

    # "The last time price dropped, were offers rising at the same
    # time" (Tamara's own follow-up, 2026-09-04) -- see
    # SourcingClassifier.compute_price_drop_offer_context. None when no
    # dip was found in the 90-day window at all (never happened), NOT
    # when it's simply not computed -- both read the same way here
    # (nothing shown) since there's nothing informative to say either
    # way, but the underlying data distinguishes them (see that
    # function's own docstring) for anyone reading report_json directly.
    dip_days_ago = parsed_report.get("days_since_last_price_dip")
    dip_offers_change = parsed_report.get("offers_change_at_last_price_dip_pct")
    dip_context_known = dip_days_ago is not None and dip_offers_change is not None

    # "The last time offers spiked, what did price actually do
    # afterward" (Tamara's own ask, 2026-09-04) -- see
    # SourcingClassifier.compute_competition_spike_evidence.
    spike_days_ago = parsed_report.get("days_since_last_competition_spike")
    spike_price_change = parsed_report.get("price_change_since_competition_spike_pct")
    spike_context_known = spike_days_ago is not None and spike_price_change is not None

    # ---- risk flags (built once, shown regardless of which action wins) ----
    # Each entry: {"label": headline, "level": "info"/"warn"/"high",
    # "detail": optional secondary line} -- kept as TWO separate strings
    # (2026-09-04, UI redesign) rather than one concatenated sentence, so
    # the template can render a short, scannable headline with the
    # elaboration underneath in smaller/muted text instead of a run-on
    # paragraph pretending to be a bullet. The day-by-day 25%-ROI
    # recurrence count is DELIBERATELY not repeated here any more -- it
    # gets its own standalone box (see risk["days_at_25pct_roi_90d"]
    # below and the template), not buried inside this flag's text.
    risk_flags = []
    if source_unconfirmed:
        risk_flags.append({"label": "No confirmed source match — verify before buying", "level": "warn", "detail": None})
    if price_falling:
        detail = None
        if dip_context_known:
            if dip_offers_change > COMPETITION_SURGE_RISK_PCT:
                detail = f"Last dip ({dip_days_ago}d ago) coincided with offers {dip_offers_change:+.0f}% above average — looks competition-driven"
            else:
                detail = f"Last dip ({dip_days_ago}d ago) had offers roughly normal ({dip_offers_change:+.0f}% vs average) — doesn't look competition-driven"
        risk_flags.append({
            "label": f"UK price falling ({price_change:+.0f}% vs 90d typical)", "level": "warn", "detail": detail,
        })
    elif price_rising:
        risk_flags.append({"label": f"UK price rising ({price_change:+.0f}% vs 90d typical)", "level": "info", "detail": None})
    if competition_elevated:
        detail = None
        if spike_context_known:
            detail = f"After the last spike ({spike_days_ago}d ago), price moved {spike_price_change:+.0f}% since"
        risk_flags.append({
            "label": f"Competition surging ({offer_change:+.0f}% more offers)", "level": "warn", "detail": detail,
        })
    best_roi_seen = max(eff_roi, peak_roi)
    verify_source_match = best_roi_seen > VERIFY_SOURCE_MATCH_ROI
    if verify_source_match:
        risk_flags.append({
            "label": f"Verify source match — ROI of {best_roi_seen:.0f}% may reflect a data or matching error",
            "level": "high", "detail": None,
        })
    if hazmat:
        risk_flags.append({"label": "Hazmat product", "level": "high", "detail": None})
    if adult:
        risk_flags.append({"label": "Restricted (18+) product", "level": "high", "detail": None})

    # Sales evidence sits in Risk only when it's LESS than STRONG -- a
    # STRONG evidence tier is a positive (see good_facts below), not a
    # risk. MODERATE/INSUFFICIENT both belong here, since either way
    # it's real uncertainty worth a human's attention -- but the wording
    # stays honest per this module's own "critical data principle":
    # INSUFFICIENT never reads as "confirmed doesn't sell".
    ev = evidence_tier(monthly_sales, sales_drops_30d)
    if ev == "MODERATE":
        risk_flags.append({"label": "Only moderate sales evidence", "level": "info", "detail": _sales_evidence_fact(monthly_sales, sales_drops_30d)})
    elif ev == "INSUFFICIENT":
        risk_flags.append({"label": "No sales evidence available yet", "level": "warn", "detail": "Not necessarily poor demand — Keepa simply has no confirmed figure or rank-drop history for this ASIN"})

    # ---- value (point 7: today and 90d ALWAYS shown separately) ----
    val = value_tier(eff_profit, eff_roi)
    value = {
        "tier": val,
        "today_profit": today_profit, "today_roi": today_roi,
        "typical_profit_90d": profit_90d, "typical_roi_90d": roi_90d,
        "peak_profit": peak_profit, "peak_roi": peak_roi,
    }

    # ---- what's good (2026-09-04 UI redesign) ----
    # Deliberately NOT freshness -- "the source was checked recently"
    # answers whether the OTHER facts here can be trusted, it isn't
    # itself a reason the listing is good (Tamara's own correction).
    # Freshness moves to freshness_caption below instead, for the
    # template's header area, not this list. Each fact here is a real
    # positive claim, never a restated number already visible in the
    # value tiles above.
    good_facts = []
    if val in ("STRONG", "EXCEPTIONAL"):
        good_facts.append(f"{val.capitalize()} 90-day economics")
    if ev == "STRONG":
        good_facts.append(_sales_evidence_fact(monthly_sales, sales_drops_30d))
    if not source_unconfirmed:
        good_facts.append(source_fact)

    evidence = {"tier": ev}

    risk = {
        "flags": risk_flags,
        # Structured form of the same evidence embedded in the flag
        # labels above -- None throughout when not yet computed (a
        # pre-2026-09-04 scan), never a fabricated zero/normal reading.
        "days_at_25pct_roi_90d": days_at_25pct if recurrence_known else None,
        "priced_days_90d": priced_days if recurrence_known else None,
        "days_since_last_competition_spike": spike_days_ago if spike_context_known else None,
        "price_change_since_competition_spike_pct": spike_price_change if spike_context_known else None,
        "days_since_last_price_dip": dip_days_ago if dip_context_known else None,
        "offers_change_at_last_price_dip_pct": dip_offers_change if dip_context_known else None,
    }

    # ---- action ----
    if recommendation == "GATED":
        action = ACTION_BLOCKED
    elif recommendation == "PEAK_WINDOW":
        # OpportunityEngine's OWN peak-viable gate (5+ profitable days
        # in 90, real sales evidence, ROI>=17%) is authoritative -- see
        # ReviewQueueService.list_leads' own comment at the merge point
        # for why the second, redundant gate that used to sit here was
        # removed. Every PEAK_WINDOW record reaching this module is
        # real, already-vetted evidence of a RECURRING window, not a
        # currently-buyable-at-face-value price -- HISTORICAL_RECURRING
        # regardless of how strong the peak numbers are.
        val = value_tier(max(eff_profit, peak_profit), max(eff_roi, peak_roi))
        value["tier"] = val
        action = ACTION_HISTORICAL_RECURRING
    else:
        notable = recommendation == "BUY" or (eff_roi > IS_NOTABLE_ROI and ev != "INSUFFICIENT")

        if notable and (not has_risk or ev == "STRONG"):
            if freshness in STALE_FRESHNESS_STATES:
                action = ACTION_HISTORICAL_RECURRING
            elif price_falling:
                action = ACTION_PRICE_DROP_BUY_NOW
            elif has_risk:
                action = ACTION_BUY_WITH_CAUTION
            else:
                action = ACTION_BUY_NOW
        elif val in ("STRONG", "EXCEPTIONAL") and ev != "STRONG":
            action = ACTION_HIGH_VALUE_LOW_CONFIDENCE
        elif val == "MODERATE" and ev == "INSUFFICIENT":
            action = ACTION_INVESTIGATE
        else:
            action = ACTION_WATCH

    return {
        "action": action,
        "action_label": ACTION_LABELS[action],
        "action_description": ACTION_DESCRIPTIONS[action],
        "value": value,
        "good": good_facts,
        "evidence": evidence,
        "risk": risk,
        "verify_source_match": verify_source_match,
        # Header-area context, not a "good"/"risky" claim -- see
        # good_facts' own comment on why freshness moved here.
        "freshness_caption": _freshness_fact(freshness),
    }
