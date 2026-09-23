from urllib.parse import quote

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.discovery_intelligence_service import DiscoveryIntelligenceService
from app.services.scan_queue_service import ScanQueueService
from app.services.scan_schedule_service import pending_reviews, decide_review, TIERS
from app.services.brand_scan_service import BrandScanService
from app.services.scan_coordinator import ScanCoordinator
from app.services.activity_log import ActivityLog
from app.services.stale_cache import StaleCache
from app.services.opportunity_lens_service import ACTION_LABELS
from app.services.historical_buying_service import get_brand_history, classify_discovery_state
from app.services.scan_economics_service import get_brand_economics, MIN_SCANS_FOR_EFFICIENCY
from app.services.attention_engine_service import (
    get_attention_candidates, ignore_brand, unignore_brand,
    LANE_INCREASE_ATTENTION, LANE_EXPLORE,
)

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# One-off manual "Scan Now" pass (2026-09-04, Discovery Phase 3) -- same
# manual-scan code path Discovery's own /discovery route uses (acquires
# ScanCoordinator, respects the normal RESCAN_COOLDOWN, spends real Keepa
# tokens). Deliberately smaller than the Scan Queue's own per-tick limit
# (100) -- this is a quick "check this recommendation now" action from a
# human click, not a full campaign; ongoing coverage is what "Add to Scan
# Queue" is for.
SCAN_NOW_LIMIT = 50

# Rows per page for the ranked brand table (visual pass, 2026-09-04) --
# 200 ranked brands rendered in one giant table was the main complaint
# behind wanting this redesign in the first place. Pure client-visible
# slicing of the already-computed, already-sorted list -- no new query,
# no change to what list_discovery_targets itself returns or how it's
# scored/ordered.
TABLE_PAGE_SIZE = 20

# Comfortably above the real brand count (652 at last check) --
# list_discovery_targets computes every ranked brand internally
# regardless of `limit` (confirmed by direct timing in Phase 4B: same
# cost whether capped at 200 or uncapped), so raising this is a free
# correctness fix, not a performance tradeoff. Needed for Historical
# Buying Intelligence (Phase 4D) specifically -- a plain limit=200 cap
# was silently excluding 53 of the 102 real EU A2A buy-history brands
# (including Playmobil, Eichhorn, Instax, Knipex) from ever appearing
# on this page at all.
DISCOVERY_TARGETS_LIMIT = 5000


def _summary_metrics(targets: list, queue_states: dict, discovery_states: dict) -> dict:
    """
    Top-of-page counts -- deliberately never a single opaque score, just
    plain, inspectable tallies of the same tiers/flags the table and
    detail panel already show. Always computed over the FULL ranked
    list, never the filtered/paginated table view, so the numbers here
    don't shift depending on which filter tab or search is active.
    """
    high = sum(1 for t in targets if t["tier"] == "HIGH")
    medium = sum(1 for t in targets if t["tier"] == "MEDIUM")
    low = sum(1 for t in targets if t["tier"] == "LOW")
    gated = sum(1 for t in targets if t["gated"])
    capped = sum(1 for t in targets if t["capped"])
    queued = sum(1 for s in queue_states.values() if s["state"] != "not_queued")

    # Historical Buying Intelligence (Phase 4D) -- counts across the
    # SAME three states classify_discovery_state returns (discovery_states
    # is keyed over every buy-history brand, not just `targets` -- see
    # _discovery_states' own docstring for why), so these numbers agree
    # exactly with the per-brand badges/panel below.
    proven_active = sum(1 for s in discovery_states.values() if s == "proven_active")
    proven_quiet = sum(1 for s in discovery_states.values() if s == "proven_quiet")
    proven_invisible = sum(1 for s in discovery_states.values() if s == "proven_invisible")

    recent_confirmed = 0
    high_value_low_confidence = 0
    for t in targets:
        own = t["evidence"]["own"]
        if not own:
            continue
        if own["eu_confirmed_buy_count_recent"] > 0:
            recent_confirmed += 1
        if own["eu_high_value_low_confidence_count"] >= 3:
            high_value_low_confidence += 1

    return {
        "total": len(targets),
        "high": high,
        "medium": medium,
        "low": low,
        "gated": gated,
        "capped": capped,
        "queued": queued,
        "recent_confirmed": recent_confirmed,
        "high_value_low_confidence": high_value_low_confidence,
        "proven_active": proven_active,
        "proven_quiet": proven_quiet,
        "proven_invisible": proven_invisible,
        "proven_total": proven_active + proven_quiet + proven_invisible,
    }


