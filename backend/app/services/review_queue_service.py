import json
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead, ProductRecord, SellerNewListing, TrackedSeller
from app.services.product_repository import ProductRepository
from app.services.seller_watch_service import SellerWatchService

# Nav consolidation fast-follow (2026-08-24) -- Reviewed History was
# Lead-only; there was no equivalent history view for scan/competitor
# reviews at all (Products just shows everything unfiltered, reviewed
# or not -- confirmed live, not something this merges INTO). Mirrors
# leads.py's old Lead-only history cap.
HISTORY_LIMIT = 300

# Atlas nav consolidation, Phase 1 (2026-08-24) -- the Lead Queue's
# BUY/WATCH/AVOID verdicts don't line up with a score/confidence tier,
# so this is the one real judgment call in the merge, confirmed with
# the user before building: BUY reads as "main tab" (same trust level
# as a star buy/BUY recommendation), WATCH reads as "Consider tab"
# (worth a look, not yet clearly-worth-it), AVOID is excluded from the
# live queue entirely -- same treatment IGNORE already gets today.
LEAD_MAIN_VERDICTS = ("BUY",)
LEAD_CONSIDER_VERDICTS = ("WATCH",)

# Comfortably covers any realistic unreviewed-notable backlog -- same
# pragmatic-cap pattern SellerWatchService.list_detections already
# uses (limit=200) rather than a genuinely unbounded fetch.
MAX_LEADS = 500

# First-pass volume guard for the "Consider" tab (2026-08-19). Reviewing
# a Consider lead removes it (same as every other Review Queue filter),
# but the tab is explicitly NOT expected to be cleared to zero
# regularly -- it's fine for a genuine backlog to sit there with newer
# leads landing on top. The user flagged the profit/sales filter itself
# may need tightening once real volume is seen after a restart -- this
# constant is the other lever if the filter alone isn't enough.
MAX_CONSIDER_LEADS = 150

# A PEAK_WINDOW lead already cleared OpportunityEngine.MIN_VIABLE_ROI
# (17%, raised from 10% 2026-08-23) at the peak price just to be
# tagged PEAK_WINDOW at all -- that floor means "not worthless", not
# "worth the risk". Buying against a recent price peak is inherently
# riskier than a normal BUY/CONSIDER (you're betting the price gets
# back there again), so the Review Queue holds PEAK_WINDOW leads to a
# higher bar before surfacing them: BOTH the peak ROI must be genuinely
# strong AND the underlying score must already be at CONSIDER-tier
# quality, despite failing the current/90d-avg price gate that kept it
# out of CONSIDER/BUY.
#
# Was an OR (either bar alone was enough) until 2026-09-03 -- real bug,
# not a design choice: a score=22 lead with roi=-44.9% at today's price
# (a genuine LOSS) was showing up purely because its speculative peak
# ROI cleared 35%, and several other sub-30-score leads did the same
# (Tamara, 2026-09-03: "including some of the peak ones that really
# aren't good enough... slow scoring leads getting through"). A bad
# score means the underlying demand/trend/competition picture is weak
# regardless of what a one-off historical price spike says, so a weak
# score should disqualify a peak lead even with a strong peak ROI, not
# just be a second way in.
PEAK_WORTH_IT_ROI = 35
PEAK_WORTH_IT_SCORE = 65

# Unified Review Queue VIEWS (Review Queue backend build, 2026-09-03
# follow-up -- "unified review queue / one decision per ASIN") -- a
# common attention layer sitting ABOVE the three pipelines' own,
# deliberately different, recommendation vocabularies (OpportunityEngine's
# BUY/CONSIDER/PEAK_WINDOW/... for scan+competitor, Lead.verdict's
# BUY/WATCH/AVOID for VA). This never re-scores or re-thresholds
# anything -- see _item_views -- it only reads what each pipeline
# already decided and maps it onto zero or more of these.
#
# IMPORTANT: an item can belong to MULTIPLE views at once (see
# _build_merged_item's own docstring) -- BUY NOW is a universal view,
# not a competing bucket with VA TO REVIEW. A VA-submitted BUY with
# clean economics is simultaneously "a strong current opportunity"
# (BUY_NOW) and "awaiting your final decision" (VA_TO_REVIEW); one
# review resolves both (see resolve_item).
QUEUE_PRIORITY_BUY_NOW = "BUY_NOW"
QUEUE_PRIORITY_VA_TO_REVIEW = "VA_TO_REVIEW"
QUEUE_PRIORITY_BORDERLINE = "BORDERLINE"
QUEUE_PRIORITY_NEEDS_ATTENTION = "NEEDS_ATTENTION"

# Reserved (explicitly NOT built yet, per instruction) for the future
# "Atlas Attention Queue" -- no item produced today is ever given this
# view; it exists purely so the views vocabulary already has a slot for
# it and a later addition isn't a breaking change to this one.
QUEUE_VIEW_URGENT = "URGENT"

QUEUE_PRIORITIES = (
    QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
)

# For sorting/the single "primary" view a list needs to sort by -- the
# STRONGEST/most-urgent view among however many an item belongs to.
# NEEDS_ATTENTION always wins (a conflict or a stale/blocked BUY is
# exactly the thing you'd otherwise miss by only looking at the "best"
# view). Does NOT mean the item only HAS that one view -- see `views`
# (plural) on the merged item for full membership.
_PRIORITY_RANK = {
    QUEUE_PRIORITY_NEEDS_ATTENTION: 0,
    QUEUE_PRIORITY_BUY_NOW: 1,
    QUEUE_PRIORITY_VA_TO_REVIEW: 2,
    QUEUE_PRIORITY_BORDERLINE: 3,
    QUEUE_VIEW_URGENT: 0,
}

# Reserved for the future "Atlas Attention Queue" (explicitly NOT built
# yet, per instruction) -- every item ReviewQueueService produces today
# is sourcing-related. URGENT_ACTION/ADMIN/etc are a different future
# subsystem's categories; this constant exists purely so today's items
# already carry the field a later merge won't have to retrofit.
ATTENTION_CATEGORY_SOURCING = "SOURCING"

# Scan/competitor recommendation values that map to BORDERLINE -- the
# existing "Consider tab" tier, unchanged, just given a name in the
# unified vocabulary.
BORDERLINE_RECOMMENDATIONS = {"CONSIDER", "PEAK_WINDOW", "LOW_CONFIDENCE", "LOW_SCORE"}

# classify_offer_freshness states that mean "don't trust this BUY at
# face value any more" -- see that function's own docstring.
STALE_FRESHNESS_STATES = {"STALE", "UNAVAILABLE"}


def _sort_when(lead) -> datetime:
    """
    "when" now comes from three different columns across two tables
    (ProductRecord.scanned_at/SellerNewListing.detected_at, both
    naive, and Lead.analyzed_at) -- confirmed live 2026-08-24 that a
    tz-aware value slipping in anywhere crashes the whole page
    (`can't compare offset-naive and offset-aware datetimes`).
    Normalizes to naive UTC (dropping tzinfo, not converting -- every
    value in this app is already UTC in practice, see
    Lead.added_at/analyzed_at's own `datetime.now(timezone.utc)`
    default) so the merged sort can never crash on this again,
    regardless of which table a given row's timestamp came from.
    """
    when = lead["when"]

    if when is None:
        return datetime.min

    return when.replace(tzinfo=None) if when.tzinfo else when


# Mirrors ProductRepository.SORT_OPTIONS' shape (lambda + reverse flag)
# but keyed on the merged lead dict, since scan and competitor leads
# don't share a single ORM model to sort in the DB -- this list is
# already fully in memory by the time sorting happens.
SORT_OPTIONS = {
    "when_desc": (_sort_when, True),
    "when_asc": (_sort_when, False),
    "score_desc": (lambda lead: lead["score"] or 0, True),
    "score_asc": (lambda lead: lead["score"] or 0, False),
    # Added for the Review Queue's sort control (UI redesign pass,
    # 2026-09-03) -- same shape as the two pairs above, just keyed on
    # profit/roi instead of score. Both fields already exist on every
    # lead dict (see _scan_lead_dict/_lead_dict/_competitor_lead_dict);
    # this is a display ordering choice, not a new computed value.
    "profit_desc": (lambda lead: lead["profit"] or 0, True),
    "roi_desc": (lambda lead: lead["roi"] or 0, True),
}

# Canonical origin values for /review-queue's source filter -- one per
# badge the table already renders (see review_queue.html's Source
# column), so filtering to e.g. "sheet" shows exactly the rows already
# wearing the "VA Sheet" badge, no separate taxonomy to keep in sync.
# The "lead" row source collapses to three of these via lead_subsource
# (manual/sheet/shortlist -- see _lead_dict); "scan" and "competitor"
# map straight through.
SOURCE_FILTERS = ("scan", "competitor", "manual", "sheet", "shortlist")

