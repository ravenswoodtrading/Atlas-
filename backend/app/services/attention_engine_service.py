"""
Attention Engine v1 (Phase 5B, 2026-09-04) -- decides WHO deserves
scanning attention next, using evidence Atlas already has. Does NOT
scan anything, does NOT touch the scheduler's actual tick execution,
and does NOT change Discovery/OpportunityLens scoring anywhere. See
this module's own rule functions for the explicit (never opaque)
reasoning behind every recommendation -- the whole point of this phase,
per the approved brief, is that Discovery tier is NOT scan priority:
Grohe/Makita/Brother (MEDIUM) measurably outperform several HIGH-tier
brands on useful opportunities per token (see scan_economics_service).

Four evidence layers stay visibly SEPARATE inputs into one set of
explicit rules -- never blended into one opaque score:
  - Discovery Intelligence (DiscoveryIntelligenceService)      -- "where might there be opportunity?"
  - Scan Economics (scan_economics_service)                    -- "where are we actually getting useful results?"
  - Historical Buying Intelligence (historical_buying_service)  -- "where have we succeeded before?"
  - Competitor evidence (part of Discovery Intelligence's own evidence dict)

Four lanes, explicit rules, ranked by explicit tier (1 = strongest):
  - KEEP_SCANNING:    already queued, not quiet, doesn't clear the
                       extra-attention bar -- steady, no change recommended.
  - INCREASE_ATTENTION: clears at least one of the four eligibility
                       tiers below -- either a queued brand whose measured
                       yield/recent BUY justifies MORE than baseline, or
                       an unqueued brand with strong-enough evidence to
                       deserve a first look.
  - EXPLORE:          real but thinner evidence -- historically proven
                       but currently invisible, competitor-thin, HVC-heavy
                       with no recent confirmed BUY, or a strong category
                       with little brand-level evidence yet. Visible, but
                       explicitly NOT to be read as a BUY signal.
  - REDUCE_QUIET:     already queued, meaningful scan volume, but no
                       useful Action in QUIET_AFTER_DAYS (scan_economics_
                       service's own threshold, reused, not reinvented).
                       Recommendation is "QUIET -- CONSIDER REDUCING
                       ATTENTION", never automatic removal.

Eligibility tiers for INCREASE_ATTENTION (a brand needs only ONE):
  1. Recent confirmed BUY (BUY_NOW/PRICE_DROP_BUY_NOW within Discovery
     Intelligence's own RECENT_WINDOW_DAYS) -- the strongest possible
     evidence: it just happened.
  2. Strong measured scan economics -- meets scan_economics_service's
     own MIN_SCANS_FOR_EFFICIENCY floor AND useful/10k tokens above the
     MEDIAN of all floor-qualifying candidates RIGHT NOW (computed live
     from the current candidate set, never a fixed invented number --
     this is deliberately self-adjusting as Atlas's own fleet economics
     move, per the approved brief's "do not invent arbitrary weights").
  3. Real historical buying evidence (proven_active or proven_quiet --
     NOT proven_invisible, see below) on a brand NOT currently queued --
     "we've made money from this before and Atlas isn't watching it."
  4. Strong, recent competitor evidence (>=3 distinct competitors,
     reusing DiscoveryIntelligenceService.score_target's own existing
     "3 distinct competitors" bonus threshold rather than inventing a
     new number) on a brand with thin own-scan evidence, NOT currently
     queued -- "competitors keep finding this and we've barely looked."

Tier 3/4 apply ONLY to brands not already queued -- an already-queued
brand's case for extra attention rests on tier 1/2 (real evidence Atlas
itself has produced), matching the approved brief's own ordering ("competitor
evidence alone should not outrank demonstrated Atlas scan performance").

proven_invisible brands (real buying history, ZERO current Discovery
evidence at all) deliberately do NOT clear tier 3 -- there is no live
signal to act on yet, only historical proof it once worked. They land
in EXPLORE, exactly as the approved brief's own example lists them.

NOTHING here writes to the database or calls Keepa/SP-API/SerpApi/
Brave -- every function is a pure read + pure computation.
"""
from collections import defaultdict

