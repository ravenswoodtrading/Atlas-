from dataclasses import replace
from datetime import datetime, timedelta, timezone

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

    # An auto-added watch has to be at least this old, AND have been
    # genuinely rechecked at least PRUNE_MIN_RECHECKS separate times
    # since it was added, before prune_stale_auto_adds will consider
    # removing it -- see that method's docstring for the reasoning. A
    # 2026-08-21 review found the watchlist was 94% auto-added and 88%
    # had never gone profitable either way in up to 23 days of
    # rechecking; these two numbers are meant to give every auto-add a
    # fair, repeated chance to prove itself before it's judged dead
    # weight -- not to clear the list out aggressively.
    PRUNE_AFTER_DAYS = 14
    PRUNE_MIN_RECHECKS = 2

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

        scanner = BrandScanService(usage_category="watchlist")
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

    @staticmethod
    def prune_stale_auto_adds() -> dict:
        """
        Removes an auto-added watch once it's had a fair, repeated
        chance to prove the bet it was added on (a real recurring EU
        cost low) and hasn't: at least PRUNE_AFTER_DAYS since it was
        added, at least PRUNE_MIN_RECHECKS genuinely separate Keepa
        rechecks in that window (so a stretch where Atlas wasn't
        running, or nobody visited /watchlist and the weekly job
        hadn't caught it yet, doesn't get mistaken for "proof" of
        anything), and NOT ONE of those rechecks ever found it
        profitable today or at the 90-day typical price.

        Added 2026-08-21 -- a review found the watchlist was 94%
        auto-added (WatchlistService.maybe_auto_watch runs off the
        tail of every scan anywhere in the app) and 88% of the whole
        list had never gone profitable either way in up to 23 days,
        while every one of those items still gets fully re-priced
        across every EU marketplace every ~48h. This is what actually
        clears that dead weight out instead of it being re-scanned
        (at real token cost) indefinitely.

        Deliberately scoped to auto-added watches ONLY -- checked via
        the same "Auto-added:" note prefix the UI already uses to show
        the "Auto" badge (see watchlist.html). A manual "Watch" click
        or an OOS-restock watch (note starts "Amazon OOS at review")
        is the user's own explicit choice and is never silently
        removed here, no matter how long it sits there.

        Meant to be called from a slow (daily-or-slower) background
        tick, same as check_stale -- see main.py's
        _weekly_recheck_scheduler. Never raises, for the same reason
        as maybe_auto_watch: a side-effect cleanup job must never take
        down the scheduler tick it rides along on.
        """
        try:
            watched = ProductRepository.list_watched()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=WatchlistService.PRUNE_AFTER_DAYS)
            ).replace(tzinfo=None)

            pruned = []

            for w in watched:
                if not (w.note or "").startswith("Auto-added:"):
                    continue

                if w.watched_at > cutoff:
                    continue  # hasn't had its fair chance yet

                summary = ProductRepository.get_recheck_summary_since(w.asin, w.watched_at)

                if summary["recheck_count"] < WatchlistService.PRUNE_MIN_RECHECKS:
                    continue  # not enough real rechecks to call this proven either way

                if summary["ever_profitable"]:
                    continue  # the bet paid off at least once -- keep watching it

                ProductRepository.remove_watch(w.asin)
                pruned.append(w.asin)

            if pruned:
                preview = ", ".join(pruned[:10]) + ("..." if len(pruned) > 10 else "")
                ActivityLog.record(
                    "watchlist_prune",
                    f"removed {len(pruned)} auto-added watch(es) that never went profitable "
                    f"in {WatchlistService.PRUNE_AFTER_DAYS}+ days: {preview}",
                )

            return {"pruned": len(pruned), "asins": pruned}

        except Exception as exc:
            print(f"WatchlistService.prune_stale_auto_adds failed: {exc}")
            return {"pruned": 0, "asins": [], "error": str(exc)}
