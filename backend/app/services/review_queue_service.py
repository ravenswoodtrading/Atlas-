import json
from datetime import datetime

from app.services.product_repository import ProductRepository
from app.services.seller_watch_service import SellerWatchService

# Comfortably covers any realistic unreviewed-notable backlog -- same
# pragmatic-cap pattern SellerWatchService.list_detections already
# uses (limit=200) rather than a genuinely unbounded fetch.
MAX_LEADS = 500

# A PEAK_WINDOW lead already cleared OpportunityEngine.MIN_VIABLE_ROI
# (10%) at the peak price just to be tagged PEAK_WINDOW at all -- that
# floor means "not worthless", not "worth the risk". Buying against a
# recent price peak is inherently riskier than a normal BUY/CONSIDER
# (you're betting the price gets back there again), so the Review
# Queue holds PEAK_WINDOW leads to a higher bar before surfacing them:
# either the peak ROI is genuinely strong, or the underlying score is
# already at CONSIDER-tier quality despite failing the current/90d-avg
# price gate that kept it out of CONSIDER/BUY. Below both, it's not
# worth seeing -- this is what let weak, low-score peak leads (e.g.
# two Wiha ASINs barely over the 10% floor) leak into the queue.
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
            parsed_report = {}
            if record.report_json:
                try:
                    parsed_report = json.loads(record.report_json)
                except Exception:
                    parsed_report = {}

            if record.recommendation == "PEAK_WINDOW":
                # "Worth the risk" gate -- see PEAK_WORTH_IT_ROI/
                # PEAK_WORTH_IT_SCORE above. peak_roi lives only in
                # report_json (no DB column), hence checking it here
                # rather than in ProductRepository.list_latest's
                # review_filter="peak" branch, which only has the ORM
                # record's own columns to filter on.
                peak_roi = parsed_report.get("peak_roi") or 0
                if peak_roi < PEAK_WORTH_IT_ROI and record.score < PEAK_WORTH_IT_SCORE:
                    continue

            leads.append({
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
            })

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
