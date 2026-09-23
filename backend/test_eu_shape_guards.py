"""Two guards (2026-09-20, Tamara):
  1. A product with a live, VIABLE EU source is never an "OA lead" -- including the case where the UK price
     has fallen too, so today's ROI looks poor but the same EU cost is fine at the UK's normal price.
  2. A great-but-not-buyable "UK recovery" product is auto-added to the Watchlist, and retired by the prune step on
     TODAY's profit only (otherwise it would look "profitable" forever).
Isolated in-memory SQLite and mocks -- no live database writes, no Keepa calls."""
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services import product_repository as pr
from app.services import seller_watch_service as sws
from app.services.brand_scan_service import BrandScanService
from app.services.product_repository import ProductRepository
from app.services.seller_watch_service import SellerWatchService
from app.services.watchlist_service import WatchlistService

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def rec(**kw):
    base = dict(best_source_cost_gbp=20.0, roi=0.0, roi_90d=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


class HasViableEuSourceTests(unittest.TestCase):
    def test_no_eu_source_is_never_eu_shaped(self):
        self.assertFalse(SellerWatchService.has_viable_eu_source(rec(best_source_cost_gbp=0, roi=90, roi_90d=90)))
        self.assertFalse(SellerWatchService.has_viable_eu_source(rec(best_source_cost_gbp=None, roi=90)))

    def test_viable_today_or_only_at_the_90_day_uk_price(self):
        self.assertTrue(SellerWatchService.has_viable_eu_source(rec(roi=30, roi_90d=10)))
        self.assertTrue(SellerWatchService.has_viable_eu_source(rec(roi=6.5, roi_90d=34.1)))   # the "UK also fell" case

    def test_the_bar_is_inclusive_and_uses_the_classifiers_own_constant(self):
        bar = sws.RECENT_VIABLE_ROI_PCT
        self.assertEqual(bar, 17.0)
        self.assertTrue(SellerWatchService.has_viable_eu_source(rec(roi=bar)))
        self.assertTrue(SellerWatchService.has_viable_eu_source(rec(roi_90d=bar)))
        self.assertFalse(SellerWatchService.has_viable_eu_source(rec(roi=bar - 0.1, roi_90d=bar - 0.1)))

    def test_unprofitable_or_missing_roi_values_are_not_viable(self):
        self.assertFalse(SellerWatchService.has_viable_eu_source(rec(roi=-20, roi_90d=-5)))
        self.assertFalse(SellerWatchService.has_viable_eu_source(rec(roi=None, roi_90d=None)))


class OaQueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        patcher = patch.object(sws, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            db.add(TrackedSeller(seller_id="ASELLER", nickname="Seller"))
            db.commit()

    def add(self, asin, tag="OA / unclear", dismissed=False, review=None, **record):
        fields = dict(asin=asin, title=asin, buy_box_now=40.0, category_name="Toys & Games", fba_fee=3.5,
                      best_source_cost_gbp=0.0, roi=0.0, roi_90d=0.0, recommendation="IGNORE")
        fields.update(record)
        with self.sessions() as db:
            product = ProductRecord(**fields)
            db.add(product)
            db.flush()
            db.add(SellerNewListing(tracked_seller_id=1, asin=asin, detected_at=NOW, product_record_id=product.id,
                                    sourcing_tag=tag, dismissed=dismissed, review=review))
            db.commit()

    def queue(self):
        return sorted(e["listing"].asin for e in SellerWatchService.list_oa_worth_investigating(limit=100))

    def test_genuine_oa_products_stay_in_the_queue(self):
        self.add("B0NOEUSRC0")                                                      # no EU source at all
        self.add("B0EUUNPROF", best_source_cost_gbp=30.0, roi=5, roi_90d=8)         # EU source, but it doesn't pay
        self.assertEqual(self.queue(), ["B0EUUNPROF", "B0NOEUSRC0"])

    def test_a_product_viable_at_todays_price_is_kept_out(self):
        self.add("B0VIABLE00", best_source_cost_gbp=20.0, roi=34, roi_90d=30)
        self.assertEqual(self.queue(), [])

    def test_the_uk_also_fell_example_is_kept_out(self):
        """DE down 10%, UK down 20%: 6.5% ROI today, 34% at the UK's normal price. Tagged OA / unclear."""
        self.add("B0UKALSOFL", best_source_cost_gbp=19.8, roi=6.5, roi_90d=34.1, buy_box_now=32.0)
        self.assertEqual(self.queue(), [])

    def test_the_boundary(self):
        self.add("B0JUSTOVER", best_source_cost_gbp=20.0, roi=1, roi_90d=17.0)
        self.add("B0JUSTUNDR", best_source_cost_gbp=20.0, roi=1, roi_90d=16.9)
        self.assertEqual(self.queue(), ["B0JUSTUNDR"])

    def test_other_queue_rules_still_apply(self):
        self.add("B0DISMISSD", dismissed=True)
        self.add("B0REVIEWED", review="down")
        self.add("B0EUA2ATAG", tag="EU A2A")
        self.add("B0KEPTONE0")
        self.assertEqual(self.queue(), ["B0KEPTONE0"])

    def test_the_filter_applies_to_every_view_built_on_the_queue(self):
        source = inspect.getsource(SellerWatchService.list_oa_worth_investigating)
        self.assertIn("has_viable_eu_source", source)


def product(**kw):
    """A 'UK recovery' candidate unless overridden: UK 32 vs typical 40, DE cost 19.80 -> 6.5% today, 34% at 40."""
    base = dict(asin="B0RECOVERY", title="Some Product", brand="Brand", best_source_cost_gbp=19.8,
                best_source_marketplace="DE", roi=6.5, roi_90d=34.1, buy_box_now=32.0, buy_box_90d=40.0,
                monthly_sales=80, sales_drops_30d=0)
    base.update(kw)
    return SimpleNamespace(**base)


class RecoveryWatchTests(unittest.TestCase):
    def run_watch(self, prod, recommendation="IGNORE", watched=()):
        with patch.object(ProductRepository, "get_watched_asins", return_value=set(watched)), \
             patch.object(ProductRepository, "add_watch") as add:
            WatchlistService.maybe_auto_watch_recovery(prod, recommendation)
        return add

    def test_the_example_is_watched_with_a_note_that_explains_it(self):
        add = self.run_watch(product())
        add.assert_called_once()
        args, kwargs = add.call_args
        self.assertEqual(args[0], "B0RECOVERY")
        note = kwargs["note"]
        self.assertTrue(note.startswith(WatchlistService.RECOVERY_WATCH_MARKER))
        self.assertTrue(note.startswith("Auto-added:"))               # keeps the Auto badge and the prune rule
        for fragment in ("32.00", "20% below", "40.00", "DE", "19.80", "6% ROI today", "34% if the UK recovers"):
            self.assertIn(fragment, note)

    def test_never_when_it_is_already_a_buy_or_cannot_be_bought(self):
        for recommendation in ("BUY", "GATED", "FREQUENTLY_RETURNED"):
            self.run_watch(product(), recommendation).assert_not_called()

    def test_other_recommendations_do_qualify(self):
        for recommendation in ("IGNORE", "CONSIDER", "PEAK_WINDOW", "LOW_SCORE", "WATCH", ""):
            self.run_watch(product(), recommendation).assert_called_once()

    def test_needs_a_live_eu_source(self):
        self.run_watch(product(best_source_cost_gbp=0)).assert_not_called()
        self.run_watch(product(best_source_cost_gbp=None)).assert_not_called()

    def test_not_when_it_is_already_viable_today(self):
        floor = WatchlistService_floor()
        self.run_watch(product(roi=floor)).assert_not_called()
        self.run_watch(product(roi=floor - 0.1)).assert_called_once()

    def test_needs_25_percent_at_the_uk_typical_price(self):
        bar = WatchlistService.RECOVERY_WATCH_ROI_90D_MIN
        self.assertEqual(bar, 25.0)
        self.run_watch(product(roi_90d=bar)).assert_called_once()
        self.run_watch(product(roi_90d=bar - 0.1)).assert_not_called()

    def test_sanity_caps_a_collapse_or_an_absurd_spread_is_not_a_recovery(self):
        top = WatchlistService.RECOVERY_WATCH_MAX_ROI_90D
        self.run_watch(product(roi_90d=top)).assert_called_once()
        self.run_watch(product(roi_90d=top + 1)).assert_not_called()             # too good to be true
        cap = WatchlistService.RECOVERY_WATCH_MAX_UK_DROP_PCT
        self.run_watch(product(buy_box_now=40.0 * (1 - cap / 100))).assert_called_once()        # exactly at the cap
        self.run_watch(product(buy_box_now=40.0 * (1 - cap / 100) - 0.5)).assert_not_called()   # deeper: not a dip
        self.run_watch(product(buy_box_now=11.82, buy_box_90d=48.28, roi_90d=312)).assert_not_called()   # a real live example

    def test_needs_real_sales_evidence(self):
        self.run_watch(product(monthly_sales=0, sales_drops_30d=0)).assert_not_called()
        self.run_watch(product(monthly_sales=0, sales_drops_30d=11)).assert_not_called()
        self.run_watch(product(monthly_sales=0, sales_drops_30d=ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD)).assert_called_once()
        self.run_watch(product(monthly_sales=1)).assert_called_once()

    def test_the_uk_must_actually_be_below_its_typical_price(self):
        self.run_watch(product(buy_box_now=40.0)).assert_not_called()
        self.run_watch(product(buy_box_now=45.0)).assert_not_called()
        self.run_watch(product(buy_box_now=0)).assert_not_called()
        self.run_watch(product(buy_box_90d=0)).assert_not_called()

    def test_already_watched_is_left_alone(self):
        self.run_watch(product(), watched={"B0RECOVERY"}).assert_not_called()

    def test_it_never_raises_because_it_rides_on_every_scan(self):
        with patch.object(ProductRepository, "get_watched_asins", side_effect=RuntimeError("db gone")):
            WatchlistService.maybe_auto_watch_recovery(product(), "IGNORE")           # must not raise
        WatchlistService.maybe_auto_watch_recovery(SimpleNamespace(asin="X"), "IGNORE")  # missing fields: swallowed

    def test_it_is_wired_into_the_scan_after_the_recommendation_exists(self):
        source = inspect.getsource(BrandScanService.scan)
        self.assertIn("maybe_auto_watch_recovery(product, report.recommendation)", source)
        self.assertLess(source.index("OpportunityEngine.analyse(product)"),
                        source.index("maybe_auto_watch_recovery"))


def WatchlistService_floor():
    from app.services.opportunity_engine import OpportunityEngine
    return float(OpportunityEngine.MIN_VIABLE_ROI)


class RecoveryWatchPruneTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        patcher = patch.object(pr, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def scans(self, asin, *rows):
        with self.sessions() as db:
            for i, (profit, profit_90d) in enumerate(rows):
                db.add(ProductRecord(asin=asin, profit=profit, profit_90d=profit_90d,
                                     scanned_at=NOW - timedelta(days=1, hours=i)))
            db.commit()

    def test_today_only_ignores_the_90_day_profit(self):
        self.scans("B0RECOVERY", (-3.0, 9.0), (-2.5, 9.5))
        since = NOW - timedelta(days=30)
        normal = ProductRepository.get_recheck_summary_since("B0RECOVERY", since)
        today = ProductRepository.get_recheck_summary_since("B0RECOVERY", since, today_only=True)
        self.assertEqual((normal["recheck_count"], normal["ever_profitable"]), (2, True))     # old behaviour, unchanged
        self.assertEqual((today["recheck_count"], today["ever_profitable"]), (2, False))

    def test_today_only_still_notices_a_genuine_recovery(self):
        self.scans("B0RECOVERY", (-3.0, 9.0), (4.0, 9.5))
        summary = ProductRepository.get_recheck_summary_since("B0RECOVERY", NOW - timedelta(days=30), today_only=True)
        self.assertTrue(summary["ever_profitable"])

    def prune(self, watches, summary):
        removed = []
        with patch.object(ProductRepository, "list_watched", return_value=watches), \
             patch.object(ProductRepository, "get_recheck_summary_since", return_value=summary) as get_summary, \
             patch.object(ProductRepository, "remove_watch", side_effect=removed.append), \
             patch("app.services.watchlist_service.ActivityLog.record"):
            WatchlistService.prune_stale_auto_adds()
        return removed, get_summary

    def watch(self, asin, note):
        return SimpleNamespace(asin=asin, note=note, watched_at=NOW - timedelta(days=WatchlistService.PRUNE_AFTER_DAYS + 3))

    def test_recovery_watches_are_judged_on_todays_profit_and_other_auto_adds_are_not(self):
        recovery = self.watch("B0RECOVERY", WatchlistService.RECOVERY_WATCH_MARKER + " -- UK is down")
        ordinary = self.watch("B0ORDINARY", "Auto-added: DE cost has dropped to GBP 10 before")
        _, get_summary = self.prune([recovery, ordinary], {"recheck_count": 3, "ever_profitable": False})
        flags = {call.args[0]: call.kwargs["today_only"] for call in get_summary.call_args_list}
        self.assertEqual(flags, {"B0RECOVERY": True, "B0ORDINARY": False})

    def test_a_recovery_watch_that_never_recovers_is_eventually_removed(self):
        removed, _ = self.prune([self.watch("B0RECOVERY", WatchlistService.RECOVERY_WATCH_MARKER + " -- x")],
                                {"recheck_count": 3, "ever_profitable": False})
        self.assertEqual(removed, ["B0RECOVERY"])

    def test_a_recovery_watch_that_recovered_is_kept_and_manual_watches_are_never_touched(self):
        removed, _ = self.prune([self.watch("B0RECOVERY", WatchlistService.RECOVERY_WATCH_MARKER + " -- x"),
                                 self.watch("B0MANUAL00", "Amazon OOS at review -- watching for restock")],
                                {"recheck_count": 3, "ever_profitable": True})
        self.assertEqual(removed, [])
        removed, _ = self.prune([self.watch("B0MANUAL00", "Amazon OOS at review -- watching for restock")],
                                {"recheck_count": 9, "ever_profitable": False})
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