SOURCE_FILTER_LABELS = {
    "scan": "Scan",
    "competitor": "Competitor find",
    "manual": "Manual (Verdict Checker)",
    "sheet": "VA Sheet",
    "shortlist": "Shortlist",
}

# Structured review/decision reason categories (atlas-review-queue-
# backend-v1.md section 5) -- ADDITIVE alongside the existing free-text
# review_reason/decision_reason columns, never a replacement for them.
# A rejecting user may optionally pick one of these; historical rows
# (and anyone who skips the picker) simply have category=None and keep
# their free-text reason exactly as before. Lets Atlas eventually
# answer questions like "what % of BUYs are rejected because the
# source offer disappeared" without parsing free text (section 11).
REVIEW_REASON_CATEGORIES = (
    "NO_BUYABLE_OFFER",
    "PRICE_CHANGED",
    "NO_LONGER_PROFITABLE",
    "ALREADY_BOUGHT",
    "TOO_MUCH_STOCK",
    "TOO_EXPENSIVE",
    "IMPORT_DUTY",
    "WRONG_MATCH",
    "GATED",
    "INSUFFICIENT_SALES",
    "INSUFFICIENT_PROFIT",
    "PRODUCT_RISK",
    "EU_PLUG",
    "EBAY",
    "OTHER",
    # Added for the Quick Reject menu (UI redesign pass, 2026-09-03) --
    # a generic "not interested" that isn't any of the more specific
    # reasons above and isn't vague enough to need free text either.
    # Purely additive, same TEXT-column vocabulary extension pattern as
    # every other value in this tuple -- nothing validates against this
    # list (grepped: no caller does membership-checking on it), so this
    # is display/reporting vocabulary only, not a new decision pathway.
    "NOT_INTERESTED",
)

REVIEW_REASON_CATEGORY_LABELS = {
    "NO_BUYABLE_OFFER": "No buyable offer",
    "PRICE_CHANGED": "Price changed",
    "NO_LONGER_PROFITABLE": "No longer profitable",
    "ALREADY_BOUGHT": "Already bought",
    "TOO_MUCH_STOCK": "Too much stock",
    "TOO_EXPENSIVE": "Too expensive",
    "IMPORT_DUTY": "Over import duty",
    "WRONG_MATCH": "Wrong match",
    "GATED": "Gated",
    "INSUFFICIENT_SALES": "Not enough sales evidence",
    "INSUFFICIENT_PROFIT": "Not enough profit",
    "PRODUCT_RISK": "Product risk (returns/safety/etc)",
    "EU_PLUG": "EU plug",
    "EBAY": "eBay",
    "OTHER": "Other",
    "NOT_INTERESTED": "Don't want",
}

# Categories that mean "the opportunity expired between Atlas's check
# and the user's review" (section 2) rather than "Atlas's original
# sourcing/matching judgement was wrong" -- used to interpret a
# rejection without ever touching the stored decision itself. Anything
# not in this set (WRONG_MATCH, GATED, PRODUCT_RISK, etc.) is treated
# as a real sourcing-quality signal, not timing/volatility.
EXPIRY_REASON_CATEGORIES = {"NO_BUYABLE_OFFER", "PRICE_CHANGED", "NO_LONGER_PROFITABLE"}


def interpret_review_reason(category: str | None) -> str | None:
    """
    "OPPORTUNITY_EXPIRED" | "SOURCING_ISSUE" | None (no category
    picked, nothing to interpret). Pure classification, section 2 --
    never changes what got stored, only how a rejection reads.
    """
    if not category:
        return None
    return "OPPORTUNITY_EXPIRED" if category in EXPIRY_REASON_CATEGORIES else "SOURCING_ISSUE"


# Offer freshness states (section 3). Thresholds are configurable here
# rather than hardcoded elsewhere, and are deliberately anchored to
# EU_A2A_FRESHNESS_INTERVAL_SECONDS (app/main.py) -- the only existing
# automated source-recheck cadence in Atlas (daily). FRESH covers
# "checked well within the last sweep cycle"; AGING covers "checked,
# but approaching a full cycle old, sweep may not have reached it
# again yet"; anything older is STALE. UNAVAILABLE overrides both on
# any check (automated or manual) that came back not-buyable,
# regardless of age -- a confirmed "not there" is a stronger signal
# than elapsed time either way.
OFFER_FRESHNESS_FRESH_HOURS = 6
OFFER_FRESHNESS_AGING_HOURS = 30  # ~1.25x the daily sweep interval


def classify_offer_freshness(
    scanned_at: datetime | None,
    last_offer_checked_at: datetime | None = None,
    last_offer_buyable: bool | None = None,
    now: datetime | None = None,
) -> str:
    """
    "FRESH" | "AGING" | "STALE" | "UNAVAILABLE" | "UNKNOWN".

    last_offer_checked_at/last_offer_buyable are the persisted result
    of the most recent AUTOMATED recheck (see eu_a2a_freshness_service),
    which is more authoritative than the original scan if present --
    falls back to scanned_at (the original check) when no separate
    recheck has ever happened, which is the common case today outside
    EU A2A. "UNKNOWN" only when there is no timestamp at all to judge
    from (should not happen for a real record, but this must never
    raise on a blank/legacy row).
    """
    if last_offer_buyable is False:
        return "UNAVAILABLE"

    checked_at = last_offer_checked_at or scanned_at
    if checked_at is None:
        return "UNKNOWN"

    now = now or datetime.now(timezone.utc)
    checked_at = checked_at.replace(tzinfo=None) if checked_at.tzinfo else checked_at
    now = now.replace(tzinfo=None) if now.tzinfo else now

    age_hours = (now - checked_at).total_seconds() / 3600.0

    if age_hours < 0:
        return "FRESH"  # clock skew guard -- never show a negative age as stale
    if age_hours <= OFFER_FRESHNESS_FRESH_HOURS:
        return "FRESH"
    if age_hours <= OFFER_FRESHNESS_AGING_HOURS:
        return "AGING"
    return "STALE"


def apply_lead_decision(
    lead: Lead, decision: str, reason: str | None = None, reason_category: str | None = None
) -> None:
    """
    What "deciding" a lead actually means -- shared by /review/decide
    (a human using Atlas's own UI), the sheet-decision webhook (a
    human's decision arriving from the VA sheet instead), and
    ReviewQueueService.resolve_item (the unified cross-source decision,
    2026-09-03), so none of these three entry points can drift apart on
    what a decision does. Caller owns the db session/commit; this only
    mutates the passed-in lead.

    MOVED here from app/routes/leads.py (2026-09-03, unified Review
    Queue build) so ReviewQueueService can call it directly --
    routes/leads.py already imports FROM review_queue_service (for
    /review/history), so the reverse import would have been circular.
    routes/leads.py now imports this function from here instead; its
    own behaviour for the existing "approved"/"rejected"/"oos" values
    is byte-for-byte unchanged.

    decision: "approved" | "rejected" | "oos" | "watch" | "need_more_info".
    "oos" (Amazon out of stock right now) and "watch" (added 2026-09-03,
    unified Review Queue build) are both "not actionable this instant,
    but worth catching later" -- not a real approve/reject. "need_more_info"
    (also added 2026-09-03) means "flagged for follow-up before I can
    decide" -- distinct from both. All five clear the lead from the
    pending Lead Queue the same way (status="reviewed") -- each is its
    own bucket in Reviewed History (see reviewed_leads.html /
    list_reviewed_history), never merged into "rejected".

    reason: optional free-text "why not" (2026-08-23) -- see
    Lead.decision_reason.

    reason_category: optional structured reason (atlas-review-queue-
    backend-v1.md section 5, see REVIEW_REASON_CATEGORIES below) --
    additive alongside `reason`, never a replacement for it. The sheet
    webhook never sends one (the sheet has no such column), so this is
    None there, same as before.

    "oos" and "watch" BOTH auto-add the ASIN to the existing Watchlist,
    reusing its already-built re-check machinery (WatchlistService.
    check_stale runs weekly, or visit /watchlist to force an immediate
    recheck) instead of building a parallel monitoring mechanism --
    title/brand come from the lead's own keepa_metrics (see
    VerdictService/LeadAnalysisService), so no extra Keepa lookup is
    needed here. "need_more_info" does NOT auto-watch -- it means
    "flagged for follow-up", not "recheck price/stock periodically", so
    the Watchlist's own re-check machinery wouldn't actually help here.
    """
    lead.decision = decision
    lead.decision_reason = reason or None
    lead.decision_reason_category = reason_category or None
    lead.status = "reviewed"
    lead.reviewed_at = datetime.now(timezone.utc)

    if decision in ("oos", "watch"):
        title, brand = "", ""

        if lead.keepa_metrics:
            try:
                metrics = json.loads(lead.keepa_metrics)
                title = metrics.get("title") or ""
                brand = metrics.get("brand") or ""
            except Exception:
                pass

        note = (
            "Amazon OOS at review -- watching for restock" if decision == "oos"
            else "Marked WATCH at review -- keeping an eye on this"
        )
        ProductRepository.add_watch(lead.asin, title=title, brand=brand, note=note)


