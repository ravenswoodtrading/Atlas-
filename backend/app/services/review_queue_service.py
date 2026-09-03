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
            "profit": record.profit,
            "roi": record.roi,
            "profit_90d": record.profit_90d,
            "roi_90d": record.roi_90d,
            "score": record.score,
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

        return {
            "source": "lead",
            "lead_subsource": lead.source,  # "manual" | "sheet" | "shortlist"
            "lead_id": lead.id,
            "asin": lead.asin,
            "title": metrics.get("title") or lead.asin,
            "brand": metrics.get("brand") or "",
            "best_source_marketplace": lead.source_detail or lead.sourcing_type or "",
            "best_source_cost_gbp": lead.va_cost_price or 0.0,
            "profit": profit or 0.0,
            "roi": roi or 0.0,
            "profit_90d": metrics.get("keepa_estimate_profit_90d") or 0.0,
            "roi_90d": metrics.get("keepa_estimate_roi_90d") or 0.0,
            "score": 0,
            "recommendation": lead.verdict,  # "BUY" | "WATCH" (AVOID never reaches here)
            "monthly_sales": metrics.get("monthly_sales") or 0,
            "when": lead.analyzed_at,
            "parsed_report": {},
            "reasoning": {},
            "rationale": lead.rationale,
            "sourcing_tag": lead.sourcing_type,
            "currently_buyable": False,
            "seller": None,
            "listing_id": 0,
            "conflict_note": None,
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

        for entry in SellerWatchService.list_notable_buyable(limit=MAX_LEADS):
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

            leads.append({
                "source": "competitor",
                "asin": listing.asin,
                "title": record.title,
                "brand": record.brand,
                "best_source_marketplace": record.best_source_marketplace,
                "best_source_cost_gbp": record.best_source_cost_gbp,
                "profit": record.profit,
                "roi": record.roi,
                "profit_90d": record.profit_90d,
                "roi_90d": record.roi_90d,
                "score": record.score,
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
            })

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
    # the Lead Queue's "approved"/"rejected" -- a shared 3-value
    # decision vocabulary for the merged history view. "oos" already
    # matches on both sides.
    _SCAN_DECISION_MAP = {"up": "approved", "down": "rejected", "oos": "oos"}

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
            counts = {"approved": 0, "rejected": 0, "oos": 0}

            # A reviewed competitor listing's underlying ProductRecord
            # gets counted via the SellerNewListing branch below, not
            # here -- same one-decision-one-row reasoning as the
            # covered_record_ids dedup further down, applied to the
            # totals too so the tab badges don't overcount.
            competitor_covered_ids = db.query(SellerNewListing.product_record_id).filter(
                SellerNewListing.review.isnot(None), SellerNewListing.product_record_id.isnot(None)
            )

            for raw, mapped in (("up", "approved"), ("down", "rejected"), ("oos", "oos")):
                counts[mapped] += (
                    db.query(ProductRecord)
                    .filter(ProductRecord.review == raw, ProductRecord.id.notin_(competitor_covered_ids))
                    .count()
                )
                counts[mapped] += db.query(SellerNewListing).filter(SellerNewListing.review == raw).count()

            for decision in ("approved", "rejected", "oos"):
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
            if decision_filter in ("approved", "rejected", "oos"):
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
            if decision_filter in ("approved", "rejected", "oos"):
                raw = {v: k for k, v in ReviewQueueService._SCAN_DECISION_MAP.items()}[decision_filter]
                scan_query = scan_query.filter(ProductRecord.review == raw)
            for record in scan_query.order_by(ProductRecord.scanned_at.desc()).limit(HISTORY_LIMIT).all():
                rows.append(ReviewQueueService._scan_history_dict(record))

            lead_query = db.query(Lead).filter(Lead.status == "reviewed")
            if decision_filter in ("approved", "rejected", "oos"):
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
            "total_count": counts["approved"] + counts["rejected"] + counts["oos"],
        }
