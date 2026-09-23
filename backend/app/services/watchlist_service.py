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

    # "UK recovery watch" (2026-09-20, Tamara): a product with a live EU source that is NOT viable
    # today only because the UK price has fallen, yet would be a great buy (25%+ ROI, real sales) at
    # the UK's normal price. Not buyable now, so it isn't a lead -- but worth watching in case the UK
    # recovers or the EU cost drops again. maybe_auto_watch can't catch it: it skips anything already
    # profitable at the 90-day price. The marker prefix still starts with "Auto-added:" so the
    # Watchlist shows its Auto badge and prune_stale_auto_adds can retire it, but it tells the prune
    # step to judge these on TODAY's profit only -- see prune_stale_auto_adds.
    RECOVERY_WATCH_MARKER = "Auto-added: UK recovery watch"
    RECOVERY_WATCH_ROI_90D_MIN = FeeEngine.OA_TARGET_ROI_PCT
    # Never recovery-watch something already buyable, or that can't be bought at all.
    RECOVERY_WATCH_EXCLUDED_RECOMMENDATIONS = ("BUY", "GATED", "FREQUENTLY_RETURNED")
    # Sanity caps. A UK price 40%+ below its 90-day typical isn't a temporary dip -- it usually means
    # the "typical" is inflated or the EU listing is a different pack/variant -- and an ROI above 200%
    # at the typical price is too good to be true (same ceiling as IMPLAUSIBLE_AUTO_PROMOTE_ROI_PCT in
    # oa_source_discovery_service). Measured 2026-09-20 on the live data: without these, 8 of 74
    # candidates were such cases (e.g. UK GBP 11.82 vs a "typical" 48.28, 312% ROI).
    RECOVERY_WATCH_MAX_UK_DROP_PCT = 40.0
    RECOVERY_WATCH_MAX_ROI_90D = 200.0

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
    def maybe_auto_watch_recovery(product: Product, recommendation: str = ""):
        """
        Adds a "UK recovery watch" (see RECOVERY_WATCH_MARKER) when ALL of: not already buyable
        (recommendation isn't BUY), a live EU source, not viable today (ROI under the viable floor),
        but at least RECOVERY_WATCH_ROI_90D_MIN% at the UK's 90-day typical price, real sales
        evidence, the UK Buy Box genuinely below its typical price, and not already watched.

        Rides along on every scan like maybe_auto_watch, so it never raises.
        """
        try:
            if recommendation in WatchlistService.RECOVERY_WATCH_EXCLUDED_RECOMMENDATIONS:
                return
            if not (product.best_source_cost_gbp or 0) > 0:
                return
            if (product.roi or 0) >= OpportunityEngine.MIN_VIABLE_ROI:
                return                                   # viable today: the normal queues handle it
            if not (
                WatchlistService.RECOVERY_WATCH_ROI_90D_MIN <= (product.roi_90d or 0)
                <= WatchlistService.RECOVERY_WATCH_MAX_ROI_90D
            ):
                return
            has_sales = (
                (product.monthly_sales or 0) > 0
                or (product.sales_drops_30d or 0) >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD
            )
            if not has_sales:
                return
            if not (product.buy_box_now and product.buy_box_90d) or product.buy_box_now >= product.buy_box_90d:
                return

            if product.asin in ProductRepository.get_watched_asins():
                return

            drop_pct = 100 * (1 - product.buy_box_now / product.buy_box_90d)
            if drop_pct > WatchlistService.RECOVERY_WATCH_MAX_UK_DROP_PCT:
                return
            note = (
                f"{WatchlistService.RECOVERY_WATCH_MARKER} -- UK Buy Box is GBP {product.buy_box_now:.2f}, "
                f"{drop_pct:.0f}% below its 90-day typical GBP {product.buy_box_90d:.2f}. With "
                f"{product.best_source_marketplace} at GBP {product.best_source_cost_gbp:.2f} that's "
                f"{product.roi:.0f}% ROI today but ~{product.roi_90d:.0f}% if the UK recovers. "
                f"Not buyable now; watching for the UK to recover or the EU cost to drop."
            )
            ProductRepository.add_watch(product.asin, title=product.title, brand=product.brand, note=note)

        except Exception as exc:
            print(f"WatchlistService.maybe_auto_watch_recovery failed for {product.asin}: {exc}")

    # Same day-count bar as OpportunityEngine.PEAK_MIN_VIABLE_DAYS_90D
    # (reused, not duplicated) -- applied here to the EU-source-cost-drop
    # signal instead of the UK-sale-price-peak one it was built for.
    LEAD_SOURCE_DROP_MIN_VIABLE_DAYS_90D = OpportunityEngine.PEAK_MIN_VIABLE_DAYS_90D

    @staticmethod
    def maybe_auto_watch_lead(
        asin: str, title: str, brand: str, source_marketplace: str, source_drop_evidence: dict,
    ):
        """
        The Lead-pipeline mirror of maybe_auto_watch above -- extends
        the same "not profitable now, but genuinely was recently"
        concept to VA-sheet/Verdict-Checker leads, which previously had
        NO such tracking at all: an AVOID-verdict A2A lead just died
        with nothing watching it for a future source-cost drop, unlike
        a scan-sourced equivalent. Added 2026-09-03, Tamara: "lots of my
        VA leads are A2A drops that are not profitable today but on a
        price drop so may be profitable soon."

        Takes source_drop_evidence as an already-computed dict (see
        VerdictService.compute_source_drop_evidence) rather than calling
        VerdictService itself -- that module already needs to import
        THIS one to make the call in the first place (see
        VerdictService.compute_metrics), so this file must never import
        VerdictService back, or it's circular. The caller is responsible
        for only calling this when the lead ISN'T already profitable at
        today's/90d cost -- see compute_metrics for where that's judged.

        Unlike maybe_auto_watch's single 90-day-low snapshot, this
        REQUIRES real day-count evidence -- Tamara's own explicit
        requirement, directly informed by prune_stale_auto_adds' own
        hard-won lesson (94% of the watchlist was auto-added on a single
        snapshot, 88% never went profitable). A single low day proves
        almost nothing; LEAD_SOURCE_DROP_MIN_VIABLE_DAYS_90D of them is
        real evidence.

        "Profitable now should be higher rated than one that may be
        profitable in x days" (Tamara's own framing) is enforced by the
        caller never running this check at all once a lead is already
        viable -- a genuinely profitable-now lead gets a real BUY/WATCH
        verdict and goes straight to the Review Queue; this speculative,
        lower-trust signal only ever applies to what's LEFT once that's
        ruled out, and lands on a completely separate page (Watchlist),
        never the Review Queue itself.

        No-op (never raises), same reason as maybe_auto_watch: rides
        along on every Verdict Checker / VA-sheet check and must never
        break that over a watchlist side-effect.
        """
        try:
            if source_drop_evidence["viable_days_90d"] < WatchlistService.LEAD_SOURCE_DROP_MIN_VIABLE_DAYS_90D:
                return

            if asin in ProductRepository.get_watched_asins():
                return

            note = (
                f"Auto-added: {source_marketplace} cost has been low enough to be viable on "
                f"{source_drop_evidence['viable_days_90d']} of the last 90 days (best case ~GBP "
                f"{source_drop_evidence['best_case_profit']:.2f} profit, "
                f"{source_drop_evidence['best_case_roi']:.0f}% ROI at GBP "
                f"{source_drop_evidence['recent_low_source_cost_gbp']:.2f}) -- not profitable at "
                f"today's cost. (VA/Verdict Checker lead)"
            )

            ProductRepository.add_watch(
                asin, title=title, brand=brand, note=note,
                viable_days_90d=source_drop_evidence["viable_days_90d"],
            )

        except Exception as exc:
            print(f"WatchlistService.maybe_auto_watch_lead failed for {asin}: {exc}")

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

                # A recovery watch is chosen BECAUSE it is profitable at the 90-day price, so
                # judging it on that would keep it forever. Judge it on today's profit only.
                summary = ProductRepository.get_recheck_summary_since(
                    w.asin, w.watched_at,
                    today_only=(w.note or "").startswith(WatchlistService.RECOVERY_WATCH_MARKER),
                )

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