from app.database.database import SessionLocal
from app.database.models import AttentionIgnoredBrand
from app.services.product_repository import ProductRepository
from app.services.discovery_intelligence_service import (
    DiscoveryIntelligenceService, MEANINGFUL_SCAN_THRESHOLD,
    HIGH_VALUE_LOW_CONFIDENCE_MEANINGFUL_COUNT, RECENT_WINDOW_DAYS,
)
from app.services.historical_buying_service import (
    get_brand_history, classify_discovery_state,
    STATE_PROVEN_ACTIVE, STATE_PROVEN_QUIET, STATE_PROVEN_INVISIBLE,
)
from app.services.scan_queue_service import ScanQueueService
from app.services import scan_economics_service as economics_service

LANE_KEEP_SCANNING = "KEEP_SCANNING"
LANE_INCREASE_ATTENTION = "INCREASE_ATTENTION"
LANE_EXPLORE = "EXPLORE"
LANE_REDUCE_QUIET = "REDUCE_QUIET"
LANE_NONE = "NONE"

LANE_LABELS = {
    LANE_KEEP_SCANNING: "Keep Scanning",
    LANE_INCREASE_ATTENTION: "Increase Attention",
    LANE_EXPLORE: "Explore",
    LANE_REDUCE_QUIET: "Quiet -- Consider Reducing Attention",
    LANE_NONE: "",
}

TIER_RECENT_BUY = 1
TIER_STRONG_ECONOMICS = 2
TIER_HISTORICAL_BUYING = 3
TIER_COMPETITOR = 4
TIER_EXPLORE = 5
TIER_NONE = 99

# Bounded extra-attention mechanism (approved brief, Section 5) -- a
# fixed, small, explicit ceiling, not a proportional/weighted scheme.
MAX_EXTRA_ATTENTION_PER_LAP = 3

# Competitor-evidence eligibility bar (Tier 4) -- reuses the EXACT
# threshold DiscoveryIntelligenceService.score_target already uses for
# its own ">=3 distinct competitors" scoring bonus, rather than
# inventing a second, different number for a very similar idea.
STRONG_COMPETITOR_THRESHOLD = 3


def _queue_status_by_brand() -> dict:
    """
    Same three-state classification Scan Intelligence's own
    _queue_states already uses (not_queued/active/paused/gated),
    reimplemented here as a small, self-contained read so this service
    doesn't import a route module. Reuses ONLY ScanQueueItem/
    AutomationSettings -- no new state.

    A brand can have MORE THAN ONE ScanQueueItem row (several currently
    do -- Philips has 4, Corsair 2). Grouped into a LIST per brand
    (never a naive {brand: item} dict, which would silently keep only
    whichever row iteration happened to visit last and could report a
    gated brand as active depending on row order -- caught by this
    phase's own validation simulation). A brand counts as "gated" if
    ANY of its rows are -- gating is a brand-level fact
    (is_gated_by_name), so even one row already reflecting it is
    reason enough to treat the whole brand as gated for attention
    purposes.
    """
    items_by_brand = defaultdict(list)
    for item in ScanQueueService.list_items():
        items_by_brand[item.brand].append(item)
    automation_paused = ScanQueueService.is_paused()

    statuses = {}
    for brand, items in items_by_brand.items():
        if any(item.status == "gated" for item in items):
            statuses[brand] = "gated"
        elif automation_paused:
            statuses[brand] = "paused"
        else:
            statuses[brand] = "active"
    return statuses


def _ignored_brands() -> set:
    db = SessionLocal()
    try:
        return {row.brand for row in db.query(AttentionIgnoredBrand).all()}
    finally:
        db.close()