def _discovery_states(buying_history: dict, targets_by_brand: dict) -> dict:
    """
    Historical Buying Intelligence (Phase 4D) -- {brand: state} for
    EVERY brand with real EU A2A purchase history, computed via
    classify_discovery_state. Deliberately iterates buying_history, NOT
    targets -- a "proven_invisible" brand by definition has no
    Discovery evidence at all, so it would never appear in `targets` in
    the first place; this is the only way to count it.
    """
    states = {}
    for brand, bh in buying_history.items():
        t = targets_by_brand.get(brand)
        own = t["evidence"]["own"] if t else None
        comp = t["evidence"]["competitor"] if t else None
        states[brand] = classify_discovery_state(bh, own, comp)
    return states


def _filter_targets(targets: list, queue_states: dict, discovery_states: dict, tier: str, q: str) -> list:
    """
    Table-only filter/search (visual pass, 2026-09-04) -- narrows what's
    DISPLAYED, never what's scored or how it's ordered. Summary metrics
    and the detail panel both keep reading the full, unfiltered `targets`
    list separately, so switching tabs/searching never changes the
    counts at the top of the page.
    """
    filtered = targets

    if tier == "queued":
        filtered = [t for t in filtered if queue_states.get(t["brand"], {}).get("state") != "not_queued"]
    elif tier == "proven":
        # Historical Buying Intelligence (Phase 4D) -- any brand with
        # real EU A2A purchase history, regardless of current tier.
        filtered = [t for t in filtered if t["brand"] in discovery_states]
    elif tier in ("HIGH", "MEDIUM", "LOW"):
        filtered = [t for t in filtered if t["tier"] == tier]

    q_norm = q.strip().lower()
    if q_norm:
        filtered = [t for t in filtered if q_norm in t["brand"]]

    return filtered


def _queue_states(targets: list) -> dict:
    """
    Per-brand Scan Queue status (2026-09-04, Discovery Phase 3 follow-up)
    -- reuses ScanQueueItem/AutomationSettings as the ONLY source of
    truth (no new table, no new state, no scheduler/cadence change).
    Distinguishes actually-active scanning from merely-queued-but-not-
    running, so "Add to Scan Queue" is never shown for a brand that's
    already there, and "Already scanning" is never shown for one that's
    queued but not really being worked right now:

    - "not_queued": no ScanQueueItem for this brand at all.
    - "gated": queued, but BrandScanService already refused to spend
      tokens on it (item.status == "gated" -- set by
      ScanQueueService.run_next_tick's own gated_brand_skip branch).
      Checked before the global pause flag -- this is the more specific,
      more actionable reason it isn't scanning (resuming automation
      alone wouldn't fix it; removing the brand from Gated Brands would).
    - "paused": queued, not gated, but the whole automated queue is
      paused (AutomationSettings.paused via ScanQueueService.is_paused).
    - "active": queued, not gated, automation running -- will get its
      turn in the round-robin rotation like any other queued brand.
    """
    items_by_brand = {item.brand: item for item in ScanQueueService.list_items()}
    automation_paused = ScanQueueService.is_paused()

    states = {}
    for t in targets:
        brand = t["brand"]
        item = items_by_brand.get(brand)

        if item is None:
            states[brand] = {"state": "not_queued", "item": None}
        elif item.status == "gated":
            states[brand] = {"state": "gated", "item": item}
        elif automation_paused:
            states[brand] = {"state": "paused", "item": item}
        else:
            states[brand] = {"state": "active", "item": item}

    return states


def _pagination_window(page: int, total_pages: int, radius: int = 1) -> list:
    """
    Bootstrap-style "1 2 ... 5 6 7 ... 66" page list -- always includes
    first/last page plus a small window around the current page, with
    None marking a gap (rendered as an ellipsis). Pure display helper,
    no DB access.
    """
    if total_pages <= 1:
        return [1]

    pages = {1, total_pages, page}
    for d in range(1, radius + 1):
        pages.add(page - d)
        pages.add(page + d)
    pages = sorted(p for p in pages if 1 <= p <= total_pages)

    window = []
    prev = None
    for p in pages:
        if prev is not None and p - prev > 1:
            window.append(None)
        window.append(p)
        prev = p
    return window