class ReviewQueueService:
    """
    Thin orchestrator, no new table of its own -- merges scan-sourced
    notable leads (ProductRepository) and buyable+notable competitor
    detections (SellerWatchService) into one list, one row shape, so
    they can be reviewed from a single page exactly the same way
    regardless of where they came from.
    """

    @staticmethod
    def _scan_lead_dict(record) -> dict:
        """
        Shared row shape for a scan-sourced lead (as opposed to a
        competitor detection, see the loop below) -- used by both
        list_leads() (main tab) and list_consider_leads() (Consider
        tab) so the two tabs render identically and can't drift apart.
        """
        parsed_report = {}
        if record.report_json:
            try:
                parsed_report = json.loads(record.report_json)
            except Exception:
                parsed_report = {}

        return {
            "source": "scan",
            "asin": record.asin,
            "title": record.title,
            "brand": record.brand,
            "best_source_marketplace": record.best_source_marketplace,
            "best_source_cost_gbp": record.best_source_cost_gbp,
            "buy_box_now": record.buy_box_now,
            "profit": record.profit,
            "roi": record.roi,
            "profit_90d": record.profit_90d,
            "roi_90d": record.roi_90d,
            "score": record.score,
            # Passthrough of the existing ProductRecord.confidence column
            # (UI redesign pass, 2026-09-03) -- OpportunityEngine already
            # computes and stores this 0-100 int for every scan record;
            # it just wasn't threaded into this dict before. Used to show
            # a real (never estimated) High/Medium/Low confidence badge
            # in the detail panel's "Atlas First Pass" section.
            "confidence": record.confidence,
            "recommendation": record.recommendation,
            "monthly_sales": record.monthly_sales,
            "when": record.scanned_at,
            "parsed_report": parsed_report,
            "reasoning": {},
            "rationale": None,
            "sourcing_tag": None,
            "currently_buyable": False,
            "seller": None,
            "listing_id": 0,
            "conflict_note": None,
            # OA Source Discovery match confidence (2026-08-29) --
            # "" for every non-OA scan record, which is exactly when
            # review_queue.html skips the confidence badge. See
            # ProductRecord.match_tier's own comment for why this
            # lives here instead of parsed_report.
            "match_tier": record.match_tier,
            "match_confidence_pct": record.match_confidence_pct,
            "source_confidence": record.source_confidence,
            # Offer freshness / structured reason (atlas-review-queue-
            # backend-v1.md sections 3/5) -- additive, see
            # classify_offer_freshness's own docstring.
            "freshness": classify_offer_freshness(
                record.scanned_at, record.last_offer_checked_at, record.last_offer_buyable,
            ),
            "last_offer_checked_at": record.last_offer_checked_at,
            "last_offer_price_gbp": record.last_offer_price_gbp,
            "last_offer_buyable": record.last_offer_buyable,
            "review_reason_category": record.review_reason_category,
        }

    @staticmethod
    def _lead_dict(lead) -> dict:
        """
        Row shape for a Verdict Checker / VA sheet / Shortlist lead
        (the `Lead` model), normalized into the exact same dict shape
        _scan_lead_dict produces -- see LEAD_MAIN_VERDICTS/
        LEAD_CONSIDER_VERDICTS above for the verdict->tab mapping.

        Fields with no real Lead equivalent (score, sourcing
        reasoning breakdown) are left at a neutral default rather than
        guessed -- `rationale` carries the actual Claude-written
        reasoning instead, rendered in its own "Why?" panel by the
        template rather than forced into parsed_report's differently-
        keyed shape.
        """
        metrics = {}
        if lead.keepa_metrics:
            try:
                metrics = json.loads(lead.keepa_metrics)
            except Exception:
                metrics = {}

        profit = lead.va_profit if lead.va_profit is not None else metrics.get("keepa_estimate_profit")
        roi = lead.va_roi if lead.va_roi is not None else metrics.get("keepa_estimate_roi")

        # Atlas's own evidence-based sourcing classification (2026-09-03,
        # Review Queue backend build -- see VerdictService.compute_metrics'
        # "sourcing_classification"), preferred over the VA's own typed-in
        # sourcing_type when Atlas actually managed to compute one. Falls
        # back to the VA's label (unverified) otherwise -- never silently
        # dropped, just a weaker signal than Atlas's own evidence.
        sourcing_classification = metrics.get("sourcing_classification") or {}
        atlas_sourcing_tag = sourcing_classification.get("sourcing_tag")

        source_check = metrics.get("source_check") or {}
        similar_rejections = metrics.get("similar_rejections") or []

        return {
            "source": "lead",
            "lead_subsource": lead.source,  # "manual" | "sheet" | "shortlist"
            "lead_id": lead.id,
            "asin": lead.asin,
            "title": metrics.get("title") or lead.asin,
            "brand": metrics.get("brand") or "",
            "best_source_marketplace": lead.source_detail or lead.sourcing_type or "",
            "best_source_cost_gbp": lead.va_cost_price or 0.0,
            "buy_box_now": lead.va_sale_price or metrics.get("buy_box_now") or 0.0,
            "profit": profit or 0.0,
            "roi": roi or 0.0,
            "profit_90d": metrics.get("keepa_estimate_profit_90d") or 0.0,
            "roi_90d": metrics.get("keepa_estimate_roi_90d") or 0.0,
            "score": 0,
            "recommendation": lead.verdict,  # "BUY" | "WATCH" (AVOID never reaches here)
            "monthly_sales": metrics.get("monthly_sales") or 0,
            "when": lead.analyzed_at,
            # No OpportunityEngine confidence score exists for a Lead
            # (see _scan_lead_dict's own comment on match_tier just
            # below) -- left None rather than guessed, so the detail
            # panel simply omits the Confidence line for a VA-only item.
            "confidence": None,
            "parsed_report": {},
            "reasoning": sourcing_classification.get("reasoning") or {},
            "rationale": lead.rationale,
            "sourcing_tag": atlas_sourcing_tag or lead.sourcing_type,
            "sourcing_tag_source": "atlas" if atlas_sourcing_tag else ("va" if lead.sourcing_type else None),
            "currently_buyable": False,
            "seller": None,
            "listing_id": 0,
            "conflict_note": None,
            # VA first-pass signals (2026-09-03, Review Queue backend
            # build) -- see LeadAnalysisService._analyze_one. Facts, not
            # verdicts: the priority layer (ReviewQueueService._item_
            # priority) decides what to do with them, never an
            # auto-reject here.
            "already_in_inventory": bool(metrics.get("already_in_inventory")),
            "inventory_detail": metrics.get("inventory_detail"),
            "similar_rejections": similar_rejections,
            "has_similar_rejection": bool(similar_rejections),
            "buyability_blocker": source_check.get("blocker"),
            # No OA match-confidence equivalent for a Verdict Checker/
            # VA Sheet/Shortlist lead -- see _scan_lead_dict's comment.
            "match_tier": "",
            "match_confidence_pct": 0,
            "source_confidence": "",
            # No automated source-offer recheck exists for Lead rows yet
            # (see eu_a2a_freshness_service.py's own scope note) --
            # freshness falls back to analyzed_at, the only "last
            # checked" signal a Lead actually has.
            "freshness": classify_offer_freshness(lead.analyzed_at, None, None),
            "last_offer_checked_at": None,
            "last_offer_price_gbp": None,
            "last_offer_buyable": None,
            "review_reason_category": lead.decision_reason_category,
        }

    @staticmethod
    def _origin(lead: dict) -> str:
        """One of SOURCE_FILTERS -- the "lead" row source collapses to
        its lead_subsource (manual/sheet/shortlist); scan/competitor
        map straight through."""
        if lead["source"] != "lead":
            return lead["source"]
        return lead.get("lead_subsource") or "manual"

    @staticmethod
    def filter_by_source(leads: list, source_filter: str) -> list:
        """
        Narrows an already-built merged leads list to one origin (see
        SOURCE_FILTERS) -- e.g. every VA Sheet lead, so it can be
        reviewed as its own batch instead of interleaved with scan/
        competitor/other Lead rows. An empty or unrecognized filter is
        a no-op (shows everything), same "don't hide data on a bad
        value" convention as SORT_OPTIONS.get's fallback above.
        """
        if source_filter not in SOURCE_FILTERS:
            return leads
        return [lead for lead in leads if ReviewQueueService._origin(lead) == source_filter]

    @staticmethod
    def _pending_leads(verdicts: tuple) -> list:
        """Unreviewed Lead rows (status='analyzed', decision not yet set) with a verdict in `verdicts`."""
        db = SessionLocal()
        try:
            rows = (
                db.query(Lead)
                .filter(Lead.status == "analyzed", Lead.decision.is_(None), Lead.verdict.in_(verdicts))
                .order_by(Lead.analyzed_at.desc())
                .all()
            )
            return [ReviewQueueService._lead_dict(lead) for lead in rows]
        finally:
            db.close()

    @staticmethod
    def _flag_conflicts(leads: list) -> None:
        """
        Roadmap item 12 -- the same ASIN can carry a BUY Lead verdict
        while its most recent scan record is IGNORE/GATED (or vice
        versa: a notable scan record while a Lead marked it AVOID),
        with nothing today surfacing that disagreement. Mutates each
        affected dict's conflict_note in place rather than returning a
        new structure, since this only ever adds a note to rows
        already being displayed -- it never changes which rows appear.

        Deliberately bounded to the ASINs already in `leads` (not a
        scan of every Lead/ProductRecord in the database) -- two small
        lookups by ASIN list, not a full cross-reference.
        """
        lead_asins = {row["asin"] for row in leads if row["source"] == "lead"}
        scan_asins = {row["asin"] for row in leads if row["source"] != "lead"}

        if not lead_asins and not scan_asins:
            return

        db = SessionLocal()
        try:
            conflicting_records = {}
            if lead_asins:
                records = (
                    db.query(ProductRecord)
                    .filter(ProductRecord.asin.in_(lead_asins))
                    .order_by(ProductRecord.scanned_at.desc())
                    .all()
                )
                for record in records:
                    if record.asin not in conflicting_records and record.recommendation in ("IGNORE", "GATED"):
                        conflicting_records[record.asin] = record.recommendation

            conflicting_leads = {}
            if scan_asins:
                lead_rows = (
                    db.query(Lead)
                    .filter(Lead.asin.in_(scan_asins), Lead.verdict == "AVOID")
                    .order_by(Lead.analyzed_at.desc())
                    .all()
                )
                for lead in lead_rows:
                    conflicting_leads.setdefault(lead.asin, lead.verdict)
        finally:
            db.close()

        for row in leads:
            if row["source"] == "lead" and row["asin"] in conflicting_records:
                row["conflict_note"] = (
                    f"Atlas's own scan pipeline currently marks this ASIN "
                    f"{conflicting_records[row['asin']]} -- worth a second look before acting."
                )
            elif row["source"] != "lead" and row["asin"] in conflicting_leads:
                row["conflict_note"] = "A Verdict Check on this exact ASIN came back AVOID -- worth a second look before acting."

    @staticmethod
    def _competitor_lead_dict(entry: dict) -> dict:
        """
        Shared row shape for a competitor-sourced lead -- used by both
        the currently_buyable branch (list_notable_buyable) and the
        historical-evidence-only branch (list_historical_a2a_not_buyable)
        in list_leads() below, so the two can't drift apart on shape.
        `entry` is {"listing": SellerNewListing, "record": ProductRecord,
        "seller": TrackedSeller}, exactly what both SellerWatchService
        methods return.
        """
        listing = entry["listing"]
        record = entry["record"]
        seller = entry["seller"]

        parsed_report = {}
        if record.report_json:
            try:
                parsed_report = json.loads(record.report_json)
            except Exception:
                parsed_report = {}

        reasoning = {}
        if listing.sourcing_reasoning_json:
            try:
                reasoning = json.loads(listing.sourcing_reasoning_json)
            except Exception:
                reasoning = {}

        return {
            "source": "competitor",
            "asin": listing.asin,
            "title": record.title,
            "brand": record.brand,
            "best_source_marketplace": record.best_source_marketplace,
            "best_source_cost_gbp": record.best_source_cost_gbp,
            "buy_box_now": record.buy_box_now,
            "profit": record.profit,
            "roi": record.roi,
            "profit_90d": record.profit_90d,
            "roi_90d": record.roi_90d,
            "score": record.score,
            "confidence": record.confidence,
            "recommendation": record.recommendation,
            "monthly_sales": record.monthly_sales,
            "when": listing.detected_at,
            "parsed_report": parsed_report,
            "reasoning": reasoning,
            "rationale": None,
            "sourcing_tag": listing.sourcing_tag,
            "currently_buyable": listing.currently_buyable,
            "seller": seller,
            "listing_id": listing.id,
            "conflict_note": None,
            # Competitor finds don't go through OA Source Discovery's
            # shopping-match pipeline -- see _scan_lead_dict's comment.
            "match_tier": record.match_tier,
            "match_confidence_pct": record.match_confidence_pct,
            "source_confidence": record.source_confidence,
            # Competitor rows read best_source_marketplace/cost off
            # the same ProductRecord the "scan" branch above uses --
            # see eu_a2a_freshness_service.py's own scope note --
            # so the same freshness fields apply here too.
            "freshness": classify_offer_freshness(
                record.scanned_at, record.last_offer_checked_at, record.last_offer_buyable,
            ),
            "last_offer_checked_at": record.last_offer_checked_at,
            "last_offer_price_gbp": record.last_offer_price_gbp,
            "last_offer_buyable": record.last_offer_buyable,
            "review_reason_category": listing.review_reason_category,
        }

    @staticmethod
    def list_leads(sort: str = "when_desc") -> list:
        scan_records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_LEADS, review_filter="notable", sort="scanned_desc",
        )

        # Riskier "peak price window" leads (see
        # OpportunityEngine.PEAK_WINDOW) -- merged in alongside the
        # normal notable leads so they're visible here too, same page,
        # but the recommendation field ("PEAK_WINDOW", never "BUY"/
        # "CONSIDER") keeps them clearly distinguishable in the UI and
        # they never affect is_notable/Discord's own trust bar.
        peak_records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_LEADS, review_filter="peak", sort="scanned_desc",
        )

        # Genuinely viable (real ROI at today's/90d-avg price, not a
        # speculative peak), CONSIDER-tier-scoring leads a confidence
        # penalty alone knocked below CONSIDER/BUY -- see
        # OpportunityEngine.analyse's LOW_CONFIDENCE branch (2026-09-03).
        # Merged in the same way as peak_records, for the same reason:
        # is_notable() explicitly excludes LOW_CONFIDENCE (see that
        # method's own comment), so these would otherwise never surface
        # anywhere despite often carrying real, strong ROI.
        low_confidence_records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_LEADS, review_filter="low_confidence", sort="scanned_desc",
        )

        # Genuinely viable, real-sales-evidence leads a weak composite
        # score alone knocked below CONSIDER/BUY -- see
        # OpportunityEngine.analyse's LOW_SCORE branch (2026-09-03).
        # Same reasoning/merge pattern as low_confidence_records above.
        low_score_records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_LEADS, review_filter="low_score", sort="scanned_desc",
        )

        leads = []
        # De-dupes the scan+peak merge below (2026-09-02, fixing a real
        # Review Queue duplicate-items bug): "notable" and "peak" are two
        # SEPARATE list_latest() calls with review_filter="notable" vs
        # "peak" -- the code assumed these were mutually exclusive
        # (is_notable's roi/roi_90d checks "never see" a PEAK_WINDOW
        # record, per the comment above), but is_notable() only excludes
        # IGNORE/GATED, not PEAK_WINDOW, so a PEAK_WINDOW record whose
        # regular (non-peak) roi/roi_90d ALSO clears 25% with sales
        # evidence passes both filters -- confirmed live (a real
        # unreviewed record with roi=71.7%) showing up twice. Scoped to
        # just this scan+peak merge, not is_notable() itself, since that
        # function is also the Discord-notify gate in
        # brand_scan_service.py -- narrowing it there would silently
        # stop Discord pings for worthwhile PEAK_WINDOW finds, a
        # different behavior than what was reported.
        seen_scan_asins = set()

        for record in (
            list(scan_records) + list(peak_records) + list(low_confidence_records) + list(low_score_records)
        ):
            if record.asin in seen_scan_asins:
                continue

            lead = ReviewQueueService._scan_lead_dict(record)

            if record.recommendation == "PEAK_WINDOW":
                # "Worth the risk" gate -- see PEAK_WORTH_IT_ROI/
                # PEAK_WORTH_IT_SCORE above. peak_roi lives only in
                # report_json (no DB column), hence checking it here
                # rather than in ProductRepository.list_latest's
                # review_filter="peak" branch, which only has the ORM
                # record's own columns to filter on.
                peak_roi = lead["parsed_report"].get("peak_roi") or 0
                if peak_roi < PEAK_WORTH_IT_ROI or record.score < PEAK_WORTH_IT_SCORE:
                    continue

            seen_scan_asins.add(record.asin)
            leads.append(lead)

        seen_competitor_listing_ids = set()

        for entry in SellerWatchService.list_notable_buyable(limit=MAX_LEADS):
            leads.append(ReviewQueueService._competitor_lead_dict(entry))
            seen_competitor_listing_ids.add(entry["listing"].id)

        # Real historical EU/UK A2A evidence with no CURRENT buyable
        # offer (section 8's own worked example) -- previously invisible
        # to the Review Queue entirely, since list_notable_buyable only
        # ever looks at currently_buyable==True listings. See
        # SellerWatchService.list_historical_a2a_not_buyable's own
        # docstring. _item_views (below) is what actually routes these
        # to NEEDS_ATTENTION, never BUY_NOW.
        for entry in SellerWatchService.list_historical_a2a_not_buyable(limit=MAX_LEADS):
            if entry["listing"].id in seen_competitor_listing_ids:
                continue
            leads.append(ReviewQueueService._competitor_lead_dict(entry))

        leads.extend(ReviewQueueService._pending_leads(LEAD_MAIN_VERDICTS))

        ReviewQueueService._flag_conflicts(leads)

        # "when"/"score" fallbacks -- "when" can be None (report_json/
        # scanned_at gaps are pre-existing edge cases elsewhere too)
        # and datetime/None can't be compared directly during sort;
        # "score" defaults to 0 for the same reason if ever missing.
        key, reverse = SORT_OPTIONS.get(sort, SORT_OPTIONS["when_desc"])
        leads.sort(key=key, reverse=reverse)

        return leads

    @staticmethod
    def list_consider_leads(sort: str = "when_desc") -> list:
        """
        "Consider" tab (2026-08-19) -- CONSIDER-tier leads that are
        profitable (today OR the 90-day typical price -- "anything
        unprofitable both ways isn't likely to be bought") with some
        sign of real sales. Reviewing a row removes it from this list
        exactly like the main tab (list_leads() above) -- the
        difference is expectation, not mechanics: per the user's
        explicit request, this tab is NOT meant to be cleared to zero
        regularly. It's fine (expected, even) for a genuine backlog to
        sit here with newer leads landing on top, unlike the main tab
        which should get worked through.

        Excludes anything is_notable() already claims, so a lead never
        appears on both this tab and the main one (see
        ProductRepository.list_latest's "consider_worthwhile" filter
        for the exact criteria). Capped at MAX_CONSIDER_LEADS as a
        first-pass volume guard -- flagged by the user as something
        that may need retuning (the filter itself, or this cap) once
        real volume is visible after a restart.
        """
        records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_CONSIDER_LEADS,
            review_filter="consider_worthwhile", sort="scanned_desc",
        )

        leads = [ReviewQueueService._scan_lead_dict(record) for record in records]
        leads.extend(ReviewQueueService._pending_leads(LEAD_CONSIDER_VERDICTS))

        ReviewQueueService._flag_conflicts(leads)

        key, reverse = SORT_OPTIONS.get(sort, SORT_OPTIONS["when_desc"])
        leads.sort(key=key, reverse=reverse)

        return leads

    @staticmethod
    def _item_views(lead: dict) -> set:
        """
        Maps ONE per-source lead dict (see _scan_lead_dict/_lead_dict/
        the competitor branch above) onto the set of unified views it
        belongs to -- reuses each pipeline's OWN existing recommendation/
        verdict vocabulary and existing signals (freshness, conflict_note,
        historical A2A evidence, the VA first-pass flags from
        LeadAnalysisService), invents no new thresholds. Pure function of
        the dict, no DB access.

        Returns a SET, not a single value (2026-09-03, unified Review
        Queue build) -- BUY NOW is a universal view, not one competing
        bucket among several: a VA lead with a clean BUY verdict belongs
        in BOTH BUY_NOW (it's a strong current opportunity) AND
        VA_TO_REVIEW (you still haven't made your own final call on it)
        at once. Never empty.
        """
        views = set()
        recommendation = lead.get("recommendation")

        if lead["source"] == "lead":
            # VA_TO_REVIEW is a WORKFLOW view, true for every VA lead
            # still awaiting your decision -- NOT a verdict-quality
            # judgement, and NOT mutually exclusive with BUY_NOW. "It
            # does NOT mean Atlas doesn't think this is a BUY."
            views.add(QUEUE_PRIORITY_VA_TO_REVIEW)

            if recommendation == "BUY":
                # A VA BUY additionally earns BUY_NOW only if nothing
                # from the first pass says otherwise -- doesn't reject,
                # just also adds NEEDS_ATTENTION so it's not lost among
                # ordinary VA_TO_REVIEW backlog.
                if (
                    lead.get("already_in_inventory") or lead.get("buyability_blocker")
                    or lead.get("has_similar_rejection") or lead.get("conflict_note")
                ):
                    views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)
                else:
                    views.add(QUEUE_PRIORITY_BUY_NOW)
            elif recommendation != "WATCH":
                # AVOID never reaches here (_pending_leads only pulls
                # BUY/WATCH verdicts) -- an unexpected/missing verdict
                # is surfaced, not silently dropped.
                views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)

            return views

        # scan / competitor -- OpportunityEngine's own vocabulary.
        if lead.get("conflict_note"):
            views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)

        if recommendation == "BUY":
            if lead.get("freshness") in STALE_FRESHNESS_STATES:
                views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)
            else:
                views.add(QUEUE_PRIORITY_BUY_NOW)
        elif recommendation in BORDERLINE_RECOMMENDATIONS:
            views.add(QUEUE_PRIORITY_BORDERLINE)
        elif not views:
            views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)

        # Competitor-specific (section 8's own worked example): real
        # historical A2A evidence exists but today's recommendation
        # isn't BUY -- e.g. the original source dried up and nothing
        # else currently clears the bar either. That's a genuine "was a
        # real opportunity, needs a look" case, not silently nothing
        # (which is what happened before: SellerWatchService.
        # list_notable_buyable only ever surfaced currently_buyable
        # listings at all -- see list_leads()'s widened competitor
        # query). Added ALONGSIDE whatever else already applies, never
        # replacing it.
        if lead["source"] == "competitor" and recommendation != "BUY":
            hist = (lead.get("reasoning") or {}).get("historical_a2a_evidence") or {}
            if hist.get("eu_a2a") or hist.get("uk_a2a"):
                views.add(QUEUE_PRIORITY_NEEDS_ATTENTION)

        return views

    @staticmethod
    def _build_merged_item(asin: str, source_items: list) -> dict:
        """
        Combines every per-source dict for ONE ASIN into a single
        Review Queue item -- presentation/aggregation only (see
        merge_by_asin's own docstring: the underlying ProductRecord/
        SellerNewListing/Lead rows are never touched, and every
        original per-source dict survives intact in source_items).
        """
        per_item_views = [ReviewQueueService._item_views(item) for item in source_items]
        views = set().union(*per_item_views) if per_item_views else set()

        # queue_priority is the single STRONGEST view, for sorting/
        # anything that needs one value -- `views` (plural, below) is
        # the real membership: an item can be in BUY_NOW AND
        # VA_TO_REVIEW at once (section 2/3 -- "BUY NOW is a universal
        # view", not a competing bucket), so this is a convenience, not
        # the full picture.
        queue_priority = min(views, key=lambda v: _PRIORITY_RANK[v]) if views else QUEUE_PRIORITY_NEEDS_ATTENTION

        # The source item that actually contributed the winning view --
        # ties broken by source_items' own order (list_leads()'s
        # existing scan -> competitor -> lead merge order, not re-decided
        # here). This is the "strongest/current recommendation" (section
        # 1) the merged item leads with.
        primary = next(
            (item for item, item_views in zip(source_items, per_item_views) if queue_priority in item_views),
            source_items[0],
        )

        sources = [item["source"] for item in source_items]
        recommendations = {item["source"]: item.get("recommendation") for item in source_items}
        is_conflict = (
            len({r for r in recommendations.values() if r}) > 1
            or any(item.get("conflict_note") for item in source_items)
        )

        # Representative display numbers -- prefer a source item with a
        # real linked ProductRecord (scan/competitor both have one) over
        # a Lead's own VA/Keepa-estimate figures, since the ProductRecord
        # side is what OpportunityEngine/FeeEngine actually scored.
        display = next((item for item in source_items if item["source"] != "lead"), primary)

        historical_sourcing_evidence = {}
        for item in source_items:
            hist = ((item.get("reasoning") or {}).get("historical_a2a_evidence")) or {}
            if hist:
                historical_sourcing_evidence[item["source"]] = hist

        va_item = next((item for item in source_items if item["source"] == "lead"), None)
        competitor_item = next((item for item in source_items if item["source"] == "competitor"), None)

        return {
            "asin": asin,
            "category": ATTENTION_CATEGORY_SOURCING,
            "queue_priority": queue_priority,
            # Full view membership (2026-09-03, unified Review Queue
            # build) -- an item appears in EVERY view listed here, e.g.
            # ["BUY_NOW", "VA_TO_REVIEW"] for a clean VA BUY awaiting
            # your decision. queue_priority above is just views[0] --
            # kept as its own key for anything that only wants one value.
            "views": sorted(views, key=lambda v: _PRIORITY_RANK[v]),
            "sources": sources,
            "conflict": is_conflict,
            "conflict_note": next(
                (item.get("conflict_note") for item in source_items if item.get("conflict_note")), None
            ),
            "recommendations": recommendations,
            "strongest_recommendation": primary.get("recommendation"),
            "title": display.get("title") or primary.get("title"),
            "brand": display.get("brand") or primary.get("brand"),
            "score": display.get("score"),
            "confidence": display.get("confidence"),
            "sourcing_tag": display.get("sourcing_tag") or primary.get("sourcing_tag"),
            "sourcing_tag_source": display.get("sourcing_tag_source") or primary.get("sourcing_tag_source"),
            "best_source_marketplace": display.get("best_source_marketplace"),
            "best_source_cost_gbp": display.get("best_source_cost_gbp"),
            "buy_box_now": display.get("buy_box_now"),
            "profit": display.get("profit"),
            "roi": display.get("roi"),
            "freshness": display.get("freshness"),
            "review_reason_category": display.get("review_reason_category"),
            "historical_sourcing_evidence": historical_sourcing_evidence,
            "va_info": (
                {
                    "lead_id": va_item.get("lead_id"),
                    "lead_subsource": va_item.get("lead_subsource"),
                    "verdict": va_item.get("recommendation"),
                    "rationale": va_item.get("rationale"),
                    "already_in_inventory": va_item.get("already_in_inventory"),
                    "inventory_detail": va_item.get("inventory_detail"),
                    "similar_rejections": va_item.get("similar_rejections"),
                    "buyability_blocker": va_item.get("buyability_blocker"),
                }
                if va_item else None
            ),
            "competitor_info": (
                {
                    "listing_id": competitor_item.get("listing_id"),
                    "seller": competitor_item.get("seller"),
                    "currently_buyable": competitor_item.get("currently_buyable"),
                }
                if competitor_item else None
            ),
            "when": max((item.get("when") for item in source_items if item.get("when")), default=None),
            "source_items": source_items,
        }

    @staticmethod
    def merge_by_asin(leads: list) -> list:
        """
        Groups the ALREADY-BUILT per-source lead dicts (from
        list_leads()/list_consider_leads(), themselves entirely
        unchanged by this) by ASIN into ONE Review Queue item per ASIN
        (atlas-review-queue-backend-v1.md follow-up build, "ONE ASIN =
        ONE REVIEW QUEUE ITEM"). Presentation/aggregation only -- the
        underlying ProductRecord/SellerNewListing/Lead rows and their
        own review/decision columns are never touched or merged;
        source_items on each returned item keeps every original
        per-source dict intact, so nothing is discarded, only combined
        for display. Order-preserving (first-seen ASIN order).
        """
        by_asin: dict = {}
        order: list = []

        for lead in leads:
            asin = lead["asin"]
            if asin not in by_asin:
                by_asin[asin] = []
                order.append(asin)
            by_asin[asin].append(lead)

        return [ReviewQueueService._build_merged_item(asin, by_asin[asin]) for asin in order]

    @staticmethod
    def get_queue_item(asin: str) -> dict | None:
        """
        Efficient single-ASIN lookup (Command Centre UI build, 2026-09-03)
        -- list_queue_items() recomputes the ENTIRE unified queue
        (hundreds of rows across four review_filter passes plus both
        competitor queries) just to extract one item; measured at
        several seconds per call, far too slow for a detail panel
        opened on every row click. Reuses the EXACT SAME dict-builders
        (_scan_lead_dict/_competitor_lead_dict/_lead_dict),
        _flag_conflicts, and _build_merged_item as list_leads()/
        list_consider_leads() do -- no new dedup/priority/decision
        logic, purely scoped `WHERE asin = ...` queries in place of the
        full-table scans those methods need for a whole-queue view.

        Returns None if nothing is currently outstanding for this ASIN
        (already resolved, or never had a pending source record at all).
        """
        db = SessionLocal()
        try:
            source_dicts = []

            record = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin == asin, ProductRecord.review.is_(None))
                .order_by(ProductRecord.scanned_at.desc())
                .first()
            )
            if record:
                source_dicts.append(ReviewQueueService._scan_lead_dict(record))

            listings = (
                db.query(SellerNewListing, TrackedSeller, ProductRecord)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(
                    SellerNewListing.asin == asin, SellerNewListing.review.is_(None),
                    SellerNewListing.dismissed == False,
                )
                .all()
            )
            for listing, seller, competitor_record in listings:
                source_dicts.append(ReviewQueueService._competitor_lead_dict(
                    {"listing": listing, "record": competitor_record, "seller": seller}
                ))

            pending_leads = (
                db.query(Lead)
                .filter(Lead.asin == asin, Lead.status == "analyzed", Lead.decision.is_(None))
                .all()
            )
            for lead in pending_leads:
                source_dicts.append(ReviewQueueService._lead_dict(lead))
        finally:
            db.close()

        if not source_dicts:
            return None

        ReviewQueueService._flag_conflicts(source_dicts)
        return ReviewQueueService._build_merged_item(asin, source_dicts)

    @staticmethod
    def list_queue_items(sort: str = "when_desc") -> list:
        """
        The unified, deduplicated Review Queue -- BUY_NOW/VA_TO_REVIEW/
        BORDERLINE tier leads (list_leads() + list_consider_leads(),
        i.e. today's "main" and "Consider" tabs combined -- a CONSIDER-
        tier scan record and a BUY-tier competitor find for the same
        ASIN absolutely should merge into one item) merged by ASIN. Not
        wired into any route/template yet -- this is the new backend
        capability the next (UI) task will build the actual queue page
        against; /review-queue and review_queue.html are untouched.
        """
        leads = ReviewQueueService.list_leads(sort=sort) + ReviewQueueService.list_consider_leads(sort=sort)
        items = ReviewQueueService.merge_by_asin(leads)

        rank = {p: i for i, p in enumerate(QUEUE_PRIORITIES)}
        items.sort(key=lambda item: rank.get(item["queue_priority"], len(QUEUE_PRIORITIES)))

        return items

    @staticmethod
    def queue_priority_summary() -> dict:
        """
        Counts of the deduplicated queue by unified VIEW membership,
        plus how many raw source rows got merged away -- additive
        alongside count_summary()/consider_summary() (those existing
        counts/keys are left completely untouched so the Dashboard
        badges and anything else reading them can't regress; this is a
        NEW set of numbers for whatever UI work wires them in next).

        An item counted into MULTIPLE buckets is intentional (2026-09-03,
        unified Review Queue build, section 14's own worked example): a
        clean VA BUY counts toward BOTH buy_now AND va_to_review, because
        those are genuinely different questions ("is this worth buying"
        vs "have you made your final call on it") about the SAME one
        underlying item -- summing the per-view counts will therefore
        legitimately exceed unique_items whenever any item has more than
        one view, which is expected, not a bug.
        """
        raw_leads = ReviewQueueService.list_leads() + ReviewQueueService.list_consider_leads()
        items = ReviewQueueService.merge_by_asin(raw_leads)

        counts = {p: 0 for p in QUEUE_PRIORITIES}
        for item in items:
            for view in item["views"]:
                counts[view] += 1

        return {
            "unique_items": len(items),
            "raw_source_rows": len(raw_leads),
            "duplicates_merged": len(raw_leads) - len(items),
            **{p.lower(): counts[p] for p in QUEUE_PRIORITIES},
        }

    @staticmethod
    def resolve_item(asin: str, decision: str, reason: str | None = None, reason_category: str | None = None) -> dict:
        """
        ONE decision resolves every outstanding source record for this
        ASIN at once (unified Review Queue build, 2026-09-03 -- "I
        should NEVER have to review the same ASIN twice simply because
        it appeared in two queue views"). Reuses each source's OWN
        existing decision mechanism -- ProductRepository.set_review,
        SellerWatchService.set_review, apply_lead_decision, exactly the
        same functions /review/set, /review/decide, and the Competitors
        page's own review buttons already call -- so this can never
        drift from what clicking each item's own existing button would
        have done, and creates no new decision pathway.

        decision: one of DECISION_VALUES ("approved" | "rejected" |
        "oos" | "watch" | "need_more_info").

        Live-queries each source table directly for whatever is
        CURRENTLY outstanding for this ASIN -- never trusts a possibly-
        stale merged item computed earlier in the request/page render.
        A source is "outstanding" using the exact same tests the rest
        of this file already uses to decide what's pending (most recent
        ProductRecord with review IS NULL; every undismissed
        SellerNewListing with review IS NULL; every Lead with
        status="analyzed" and decision IS NULL) -- so this never
        re-decides something already resolved, e.g. it will never
        overwrite an old, already-reviewed ProductRecord just because a
        VA later happened to submit the same ASIN.

        Returns which rows were actually touched per source, so a
        caller can confirm nothing was silently skipped. Underlying
        rows keep their FULL history -- only their own review/decision
        columns change; nothing here deletes or merges a source record
        (section 13).
        """
        if decision not in ReviewQueueService.DECISION_VALUES:
            raise ValueError(f"Unknown decision: {decision!r} -- must be one of {ReviewQueueService.DECISION_VALUES}")

        scan_verdict = {v: k for k, v in ReviewQueueService._SCAN_DECISION_MAP.items()}[decision]
        resolved = {"scan": False, "competitor": [], "lead": []}

        db = SessionLocal()
        try:
            record = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin == asin)
                .order_by(ProductRecord.scanned_at.desc())
                .first()
            )
            scan_was_outstanding = bool(record and not record.review)

            listing_ids = [
                row.id for row in
                db.query(SellerNewListing.id)
                .filter(SellerNewListing.asin == asin, SellerNewListing.review.is_(None), SellerNewListing.dismissed == False)
                .all()
            ]

            lead_ids = [
                row.id for row in
                db.query(Lead.id)
                .filter(Lead.asin == asin, Lead.status == "analyzed", Lead.decision.is_(None))
                .all()
            ]
        finally:
            db.close()

        if scan_was_outstanding:
            ProductRepository.set_review(asin, scan_verdict, reason=reason, reason_category=reason_category)
            resolved["scan"] = True

        for listing_id in listing_ids:
            SellerWatchService.set_review(listing_id, scan_verdict, reason=reason, reason_category=reason_category)
            resolved["competitor"].append(listing_id)

        if lead_ids:
            db2 = SessionLocal()
            try:
                for lead_id in lead_ids:
                    lead = db2.get(Lead, lead_id)
                    if lead and lead.decision is None:  # re-check freshness under this fresh session
                        apply_lead_decision(lead, decision, reason, reason_category)
                        resolved["lead"].append(lead_id)
                db2.commit()
            finally:
                db2.close()

        # "oos"/"watch" on the SCAN side ALSO auto-adds to Watchlist,
        # mirroring /review/set's own existing single-item behaviour
        # (see app/routes/watchlist.py) -- reuses the exact same
        # ProductRepository.add_watch call, not a new mechanism.
        # "watch" included for the same reason apply_lead_decision
        # already does this for the Lead side (see its own docstring).
        if scan_was_outstanding and decision in ("oos", "watch"):
            last_check = ProductRepository.get_last_eu_check(asin)
            ProductRepository.add_watch(
                asin,
                title=last_check.title if last_check else "",
                brand=last_check.brand if last_check else "",
                note=(
                    "Amazon OOS at review -- watching for restock" if decision == "oos"
                    else "Marked WATCH at review -- keeping an eye on this"
                ),
            )

        return resolved

    @staticmethod
    def unresolve_item(asin: str, resolved: dict) -> dict:
        """
        Reverses exactly the rows a just-completed resolve_item call
        touched (Quick Reject/BUY undo toast, UI redesign pass,
        2026-09-03) -- takes the SAME `resolved` dict resolve_item
        returned, so it only ever reopens rows THIS action closed,
        never a fresh "whatever's outstanding right now" re-query that
        could accidentally reopen something else.

        Reuses set_review's own already-supported verdict=None ("clear")
        path for scan/competitor rows -- not a new capability, the exact
        same clear ProductRepository.set_review/SellerWatchService.
        set_review already offered before this build. For Lead rows
        (apply_lead_decision has no None path), this directly reverses
        the fields that function set, restoring status="analyzed" so
        the lead reappears in the pending queue exactly as it was
        before the decision.

        Caveat inherited from set_review itself: it targets "the most
        recent scan record for this ASIN" at undo time, not a specific
        row id -- if a background scan happened to land a NEW record
        for this exact ASIN in the few seconds the undo toast was up,
        this would clear that new record's (already-empty) review
        instead of reopening the one actually resolved. Accepted as a
        rare, harmless edge case rather than adding a new column to
        pin an exact row, same trade-off set_review already lives with
        everywhere else it's used.

        Only meaningful within the short window the UI's own undo
        toast is visible -- calling this after a later action has
        already touched the same rows again just clears/reopens them,
        same as any other review action would.
        """
        if resolved.get("scan"):
            ProductRepository.set_review(asin, None)

        for listing_id in resolved.get("competitor") or []:
            SellerWatchService.set_review(listing_id, None)

        lead_ids = resolved.get("lead") or []
        if lead_ids:
            db = SessionLocal()
            try:
                for lead_id in lead_ids:
                    lead = db.get(Lead, lead_id)
                    if lead:
                        lead.decision = None
                        lead.decision_reason = None
                        lead.decision_reason_category = None
                        lead.status = "analyzed"
                        lead.reviewed_at = None
                db.commit()
            finally:
                db.close()

        return {"ok": True}

    @staticmethod
    def consider_summary() -> dict:
        """
        Dashboard-facing counts for the Consider tab (2026-08-19):
        "today" (new leads whose latest scan landed today, UTC -- same
        boundary as Products' /products/today) and "total" (the whole
        current backlog, same population list_consider_leads()
        returns). Worth keeping these separate because, unlike the
        main tab, "total" is allowed to stay large -- "today" is what
        actually tells you whether anything NEW showed up.
        """
        today_records, _ = ProductRepository.list_latest(
            page=1, page_size=MAX_CONSIDER_LEADS,
            review_filter="consider_worthwhile", today_only=True,
        )

        return {
            "total": len(ReviewQueueService.list_consider_leads()),
            "today": len(today_records),
        }

    @staticmethod
    def count_summary() -> dict:
        """
        Categorized counts over the EXACT SAME leads list_leads()
        returns, so the Dashboard's badge is provably the same
        population as what /review-queue actually shows -- no
        separately-maintained query to drift out of sync.

        (Before 2026-08-19 the Dashboard computed its own
        "unreviewed_star_buys" directly against ProductRecord, which
        skipped the GATED/IGNORE guard is_notable() applies -- so a
        gated-brand or low-confidence product with sales evidence and
        25%+ ROI could inflate that count while never actually
        appearing in the Review Queue. Routing through list_leads()
        here closes that gap for good, and means there's nothing left
        to "reset" -- the count always reflects exactly what's
        outstanding right now.)

        Each lead is counted in exactly one bucket, in the same
        priority a person reading the page would use to describe it:
        competitor find > PEAK (risky) > LOW CONFIDENCE (risky, own
        reason) > verdict-checked BUY lead > scan BUY > star buy.
        "leads" (2026-08-24 nav consolidation) is counted separately
        from "buys" even though both currently render the same BUY
        badge -- a verdict-checked lead is an AI judgment call (Verdict
        Checker/Shortlist), not the same thing as OpportunityEngine's
        deterministic BUY recommendation, and collapsing them into one
        bucket would misrepresent the Dashboard's own "BUY
        recommendation" wording.

        low_confidence and low_score (2026-09-03) are their own buckets,
        not folded into star_buys -- before this each would have fallen
        into the catch-all `else` below and been miscounted as a
        full-trust star buy, which is exactly the wrong signal for tiers
        that exist specifically to flag "this needs your judgment on a
        confidence penalty / weak score first".
        """
        leads = ReviewQueueService.list_leads()

        star_buys = buys = peak = competitor = lead_buys = low_confidence = low_score = 0

        for lead in leads:
            if lead["source"] == "competitor":
                competitor += 1
            elif lead["source"] == "lead":
                lead_buys += 1
            elif lead["recommendation"] == "PEAK_WINDOW":
                peak += 1
            elif lead["recommendation"] == "LOW_CONFIDENCE":
                low_confidence += 1
            elif lead["recommendation"] == "LOW_SCORE":
                low_score += 1
            elif lead["recommendation"] == "BUY":
                buys += 1
            else:
                star_buys += 1

        return {
            "total": len(leads),
            "star_buys": star_buys,
            "buys": buys,
            "peak": peak,
            "low_confidence": low_confidence,
            "low_score": low_score,
            "competitor": competitor,
            "leads": lead_buys,
        }

    # "up"/"down" (ProductRecord/SellerNewListing's own vocabulary) ->
    # the Lead Queue's "approved"/"rejected" -- a shared 5-value
    # decision vocabulary for the merged history view. "oos"/"watch"/
    # "need_more_info" (the latter two added 2026-09-03, unified Review
    # Queue build -- see apply_lead_decision/resolve_item) already
    # match on both sides -- no separate word was needed for the scan/
    # competitor side of those three.
    _SCAN_DECISION_MAP = {
        "up": "approved", "down": "rejected", "oos": "oos",
        "watch": "watch", "need_more_info": "need_more_info",
    }
    DECISION_VALUES = ("approved", "rejected", "oos", "watch", "need_more_info")

    @staticmethod
    def _scan_history_dict(record) -> dict:
        return {
            "source": "scan",
            "lead_subsource": None,
            "asin": record.asin,
            "title": record.title,
            "brand": record.brand,
            "category_name": record.category_name,
            "buy_box_now": record.buy_box_now,
            "recommendation": record.recommendation,
            "decision": ReviewQueueService._SCAN_DECISION_MAP.get(record.review, record.review),
            "decision_reason": record.review_reason,
            "decision_reason_category": record.review_reason_category,
            # No dedicated "reviewed at" column on ProductRecord (only
            # scanned_at) -- reviewed_at_is_approx tells the template
            # to label this honestly rather than imply precision that
            # isn't there.
            "reviewed_at": record.scanned_at,
            "reviewed_at_is_approx": True,
            "detail_url": None,
        }

    @staticmethod
    def _competitor_history_dict(listing, record, seller) -> dict:
        return {
            "source": "competitor",
            "lead_subsource": None,
            "asin": listing.asin,
            "title": record.title if record else listing.asin,
            "brand": record.brand if record else "",
            "category_name": record.category_name if record else "",
            "buy_box_now": record.buy_box_now if record else 0,
            "recommendation": record.recommendation if record else None,
            "decision": ReviewQueueService._SCAN_DECISION_MAP.get(listing.review, listing.review),
            "decision_reason": listing.review_reason,
            "decision_reason_category": listing.review_reason_category,
            "reviewed_at": listing.detected_at,
            "reviewed_at_is_approx": True,
            "seller": seller,
            "detail_url": None,
        }

    @staticmethod
    def _lead_history_dict(lead) -> dict:
        metrics = {}
        if lead.keepa_metrics:
            try:
                metrics = json.loads(lead.keepa_metrics)
            except Exception:
                metrics = {}

        return {
            "source": "lead",
            "lead_subsource": lead.source,
            "asin": lead.asin,
            "title": metrics.get("title") or lead.asin,
            "brand": metrics.get("brand") or "",
            "category_name": metrics.get("category_name") or "",
            "buy_box_now": metrics.get("buy_box_now"),
            "recommendation": lead.verdict,
            "decision": lead.decision,
            "decision_reason": lead.decision_reason,
            "decision_reason_category": lead.decision_reason_category,
            "reviewed_at": lead.reviewed_at,
            "reviewed_at_is_approx": False,
            "detail_url": f"/review/{lead.id}",
        }

    @staticmethod
    def list_reviewed_history(decision_filter: str = "") -> dict:
        """
        Merged reviewed history across all three sources (nav
        consolidation fast-follow, 2026-08-24) -- previously Lead-only
        (see leads.py's old review_history_page). Products/Discovery/
        Watchlist's own thumbs-reviewed ProductRecord rows, buyable
        competitor detections, and reviewed Leads, normalized into one
        shape and one timeline. Counts are exact (COUNT queries, not
        loaded rows); the displayed list is capped at HISTORY_LIMIT
        per source before merging -- generous enough that a genuine
        "show me everything" browse never needs more, without loading
        an unbounded, ever-growing history table into memory.
        """
        db = SessionLocal()
        try:
            counts = {v: 0 for v in ReviewQueueService.DECISION_VALUES}

            # A reviewed competitor listing's underlying ProductRecord
            # gets counted via the SellerNewListing branch below, not
            # here -- same one-decision-one-row reasoning as the
            # covered_record_ids dedup further down, applied to the
            # totals too so the tab badges don't overcount.
            competitor_covered_ids = db.query(SellerNewListing.product_record_id).filter(
                SellerNewListing.review.isnot(None), SellerNewListing.product_record_id.isnot(None)
            )

            for raw, mapped in ReviewQueueService._SCAN_DECISION_MAP.items():
                counts[mapped] += (
                    db.query(ProductRecord)
                    .filter(ProductRecord.review == raw, ProductRecord.id.notin_(competitor_covered_ids))
                    .count()
                )
                counts[mapped] += db.query(SellerNewListing).filter(SellerNewListing.review == raw).count()

            for decision in ReviewQueueService.DECISION_VALUES:
                counts[decision] += db.query(Lead).filter(Lead.status == "reviewed", Lead.decision == decision).count()

            rows = []

            # Reviewing a competitor-sourced row marks BOTH its
            # SellerNewListing.review AND the underlying ProductRecord.
            # review in one click (see /review/set's own docstring) --
            # so the same physical decision would otherwise show up as
            # two separate history rows. Built first so the scan query
            # below can exclude whichever ProductRecords a competitor
            # listing already accounts for.
            listing_query = db.query(SellerNewListing).filter(SellerNewListing.review.isnot(None))
            if decision_filter in ReviewQueueService.DECISION_VALUES:
                raw = {v: k for k, v in ReviewQueueService._SCAN_DECISION_MAP.items()}[decision_filter]
                listing_query = listing_query.filter(SellerNewListing.review == raw)
            listings = listing_query.order_by(SellerNewListing.detected_at.desc()).limit(HISTORY_LIMIT).all()
            covered_record_ids = {listing.product_record_id for listing in listings if listing.product_record_id}

            for listing in listings:
                record = db.get(ProductRecord, listing.product_record_id) if listing.product_record_id else None
                seller = db.get(TrackedSeller, listing.tracked_seller_id)
                rows.append(ReviewQueueService._competitor_history_dict(listing, record, seller))

            scan_query = db.query(ProductRecord).filter(ProductRecord.review.isnot(None))
            if covered_record_ids:
                scan_query = scan_query.filter(ProductRecord.id.notin_(covered_record_ids))
            if decision_filter in ReviewQueueService.DECISION_VALUES:
                raw = {v: k for k, v in ReviewQueueService._SCAN_DECISION_MAP.items()}[decision_filter]
                scan_query = scan_query.filter(ProductRecord.review == raw)
            for record in scan_query.order_by(ProductRecord.scanned_at.desc()).limit(HISTORY_LIMIT).all():
                rows.append(ReviewQueueService._scan_history_dict(record))

            lead_query = db.query(Lead).filter(Lead.status == "reviewed")
            if decision_filter in ReviewQueueService.DECISION_VALUES:
                lead_query = lead_query.filter(Lead.decision == decision_filter)
            for lead in lead_query.order_by(Lead.reviewed_at.desc()).limit(HISTORY_LIMIT).all():
                rows.append(ReviewQueueService._lead_history_dict(lead))
        finally:
            db.close()

        rows.sort(key=lambda r: _sort_when({"when": r["reviewed_at"]}), reverse=True)
        rows = rows[:HISTORY_LIMIT]

        return {
            "rows": rows,
            "approved_count": counts["approved"],
            "rejected_count": counts["rejected"],
            "oos_count": counts["oos"],
            # Additive (2026-09-03, unified Review Queue build) -- existing
            # approved_count/rejected_count/oos_count/total_count keys are
            # untouched in shape; total_count now correctly sums all five
            # buckets instead of undercounting watch/need_more_info (which
            # would otherwise vanish from the total silently).
            "watch_count": counts["watch"],
            "need_more_info_count": counts["need_more_info"],
            "total_count": sum(counts.values()),
        }