def _build_candidate_universe(targets_by_brand: dict, buying_history: dict, queued_brands: set,
                               excluded_brands: set) -> set:
    """
    Every brand worth evaluating at all -- deliberately excludes the
    ~300+ brands with genuinely minimal evidence either side (nothing
    for any rule below to act on). Union of: currently queued, has real
    EU A2A buying history, has at least 1 of our own EU scans, or has
    strong recent competitor evidence.

    GATED and EXCLUDED brands are removed here, unconditionally, before
    any lane is assigned (2026-09-05, real bug fix -- this service had
    no awareness of gating/exclusion at all, so brands Atlas can't or
    won't sell (Canon, HP, Samsung, SanDisk, Braun, Rimmel, Oral-B were
    all found gated-but-still-recommended in real data) could appear in
    ANY lane, including INCREASE_ATTENTION, with no warning. A brand
    Atlas can't or won't sell is never a legitimate "spend more
    attention here" candidate, however good its evidence looks.
    """
    candidates = set(queued_brands) | set(buying_history.keys())
    for brand, t in targets_by_brand.items():
        own = t["evidence"]["own"]
        comp = t["evidence"]["competitor"]
        if own and own["eu_scanned"] >= 1:
            candidates.add(brand)
        elif comp and comp["distinct_competitors"] >= STRONG_COMPETITOR_THRESHOLD and comp.get("recent"):
            candidates.add(brand)

    candidates -= excluded_brands
    candidates -= {b for b in candidates if targets_by_brand.get(b, {}).get("gated")}
    return candidates


def _explain(lane: str, tier: int, brand: str, t: dict | None, econ: dict | None,
             bh: dict | None, discovery_state: str | None, queued: bool) -> str:
    """Human-readable, per-brand explanation -- never a generic template with no real numbers in it."""
    own = t["evidence"]["own"] if t else None
    comp = t["evidence"]["competitor"] if t else None

    if lane == LANE_REDUCE_QUIET:
        return (
            f"{econ['total_records']} scans but only {econ['useful_count']} useful action"
            f"{'s' if econ['useful_count'] != 1 else ''} and no useful result for "
            f"{econ['days_since_last_useful']} days."
        )

    if lane == LANE_INCREASE_ATTENTION:
        if tier == TIER_RECENT_BUY:
            if bh:
                return (
                    f"Historically profitable brand (£{bh['total_profit']:.2f} recorded EU A2A profit) "
                    f"with a recent confirmed BUY."
                )
            return "Recent confirmed BUY -- Atlas's strongest possible signal."
        if tier == TIER_STRONG_ECONOMICS:
            return (
                f"{econ['useful_count']} useful actions from {econ['total_records']} scans. "
                f"{econ['useful_per_10k']} useful opportunities per 10k tokens -- "
                f"one of Atlas's stronger measured yields right now."
            )
        if tier == TIER_HISTORICAL_BUYING:
            return (
                f"Historically profitable brand (£{bh['total_profit']:.2f} recorded EU A2A profit, "
                f"{bh['purchase_count']} purchases), but currently not in the Scan Queue."
            )
        if tier == TIER_COMPETITOR:
            return (
                f"{comp['distinct_competitors']} competitors are finding EU/UK A2A products from this brand "
                f"recently, but Atlas has little own scan evidence and isn't currently scanning it."
            )

    if lane == LANE_EXPLORE:
        if discovery_state == STATE_PROVEN_INVISIBLE:
            return (
                f"Historically profitable (£{bh['total_profit']:.2f} recorded EU A2A profit), "
                f"but Atlas has zero competitor or own-scan evidence for this brand today."
            )
        if own and own["eu_high_value_low_confidence_count"] >= HIGH_VALUE_LOW_CONFIDENCE_MEANINGFUL_COUNT:
            return (
                f"{own['eu_high_value_low_confidence_count']} HIGH VALUE -- LOW CONFIDENCE opportunities, "
                f"but no recent confirmed BUY to act on yet."
            )
        if comp and comp["distinct_competitors"] >= 1:
            return (
                f"{comp['distinct_competitors']} competitor(s) are finding EU/UK A2A products from this brand, "
                f"but Atlas has insufficient own scan evidence."
            )
        if t and t["evidence"]["category"] and t["evidence"]["category"]["is_top"]:
            return f"In a top EU A2A category ({t['evidence']['category']['name']}), but little brand-level evidence yet."
        return "Some evidence exists, but not enough yet to justify more scanning attention."

    if lane == LANE_KEEP_SCANNING:
        if econ and econ.get("meets_sample_floor") and econ.get("useful_per_10k") is not None:
            return f"{econ['useful_count']} useful actions from {econ['total_records']} scans -- steady, no change recommended."
        return "Currently queued and producing normal results -- no change recommended."

    return ""