def _economics_rows(economics: dict, targets_by_brand: dict) -> list:
    """
    Scan Economics table rows (Phase 5A, 2026-09-04) -- one row per
    currently-queued brand, sorted by useful-opportunities-per-10k-
    tokens for brands that meet the sample-size floor (highest first),
    then the below-floor brands after (grouped together, not scattered
    through the ranking a ratio they don't have would imply). Never
    shown: a computed ratio for a brand below MIN_SCANS_FOR_EFFICIENCY
    -- see scan_economics_service's own docstring for why that would be
    noise, not signal.
    """
    rows = []
    for brand, e in economics.items():
        t = targets_by_brand.get(brand)
        rows.append({
            "brand": brand,
            "tier": t["tier"] if t else "UNRANKED",
            **e,
        })

    with_floor = [r for r in rows if r["meets_sample_floor"]]
    without_floor = [r for r in rows if not r["meets_sample_floor"]]
    with_floor.sort(key=lambda r: -(r["useful_per_10k"] or 0))
    without_floor.sort(key=lambda r: r["brand"])
    return with_floor + without_floor


def _describe_scan_now(brand: str, result: dict) -> str:
    if result.get("gated_brand_skip"):
        return f"{brand} is on the Gated Brands list -- no tokens spent."
    if result.get("error"):
        return f"Couldn't scan {brand}: {result['error']}"
    return (
        f"Scanned {result.get('asins_scanned', 0)} {brand} products just now "
        f"({result.get('count', 0)} opportunities found)."
    )


# The three heavy roll-ups this page shows (discovery targets, scan economics, attention candidates) take ~13s to build
# cold and each kept its own 5-minute cache, so whoever opened the page just after they expired waited for all of it
# (2026-09-21, Tamara: pages slow "constantly"). They are now built together and served from here: the last result is
# handed out at once and rebuilt in the background when it is 5 minutes old. Prewarmed at server start (see main.py).
_INTEL_BUNDLE = StaleCache(ttl_seconds=300, retry_seconds=60, name="scan-intelligence")


def _compute_intel_bundle() -> dict:
    return {
        "targets": DiscoveryIntelligenceService.list_discovery_targets(limit=DISCOVERY_TARGETS_LIMIT),
        "economics": get_brand_economics(),
        "attention": get_attention_candidates(),
    }


def _intel_bundle() -> dict:
    return _INTEL_BUNDLE.get("bundle", _compute_intel_bundle)


def prewarm_intel_bundle() -> bool:
    """Start building the bundle in the background (server start); False if one is already being built."""
    return _INTEL_BUNDLE.refresh_in_background("bundle", _compute_intel_bundle)


