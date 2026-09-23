"""Keepa token priority (2026-09-20): the weekly safety-net job no longer re-runs at every server
start, and the Scan Queue holds back tokens for Replen's daily batch until it is done.
Isolated in-memory SQLite and mocks -- no live database writes, Keepa or SP-API calls."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main
from app.database.base import Base
from app.database.models import ReplenA2AItem, SchedulerStatus
from app.services import replen_a2a_service as svc
from app.services import scan_queue_service as sqs
from app.services.replen_a2a_service import ReplenA2AService, daily_quota

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)


class SecondsUntilDueTests(_Db):
    def setUp(self):
        super().setUp()
        patcher = patch("app.database.database.SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, name, hours_ago):
        with self.sessions() as db:
            db.add(SchedulerStatus(name=name, interval_seconds=86400,
                                   last_tick_at=(NOW - timedelta(hours=hours_ago)) if hours_ago is not None else None))
            db.commit()

    def test_never_run_is_due_now(self):
        self.assertEqual(main._seconds_until_due("weekly_recheck", 20), 0)

    def test_row_with_no_successful_tick_is_due_now(self):
        self.tick("weekly_recheck", None)
        self.assertEqual(main._seconds_until_due("weekly_recheck", 20), 0)

    def test_recent_success_is_not_due_and_says_how_long_is_left(self):
        self.tick("weekly_recheck", 1)               # a restart one hour after the last run
        remaining = main._seconds_until_due("weekly_recheck", 20)
        self.assertAlmostEqual(remaining / 3600, 19, delta=0.05)

    def test_old_success_is_due(self):
        self.tick("weekly_recheck", 21)
        self.assertEqual(main._seconds_until_due("weekly_recheck", 20), 0)

    def test_each_scheduler_is_judged_on_its_own_row(self):
        self.tick("weekly_recheck", 1)
        self.tick("replen_a2a", 30)
        self.assertGreater(main._seconds_until_due("weekly_recheck", 20), 0)
        self.assertEqual(main._seconds_until_due("replen_a2a", 22), 0)

    def test_replen_due_check_shares_the_helper(self):
        self.tick("replen_a2a", 3)
        self.assertFalse(main._replen_a2a_is_due())
        with self.sessions() as db:
            db.query(SchedulerStatus).one().last_tick_at = NOW - timedelta(hours=23)
            db.commit()
        self.assertTrue(main._replen_a2a_is_due())


class _Stop(Exception):
    """Raised by the patched sleep so the scheduler's endless loop can be observed for one turn."""


class WeeklyJobStartupTests(unittest.TestCase):
    def run_one_turn(self, wait_seconds, acquired=False):
        sleep = AsyncMock(side_effect=_Stop)
        with patch.object(main, "_seconds_until_due", return_value=wait_seconds), \
             patch.object(main.asyncio, "sleep", sleep), \
             patch.object(main.ScanCoordinator, "try_acquire_for_automated_tick", return_value=acquired) as acquire, \
             patch.object(main.ScanCoordinator, "release_after_automated_tick"), \
             patch.object(main.WatchlistService, "check_stale") as watchlist, \
             patch.object(main.WatchlistService, "prune_stale_auto_adds") as prune, \
             patch.object(main.SellerWatchService, "archive_stale_oa_investigate", return_value=0):
            with self.assertRaises(_Stop):
                asyncio.run(main._weekly_recheck_scheduler())
        return sleep, acquire, watchlist, prune

    def test_restart_soon_after_a_run_does_not_run_the_job(self):
        sleep, acquire, watchlist, prune = self.run_one_turn(wait_seconds=19 * 3600)
        acquire.assert_not_called()
        watchlist.assert_not_called()
        prune.assert_not_called()

    def test_it_waits_in_bounded_steps_not_one_huge_sleep(self):
        sleep, *_ = self.run_one_turn(wait_seconds=19 * 3600)
        self.assertEqual(sleep.await_args.args[0], main.WEEKLY_RECHECK_WAIT_POLL_SECONDS)
        sleep, *_ = self.run_one_turn(wait_seconds=90)
        self.assertEqual(sleep.await_args.args[0], 90)               # the last stretch is exact

    def test_when_due_it_goes_for_the_scan_lock_and_retries_in_a_minute_if_busy(self):
        sleep, acquire, watchlist, _ = self.run_one_turn(wait_seconds=0, acquired=False)
        acquire.assert_called_once()
        watchlist.assert_not_called()                                # lock busy: nothing ran
        self.assertEqual(sleep.await_args.args[0], 60)

    def test_when_due_and_lock_is_free_the_job_runs(self):
        sleep = AsyncMock(side_effect=_Stop)
        sync_result = {"checked": 0, "stale": 0}
        with patch.object(main, "_seconds_until_due", return_value=0), \
             patch.object(main.asyncio, "sleep", sleep), \
             patch.object(main.ScanCoordinator, "try_acquire_for_automated_tick", return_value=True), \
             patch.object(main.ScanCoordinator, "release_after_automated_tick") as release, \
             patch.object(main.WatchlistService, "check_stale", return_value=sync_result) as watchlist, \
             patch.object(main.ReviewQueueService, "recheck_stale_items", return_value={}), \
             patch.object(main.ReviewQueueService, "recheck_weak_leads", return_value={}), \
             patch.object(main.RevisitPoolService, "run_batch", return_value={}), \
             patch.object(main.SellerWatchService, "reclassify_all", return_value={}), \
             patch.object(main.ActivityLog, "mark_tick") as mark, \
             patch.object(main.WatchlistService, "prune_stale_auto_adds"), \
             patch.object(main.SellerWatchService, "archive_stale_oa_investigate", return_value=0):
            with self.assertRaises(_Stop):
                asyncio.run(main._weekly_recheck_scheduler())
        watchlist.assert_called_once()
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[0], "weekly_recheck")
        release.assert_called_once()                                 # lock always released