def get_attention_candidates(use_cache: bool = True) -> list:
    """
    Read-only. Returns one dict per candidate brand:
    {brand, category, tier(Discovery HIGH/MEDIUM/LOW/UNRANKED),
     queued(bool), queue_status, discovery_state (buying-history state
     or None), buy_history_profit, buy_history_purchases,
     recent_confirmed_buy(int), useful_actions(int), useful_per_10k
     (None below the sample floor), tokens_per_confirmed_buy,
     days_since_last_useful, economics_status, competitor_count,
     competitor_recent(bool), lane, lane_label, eligibility_tier,
     reason, ignored(bool)}.

    Sorted lane-first (INCREASE_ATTENTION, then EXPLORE, then
    KEEP_SCANNING, then REDUCE_QUIET), then by eligibility_tier
    ascending (1=strongest), then by the tier's own relevant magnitude.
    Ignored brands (see ignore_brand/unignore_brand) are INCLUDED with
    ignored=True rather than silently dropped -- evidence is never
    deleted, only hidden by the caller if it chooses to.
    """
    targets = DiscoveryIntelligenceService.list_discovery_targets(limit=100_000, use_cache=use_cache)
    targets_by_brand = {t["brand"]: t for t in targets}
    buying_history = get_brand_history(eu_only=True)
    queue_status = _queue_status_by_brand()
    queued_brands = set(queue_status.keys())
    ignored = _ignored_brands()
    excluded_brands = ProductRepository.get_excluded_brand_names()

    candidates = _build_candidate_universe(targets_by_brand, buying_history, queued_brands, excluded_brands)
    economics = economics_service.get_brand_economics(brands=list(candidates), use_cache=use_cache)

    # Tier 2's bar is computed LIVE from the current candidate set's own
    # distribution -- the median useful/10k among brands that clear the
    # sample floor -- never a fixed invented number (approved brief's
    # own instruction). If fewer than 2 brands clear the floor, no
    # brand can qualify via tier 2 alone (not enough data to define a
    # meaningful "above average" yet).
    floor_values = sorted(
        e["useful_per_10k"] for e in economics.values()
        if e["meets_sample_floor"] and e["useful_per_10k"] is not None
    )
    median_useful_per_10k = None
    if len(floor_values) >= 2:
        mid = len(floor_values) // 2
        median_useful_per_10k = (
            floor_values[mid] if len(floor_values) % 2
            else (floor_values[mid - 1] + floor_values[mid]) / 2
        )

    rows = []
    for brand in candidates:
        t = targets_by_brand.get(brand)
        own = t["evidence"]["own"] if t else None
        comp = t["evidence"]["competitor"] if t else None
        bh = buying_history.get(brand)
        econ = economics.get(brand)
        queued = brand in queued_brands
        q_status = queue_status.get(brand, "not_queued")

        discovery_state = classify_discovery_state(bh, own, comp) if bh else None

        recent_confirmed_buy = own["eu_confirmed_buy_count_recent"] if own else 0

        lane = LANE_NONE
        tier = TIER_NONE

        is_quiet = bool(econ and econ["status"] == economics_service.STATUS_QUIET)

        if queued and is_quiet and q_status == "active":
            lane, tier = LANE_REDUCE_QUIET, TIER_NONE
        else:
            eligible = []
            if recent_confirmed_buy > 0:
                eligible.append(TIER_RECENT_BUY)
            if (econ and econ["meets_sample_floor"] and econ["useful_per_10k"] is not None
                    and median_useful_per_10k is not None and econ["useful_per_10k"] > median_useful_per_10k):
                eligible.append(TIER_STRONG_ECONOMICS)
            if (not queued and discovery_state in (STATE_PROVEN_ACTIVE, STATE_PROVEN_QUIET)):
                eligible.append(TIER_HISTORICAL_BUYING)
            if (not queued and comp and comp["distinct_competitors"] >= STRONG_COMPETITOR_THRESHOLD
                    and comp.get("recent") and (not own or own["eu_scanned"] < MEANINGFUL_SCAN_THRESHOLD)):
                eligible.append(TIER_COMPETITOR)

            if eligible:
                lane, tier = LANE_INCREASE_ATTENTION, min(eligible)
            elif queued:
                lane, tier = LANE_KEEP_SCANNING, TIER_NONE
            else:
                explore = (
                    discovery_state == STATE_PROVEN_INVISIBLE
                    or (own and own["eu_high_value_low_confidence_count"] >= HIGH_VALUE_LOW_CONFIDENCE_MEANINGFUL_COUNT and recent_confirmed_buy == 0)
                    or (comp and comp["distinct_competitors"] >= 1)
                    or (t and t["evidence"]["category"] and t["evidence"]["category"]["is_top"])
                )
                if explore:
                    lane, tier = LANE_EXPLORE, TIER_EXPLORE

        if lane == LANE_NONE:
            continue

        reason = _explain(lane, tier, brand, t, econ, bh, discovery_state, queued)

        rows.append({
            "brand": brand,
            "category": t["category_name"] if t else (bh["category"] if bh else ""),
            "tier": t["tier"] if t else "UNRANKED",
            "queued": queued,
            "queue_status": q_status,
            "discovery_state": discovery_state,
            "buy_history_profit": bh["total_profit"] if bh else None,
            "buy_history_purchases": bh["purchase_count"] if bh else None,
            "recent_confirmed_buy": recent_confirmed_buy,
            "total_scans": econ["total_records"] if econ else 0,
            "useful_actions": econ["useful_count"] if econ else 0,
            "useful_per_10k": econ["useful_per_10k"] if econ else None,
            "meets_sample_floor": econ["meets_sample_floor"] if econ else False,
            "tokens_per_confirmed_buy": econ["tokens_per_confirmed_buy"] if econ else None,
            "days_since_last_useful": econ["days_since_last_useful"] if econ else None,
            "economics_status": econ["status"] if econ else None,
            "competitor_count": comp["distinct_competitors"] if comp else 0,
            "competitor_recent": bool(comp and comp.get("recent")),
            "lane": lane,
            "lane_label": LANE_LABELS[lane],
            "eligibility_tier": tier,
            "reason": reason,
            "ignored": brand in ignored,
        })

    lane_order = {LANE_INCREASE_ATTENTION: 0, LANE_EXPLORE: 1, LANE_KEEP_SCANNING: 2, LANE_REDUCE_QUIET: 3}

    def _sort_key(r):
        secondary = -(r["useful_per_10k"] or 0) if r["eligibility_tier"] == TIER_STRONG_ECONOMICS else (
            -(r["buy_history_profit"] or 0) if r["eligibility_tier"] in (TIER_RECENT_BUY, TIER_HISTORICAL_BUYING) else
            -(r["competitor_count"] or 0)
        )
        return (lane_order.get(r["lane"], 9), r["eligibility_tier"], secondary)

    rows.sort(key=_sort_key)
    return rows


