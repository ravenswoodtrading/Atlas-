import json
from datetime import datetime

from app.services.product_repository import ProductRepository
from app.services.seller_watch_service import SellerWatchService

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
# higher bar before surfacing them: either the peak ROI is genuinely
# strong, or the underlying score is already at CONSIDER-tier quality
# despite failing the current/90d-avg price gate that kept it out of
# CONSIDER/BUY. Below both, it's not worth seeing -- this is what let
# weak, low-score peak leads (e.g. two Wiha ASINs barely over the old
# 10% floor) leak into the queue.
PEAK_WORTH_IT_ROI = 35
PEAK_WORTH_IT_SCORE = 65

# Mirrors ProductRepository.SORT_OPTIONS' shape (lambda + reverse flag)
# but keyed on the merged lead dict, since scan and competitor leads
# don't share a single ORM model to sort in the DB -- this list is
# already fully in memory by the time sorting happens.
SORT_OPTIONS = {
    "when_desc": (lambda lead: lead["when"] or datetime.min, True),
    "when_asc": (lambda lead: lead["when"] or datetime.min, False),
    "score_desc": (lambda lead: lead["score"] or 0, True),
    "score_asc": (lambda lead: lead["score"] or 0, False),
}


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
            "sourcing_tag": None,
            "currently_buyable": False,
            "seller": None,
            "listing_id": 0,
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

        leads = []

        for record in list(scan_records) + list(peak_records):
            lead = ReviewQueueService._scan_lead_dict(record)

            if record.recommendation == "PEAK_WINDOW":
                # "Worth the risk" gate -- see PEAK_WORTH_IT_ROI/
                # PEAK_WORTH_IT_SCORE above. peak_roi lives only in
                # report_json (no DB column), hence checking it here
                # rather than in ProductRepository.list_latest's
                # review_filter="peak" branch, which only has the ORM
                # record's own columns to filter on.
                peak_roi = lead["parsed_report"].get("peak_roi") or 0
                if peak_roi < PEAK_WORTH_IT_ROI and record.score < PEAK_WORTH_IT_SCORE:
                    continue

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
                "sourcing_tag": listing.sourcing_tag,
                "currently_buyable": listing.currently_buyable,
                "seller": seller,
                "listing_id": listing.id,
            })

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
        competitor find > PEAK (risky) > BUY > star buy.
        """
        leads = ReviewQueueService.list_leads()

        star_buys = buys = peak = competitor = 0

        for lead in leads:
            if lead["source"] == "competitor":
                competitor += 1
            elif lead["recommendation"] == "PEAK_WINDOW":
                peak += 1
            elif lead["recommendation"] == "BUY":
                buys += 1
            else:
                star_buys += 1

        return {
            "total": len(leads),
            "star_buys": star_buys,
            "buys": buys,
            "peak": peak,
            "competitor": competitor,
        }