class ReplenReserveTests(_Db):
    def setUp(self):
        super().setUp()
        patcher = patch.object(svc, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, n, priced_recently=0, ignored=0, hours_ago=1):
        with self.sessions() as db:
            for i in range(n):
                db.add(ReplenA2AItem(
                    asin=f"B{i:09d}", ignored=i < ignored,
                    last_checked_at=(NOW - timedelta(hours=hours_ago)) if ignored <= i < ignored + priced_recently else None))
            db.commit()

    def test_quota_helper(self):
        self.assertEqual([daily_quota(n) for n in (0, 1, 3, 4, 5, 40, 249)], [0, 1, 1, 1, 2, 10, 63])

    def test_nothing_priced_holds_back_the_whole_days_batch(self):
        self.seed(40)                                                # quota 10 ASINs
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 10 * svc.REPLEN_TOKENS_PER_ASIN)

    def test_reserve_shrinks_as_the_day_is_priced_and_vanishes_when_done(self):
        self.seed(40, priced_recently=6)
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 4 * svc.REPLEN_TOKENS_PER_ASIN)
        with self.sessions() as db:
            for row in db.query(ReplenA2AItem).filter(ReplenA2AItem.last_checked_at.is_(None)).limit(4):
                row.last_checked_at = NOW - timedelta(hours=2)
            db.commit()
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 0)

    def test_old_pricing_does_not_count_towards_today(self):
        self.seed(40, priced_recently=10, hours_ago=svc.DAILY_WINDOW_HOURS + 1)
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 10 * svc.REPLEN_TOKENS_PER_ASIN)

    def test_hidden_items_are_not_part_of_the_quota(self):
        self.seed(48, ignored=8)                                     # 40 live items -> quota 10
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 10 * svc.REPLEN_TOKENS_PER_ASIN)

    def test_reserve_is_capped(self):
        self.seed(400)                                               # quota 100 -> 1,000 tokens uncapped
        self.assertEqual(ReplenA2AService.token_reserve_needed(), svc.REPLEN_RESERVE_CAP)

    def test_empty_list_or_a_broken_database_asks_for_nothing_and_never_raises(self):
        self.assertEqual(ReplenA2AService.token_reserve_needed(), 0)
        with patch.object(svc, "SessionLocal", side_effect=RuntimeError("db gone")):
            self.assertEqual(ReplenA2AService.token_reserve_needed(), 0)

    def test_the_reserve_matches_what_run_daily_will_actually_price(self):
        """Same quota on both sides, or the reserve would protect a different number of ASINs than run."""
        self.seed(101)
        with self.sessions() as db:
            batch = svc.select_batch(db.query(ReplenA2AItem).all())
        self.assertEqual(len(batch) * svc.REPLEN_TOKENS_PER_ASIN, ReplenA2AService.token_reserve_needed())


class ScanQueueReserveWiringTests(unittest.TestCase):
    def scan_with_reserve(self, needed):
        item = SimpleNamespace(id=1, brand="acme", category_ids="", next_page=0, status="active")
        result = {"error": "Only 3 Keepa tokens left", "completed_asins": [], "asins_scanned": 0, "count": 0}
        scanner_class = MagicMock(return_value=MagicMock(scan=MagicMock(return_value=result)))
        with patch.object(sqs, "BrandScanService", scanner_class), \
             patch.object(sqs.ReplenA2AService, "token_reserve_needed", return_value=needed), \
             patch.object(sqs.ScanCoordinator, "progress"):
            db = MagicMock()
            db.get.return_value = None
            sqs.ScanQueueService._execute_scan_for_item(db, item)
        return scanner_class.call_args.kwargs

    def test_scan_queue_keeps_replen_s_tokens_off_limits_while_replen_has_work(self):
        kwargs = self.scan_with_reserve(needed=610)
        self.assertEqual(kwargs["token_reserve"], 610)
        self.assertEqual(kwargs["usage_category"], "scan_queue")

    def test_scan_queue_falls_back_to_its_old_reserve_once_replen_is_done(self):
        from app.services.brand_scan_service import WEEKLY_SAFETY_NET_RESERVE
        self.assertEqual(self.scan_with_reserve(needed=0)["token_reserve"], WEEKLY_SAFETY_NET_RESERVE)
        self.assertEqual(self.scan_with_reserve(needed=5)["token_reserve"], WEEKLY_SAFETY_NET_RESERVE)


if __name__ == "__main__":
    unittest.main()