def select_extra_attention_slots(candidates: list, already_granted_this_lap: frozenset = frozenset(),
                                  max_slots: int = MAX_EXTRA_ATTENTION_PER_LAP) -> list:
    """
    Pure selection -- picks up to `max_slots` distinct brands to give an
    EXTRA turn this lap. Deliberately uses a DIFFERENT ranking than
    get_attention_candidates' own display order (approved 2026-09-04,
    after the first simulated lap surfaced a real flaw): Philips -- the
    WORST-yielding confirmed-BUY brand in the entire fleet at 3.68
    useful/10k tokens -- won a slot over Grohe/Makita/Brother at 20-26
    useful/10k, purely because Tier 1 (recent confirmed BUY) sorted
    ahead of Tier 2 (measured yield) in the display list. That ordering
    is right for EXPLAINING why a brand is interesting, but wrong for
    deciding who gets the next scarce turn.

    Slot-allocation ranking:
    1. QUEUED INCREASE_ATTENTION brands that meet the sample floor
       (scan_economics_service.MIN_SCANS_FOR_EFFICIENCY -- >=10 real
       scans, so useful/10k is statistically meaningful) -- ranked by
       useful/10k tokens DESCENDING. A recent confirmed BUY (Tier 1)
       no longer automatically outranks a materially stronger measured
       yield once there's enough sample to trust the yield number --
       it's still real positive evidence (it's part of why the brand
       reached this lane at all), just not a ranking override.
    2. QUEUED INCREASE_ATTENTION brands below the sample floor -- yield
       isn't statistically meaningful yet for these, so they fall back
       to the existing evidence-based eligibility_tier (1=recent BUY
       strongest), same ordering as before.
    Group 1 always ranks above group 2 -- real measured evidence beats
    inferred evidence once there's enough of it.

    UNQUEUED brands are never eligible for an extra slot, regardless of
    tier or yield -- a slot is an extra turn for a brand ALREADY
    rotating through the queue; an unqueued brand's case for attention
    is "add it to the queue" (a manual action a human takes from the
    Increase Attention list), not "give it a turn it was never having
    in the first place".

    Also excludes anything gated/paused (manual exclusions respected),
    already granted an extra slot this lap (no brand gets two), or
    explicitly ignored by a human. Does not touch the database, does
    not call Keepa, does not run a scan -- it only decides WHO, exactly
    per the approved brief's own framing ("Existing Scan Queue remains
    responsible for actually executing scans").
    """
    eligible = [
        c for c in candidates
        if c["lane"] == LANE_INCREASE_ATTENTION
        and c["queued"]
        and not c["ignored"]
        and c["brand"] not in already_granted_this_lap
        and c["queue_status"] not in ("gated", "paused")
    ]

    has_yield = [c for c in eligible if c["meets_sample_floor"] and c["useful_per_10k"] is not None]
    no_yield = [c for c in eligible if not (c["meets_sample_floor"] and c["useful_per_10k"] is not None)]

    has_yield.sort(key=lambda c: -c["useful_per_10k"])
    no_yield.sort(key=lambda c: c["eligibility_tier"])

    return (has_yield + no_yield)[:max_slots]


def ignore_brand(brand: str) -> None:
    """Hide a brand from attention recommendations WITHOUT deleting any evidence -- see AttentionIgnoredBrand."""
    brand_norm = brand.strip().lower()
    db = SessionLocal()
    try:
        if not db.query(AttentionIgnoredBrand).filter(AttentionIgnoredBrand.brand == brand_norm).first():
            db.add(AttentionIgnoredBrand(brand=brand_norm))
            db.commit()
    finally:
        db.close()


def unignore_brand(brand: str) -> None:
    brand_norm = brand.strip().lower()
    db = SessionLocal()
    try:
        db.query(AttentionIgnoredBrand).filter(AttentionIgnoredBrand.brand == brand_norm).delete()
        db.commit()
    finally:
        db.close()
