from dataclasses import replace

from app.models.product import Product
from app.services.brand_scan_service import BrandScanService
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.services.product_repository import ProductRepository
from app.services.activity_log import ActivityLog


class WatchlistService:
    """
    Auto-adds products to the existing Watchlist when they aren't
    profitable at today's EU cost but WOULD be at their recent 90-day
    low -- a real, recurring spread that's just temporarily narrowed,
    not one that never existed. Also runs the weekly safety-net rescan
    for anything on the Watchlist that hasn't been checked by anyone
    (manually or otherwise) recently.
    """

    # Matches OpportunityEngine's own bar for "worth acting on" --
    # deliberately reusing that, not inventing a new score cutoff, so
    # "decent" here means the exact same thing it means everywhere
    # else in the app.
    AUTO_WATCH_RECOMMENDATIONS = {"CONSIDER", "BUY"}

    @staticmethod
    def maybe_auto_watch(product: Product, category_name: str = ""):
        """
        No-op unless ALL of: not already profitable today or at the
        90d-typical price, a real EU 90-day low was found, the ASIN
        isn't already watched, and the HYPOTHETICAL scenario (today's
        UK price, EU cost at its 90-day low instead of today's) scores
        CONSIDER or BUY under the normal OpportunityEngine logic.

        Never raises -- this rides along on every real scan (see
        BrandScanService.scan Step 5) and must never break the scan
        itself over a watchlist side-effect.
        """
        try:
            if product.profit > 0 or product.profit_90d > 0:
                return

            if not product.best_source_cost_min_90d_gbp:
                return

            if product.asin in ProductRepository.get_watched_asins():
                return

            hypothetical = replace(
                product, best_source_cost_gbp=product.best_source_cost_min_90d_gbp
            )
            fees = FeeEngine.calculate(hypothetical, category_name=category_name)
            hypothetical.profit = fees.profit
            hypothetical.roi = fees.roi
            hypothetical.margin = fees.margin
            hypothetical.profit_90d = fees.profit_90d
            hypothetical.roi_90d = fees.roi_90d
            hypothetical.margin_90d = fees.margin_90d
            # Keep peak figures consistent with this hypothetical's
            # swapped EU cost too -- otherwise they'd carry over stale
            # from `product`'s real cost via dataclasses.replace, and
            # OpportunityEngine.analyse(hypothetical) below reads them
            # directly off the product it's given.
            hypothetical.profit_peak = fees.profit_peak
            hypothetical.roi_peak = fees.roi_peak
            hypothetical.margin_peak = fees.margin_peak

            report = OpportunityEngine.analyse(hypothetical)

            if report.recommendation not in WatchlistService.AUTO_WATCH_RECOMMENDATIONS:
                return

            effective_roi = max(hypothetical.roi, hypothetical.roi_90d)
            note = (
                f"Auto-added: {product.best_source_marketplace} cost has dropped to "
                f"GBP {product.best_source_cost_min_90d_gbp:.2f} before (vs "
                f"GBP {product.best_source_cost_gbp:.2f} now) -- would be ~{effective_roi:.0f}% ROI, "
                f"scored {report.score}/100 ({report.recommendation})."
            )

            ProductRepository.add_watch(
                product.asin, title=product.title, brand=product.brand, note=note
            )

        except Exception as exc:
            print(f"WatchlistService.maybe_auto_watch failed for {product.asin}: {exc}")

    @staticmethod
    def check_stale(since_hours: int = 24 * 7):
        """
        Weekly safety net: rescans watched ASINs that haven't been
        scanned by ANYONE (this page, another campaign, anything) in
        the last `since_hours` -- so a background tick doesn't
        re-spend tokens on items the user already refreshed manually
        by just visiting /watchlist.

        Caller is responsible for ScanCoordinator (see main.py's
        scheduler) -- this method doesn't acquire it itself, same
        convention as BrandScanService.scan().
        """
        watched = ProductRepository.list_watched()

        if not watched:
            return {"checked": 0, "total": 0, "stale": 0}

        recently_scanned = ProductRepository.get_recently_scanned_asins(since_hours)
        stale_asins = [w.asin for w in watched if w.asin not in recently_scanned]

        if not stale_asins:
            return {"checked": 0, "total": len(watched), "stale": 0}

        scanner = BrandScanService()
        result = scanner.scan(
            "watchlist-weekly", asins=stale_asins, limit=len(stale_asins), force_rescan=True,
        )

        ActivityLog.record(
            "watchlist_check",
            f"automated: {result.get('asins_scanned') or 0}/{len(stale_asins)} stale items checked",
        )

        return {
            "checked": result.get("asins_scanned") or 0,
            "total": len(watched),
            "stale": len(stale_asins),
            "error": result.get("error"),
        }