@router.get("/scan-intelligence")
def scan_intelligence(request: Request, brand: str = "", tick_result: str = "",
                       tier: str = "all", q: str = "", page: int = 1):
    bundle = _intel_bundle()
    targets = bundle["targets"]
    queue_states = _queue_states(targets)

    targets_by_brand = {t["brand"]: t for t in targets}

    # Historical Buying Intelligence (Phase 4D) -- read-only, EU-A2A-only
    # aggregation from the team's own purchasing workbook (see
    # historical_buying_service's own module docstring). Deliberately
    # NOT merged into `targets`/tier/score anywhere -- kept as its own
    # dict, looked up by brand in the template, exactly like
    # queue_states above. An empty dict here (no import has been run
    # yet) degrades to every brand simply showing no buying history --
    # never an error.
    buying_history = get_brand_history(eu_only=True)
    discovery_states = _discovery_states(buying_history, targets_by_brand)

    selected = targets_by_brand.get(brand.strip().lower()) if brand else None
    if selected is None and targets:
        selected = targets[0]

    # Historical Buying Intelligence (Phase 4D) -- the "historically
    # proven but invisible to Atlas" bucket (real EU A2A purchase
    # history, but zero competitor/own Discovery evidence) can never
    # appear in `targets` at all (there's nothing to rank it by), so it
    # gets its own dedicated section below the ranked table rather than
    # being reachable through the tier filter/table like every other
    # brand. Sorted by recorded profit, same convention as everywhere
    # else on this page.
    invisible_history = sorted(
        ((b, buying_history[b]) for b, state in discovery_states.items() if state == "proven_invisible"),
        key=lambda kv: -kv[1]["total_profit"],
    )

    # Scan Economics (Phase 5A, 2026-09-04) -- read-only measurement
    # layer, deliberately kept separate from tier/score everywhere in
    # this route. Computed only for currently-queued brands (the
    # population with enough real scan history to measure at all);
    # cached internally (see scan_economics_service) since the full
    # computation is a real ~3s pass over live ProductRecord history.
    economics = bundle["economics"]
    economics_rows = _economics_rows(economics, targets_by_brand)

    # Attention Engine v1 (Phase 5B, 2026-09-04) -- WHO deserves the
    # next unit of scanning attention, using explicit rules over the
    # evidence already computed above (never a second scoring system --
    # see attention_engine_service's own module docstring). Cached
    # internally (~13s cold, same pattern as everything else on this
    # page). "Recommended Now" shows INCREASE_ATTENTION first, then a
    # handful of EXPLORE candidates -- KEEP_SCANNING/REDUCE_QUIET are
    # already visible in the Scan Economics section above, not repeated
    # here.
    attention_candidates = bundle["attention"]
    attention_increase = [c for c in attention_candidates if c["lane"] == LANE_INCREASE_ATTENTION and not c["ignored"]]
    attention_explore = [c for c in attention_candidates if c["lane"] == LANE_EXPLORE and not c["ignored"]][:10]

    filtered = _filter_targets(targets, queue_states, discovery_states, tier, q)

    page = max(1, page)
    total_pages = max(1, -(-len(filtered) // TABLE_PAGE_SIZE))
    page = min(page, total_pages)
    start = (page - 1) * TABLE_PAGE_SIZE
    page_targets = filtered[start:start + TABLE_PAGE_SIZE]

    return templates.TemplateResponse(
        request=request,
        name="scan_intelligence.html",
        context={
            "request": request,
            "tier_reviews": pending_reviews(),
            "schedule_tiers": TIERS,
            "targets": page_targets,
            "total_filtered": len(filtered),
            "page": page,
            "total_pages": total_pages,
            "pagination_window": _pagination_window(page, total_pages),
            "tier": tier,
            "q": q,
            "summary": _summary_metrics(targets, queue_states, discovery_states),
            "selected": selected,
            "queue_states": queue_states,
            "buying_history": buying_history,
            "discovery_states": discovery_states,
            "invisible_history": invisible_history,
            "economics": economics,
            "economics_rows": economics_rows,
            "min_scans_for_efficiency": MIN_SCANS_FOR_EFFICIENCY,
            "attention_increase": attention_increase,
            "attention_explore": attention_explore,
            "tick_result": tick_result,
            "action_label": ACTION_LABELS,
        },
    )


@router.post("/scan-intelligence/tier-review")
def scan_intelligence_tier_review(review_id: int = Form(...), decision: str = Form(...)):
    if decision not in ("approve", "dismiss"):
        raise HTTPException(422, "Choose approve or dismiss")
    try:
        decide_review(review_id, decision == "approve")
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(url="/scan-intelligence#tier-reviews", status_code=303)


@router.post("/scan-intelligence/add-to-queue")
def scan_intelligence_add_to_queue(brand: str = Form(...)):
    """
    "Add to Scan Queue" -- Phase 4 option A (approved 2026-09-04):
    manual, human-triggered only, never automatic. No category
    restriction ("Let Atlas Decide") -- reuses the exact same
    ScanQueueService.add_item the Scan Queue page's own form calls, so
    it behaves identically (continues from any past campaign's
    furthest page, joins the same round-robin rotation).
    """
    brand_norm = brand.strip().lower()
    ScanQueueService.add_item(brand_norm)
    message = f"Added {brand_norm} to the Scan Queue."
    return RedirectResponse(
        url=f"/scan-intelligence?brand={quote(brand_norm)}&tick_result={quote(message)}",
        status_code=303,
    )


@router.post("/scan-intelligence/add-brand")
def scan_intelligence_add_brand(brand: str = Form(...)):
    """
    Manual "Add Brand" -- for a brand Atlas has no competitor/own-scan
    evidence for at all (so it can't appear in the ranked table yet).
    Default behaviour is "Let Atlas Decide": no category restriction,
    same ScanQueueService.add_item the ranked table's own "Add to Scan
    Queue" button uses. Category-scoped campaigns are still available
    on the Scan Queue page itself for anyone who wants finer control.
    """
    brand_norm = brand.strip().lower()
    ScanQueueService.add_item(brand_norm)
    message = f"Added {brand_norm} to the Scan Queue -- Atlas will start building evidence on it."
    return RedirectResponse(
        url=f"/scan-intelligence?brand={quote(brand_norm)}&tick_result={quote(message)}",
        status_code=303,
    )


@router.post("/scan-intelligence/scan-now")
def scan_intelligence_scan_now(brand: str = Form(...)):
    """
    "Scan Now" -- an explicit, immediate, human-triggered scan (spends
    real Keepa tokens). Same manual-scan code path /discovery uses:
    takes priority over the automated Scan Queue via ScanCoordinator,
    respects the normal rescan cooldown (force_rescan=False -- this is
    "check on Atlas's recommendation now", not "re-verify everything").
    """
    brand_norm = brand.strip().lower()

    ScanCoordinator.acquire_for_manual_scan()
    try:
        scanner = BrandScanService(usage_category="discovery")
        result = scanner.scan(brand_norm, limit=SCAN_NOW_LIMIT)
    finally:
        ScanCoordinator.release_after_manual_scan()

    if not result.get("error") and not result.get("gated_brand_skip"):
        ActivityLog.record(
            "brand_search",
            f"{brand_norm} (manual, Scan Intelligence, {result.get('asins_scanned', 0)} ASINs)",
        )

    message = _describe_scan_now(brand_norm, result)
    return RedirectResponse(
        url=f"/scan-intelligence?brand={quote(brand_norm)}&tick_result={quote(message)}",
        status_code=303,
    )


@router.post("/scan-intelligence/attention/extra-scan")
def scan_intelligence_attention_extra_scan(brand: str = Form(...)):
    """
    "Give Extra Attention" (Phase 5B, 2026-09-04) -- a MANUAL, explicit,
    human-triggered scan, identical mechanics to "Scan Now" above
    (same ScanCoordinator priority, same rescan cooldown). Kept as a
    separate endpoint (not just an alias) purely so it logs distinctly
    in ActivityLog -- "this was an Attention Engine recommendation a
    human acted on", not an ordinary Discovery search. The bounded,
    AUTOMATIC extra-attention mechanism (select_extra_attention_slots,
    max 3/lap) is built and validated (see the Phase 5B report) but NOT
    wired into the live scheduler tick yet -- this button is the only
    way an extra-attention grant actually happens right now.
    """
    brand_norm = brand.strip().lower()

    ScanCoordinator.acquire_for_manual_scan()
    try:
        scanner = BrandScanService(usage_category="discovery")
        result = scanner.scan(brand_norm, limit=SCAN_NOW_LIMIT)
    finally:
        ScanCoordinator.release_after_manual_scan()

    if not result.get("error") and not result.get("gated_brand_skip"):
        ActivityLog.record(
            "brand_search",
            f"{brand_norm} (manual, Attention Engine extra attention, {result.get('asins_scanned', 0)} ASINs)",
        )

    message = _describe_scan_now(brand_norm, result)
    return RedirectResponse(
        url=f"/scan-intelligence?brand={quote(brand_norm)}&tick_result={quote(message)}",
        status_code=303,
    )


@router.post("/scan-intelligence/attention/ignore")
def scan_intelligence_attention_ignore(brand: str = Form(...)):
    """Hide a brand from Attention recommendations without deleting any evidence (see ignore_brand's own docstring)."""
    ignore_brand(brand)
    return RedirectResponse(url="/scan-intelligence", status_code=303)


@router.post("/scan-intelligence/attention/unignore")
def scan_intelligence_attention_unignore(brand: str = Form(...)):
    unignore_brand(brand)
    return RedirectResponse(url="/scan-intelligence", status_code=303)
