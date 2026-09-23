"""Keepa priority for Competitor Watch and the Scan Queue ceiling (2026-09-20):
  * the Scan Queue stops at a rolling-24h token ceiling and says so;
  * Revisit pool runs 5 a day, not 25;
  * a competitor pass takes a priority slot (automated ticks stand down only WHILE it waits);
  * its scheduler runs 3x a day from the last successful pass, restart-proof, and retries in
    minutes -- not 8 hours -- when it loses the scan lock.
Isolated in-memory SQLite and mocks -- no live database writes, Keepa or SP-API calls."""
import asyncio
import inspect
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main
from app.database.base import Base
from app.database.models import TokenUsageEvent
from app.services import scan_queue_service as sqs
from app.services import token_usage_service as tus
from app.services.keepa_priority import KeepaPriority, ScanBusyError
from app.services.revisit_pool_service import RevisitPoolService
from app.services.scan_coordinator import ScanCoordinator

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


class _Stop(Exception):
    """Raised by the patched sleep so a scheduler's endless loop can be observed for one turn."""


class TokenSpentSinceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        patcher = patch.object(tus, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def add(self, category, tokens, hours_ago, call_type="keepa_query"):
        with self.sessions() as db:
            db.add(TokenUsageEvent(category=category, call_type=call_type, marketplace="UK", asins_count=1,
                                   tokens=tokens, occurred_at=NOW - timedelta(hours=hours_ago)))
            db.commit()

    def test_sums_only_this_category_inside_the_window_and_only_real_spend(self):
        self.add("scan_queue", 100, 1)
        self.add("scan_queue", 40, 23.5)
        self.add("scan_queue", 200, 25)                                  # older than 24h
        self.add("scan_queue", 999, 1, call_type="sp_api_saved")         # an estimate, not spend
        self.add("competitor_watch", 50, 1)                              # someone else's
        self.assertEqual(tus.TokenUsageService.spent_since("scan_queue", 24), 140)
        self.assertEqual(tus.TokenUsageService.spent_since("scan_queue", 2), 100)
        self.assertEqual(tus.TokenUsageService.spent_since("nothing", 24), 0)

    def test_a_database_error_reads_as_zero_never_blocks_a_scan(self):
        with patch.object(tus, "SessionLocal", side_effect=RuntimeError("db gone")):
            self.assertEqual(tus.TokenUsageService.spent_since("scan_queue", 24), 0.0)


class ScanQueueCeilingTests(unittest.TestCase):
    def tick(self, spent, ceiling, paused=False):
        db = MagicMock()
        settings = SimpleNamespace(paused=paused)
        with patch.object(sqs, "SessionLocal", return_value=db), \
             patch.object(sqs, "SCAN_QUEUE_DAILY_TOKEN_CEILING", ceiling), \
             patch.object(sqs.ScanQueueService, "_get_settings", return_value=settings), \
             patch.object(sqs.ScanQueueService, "_next_round_robin_item", return_value=None) as pick, \
             patch.object(sqs.TokenUsageService, "spent_since", return_value=spent) as spent_since, \
             patch.object(sqs.ScanCoordinator, "try_acquire_for_automated_tick") as acquire:
            result = sqs.ScanQueueService.run_next_tick()
        return result, pick, spent_since, acquire

    def test_at_or_over_the_ceiling_the_tick_is_skipped_with_a_visible_reason(self):
        for spent in (30000, 41214):
            result, pick, spent_since, acquire = self.tick(spent=spent, ceiling=30000)
            self.assertIn("Daily token ceiling reached", result["skipped"])
            self.assertIn("30,000", result["skipped"])
            pick.assert_not_called()                                     # no brand is even chosen
            acquire.assert_not_called()                                  # and the scan lock is never touched
            spent_since.assert_called_once_with("scan_queue", 24)

    def test_under_the_ceiling_the_queue_carries_on_as_before(self):
        result, pick, *_ = self.tick(spent=29999, ceiling=30000)
        pick.assert_called_once()
        self.assertNotIn("ceiling", result["skipped"])

    def test_a_zero_ceiling_disables_it_and_does_not_even_measure_spend(self):
        result, pick, spent_since, _ = self.tick(spent=10 ** 9, ceiling=0)
        spent_since.assert_not_called()
        pick.assert_called_once()

    def test_paused_still_wins(self):
        result, pick, spent_since, _ = self.tick(spent=10 ** 9, ceiling=30000, paused=True)
        self.assertEqual(result, {"skipped": "paused"})
        spent_since.assert_not_called()


class RevisitLimitTests(unittest.TestCase):
    def test_daily_revisits_cut_from_25_to_5(self):
        self.assertEqual(RevisitPoolService.DEFAULT_DAILY_LIMIT, 5)

    def test_the_default_actually_reaches_the_batch_and_candidate_calls(self):
        for fn in (RevisitPoolService.run_batch, RevisitPoolService.get_candidates):
            self.assertEqual(inspect.signature(fn).parameters["limit"].default, 5, fn.__name__)


class PrioritySlotTests(unittest.TestCase):
    def setUp(self):
        # Never leave the process-wide waiter count dirty for another test.
        self.addCleanup(lambda: setattr(ScanCoordinator, "_high_priority_waiting", 0))

    def test_priority_is_raised_only_while_waiting_and_withdrawn_once_the_lock_is_held(self):
        seen = {}

        def fake_acquire(timeout=None):
            seen["while_waiting"] = KeepaPriority.has_pending()
            return True

        with patch.object(ScanCoordinator, "acquire_for_manual_scan", side_effect=fake_acquire), \
             patch.object(ScanCoordinator, "release_after_manual_scan") as release:
            with KeepaPriority.priority_slot(timeout=900):
                seen["inside"] = KeepaPriority.has_pending()      # the caller's own scans must not see it
        self.assertTrue(seen["while_waiting"])
        self.assertFalse(seen["inside"])
        self.assertFalse(KeepaPriority.has_pending())
        release.assert_called_once()

    def test_timing_out_raises_clears_the_signal_and_releases_nothing(self):
        with patch.object(ScanCoordinator, "acquire_for_manual_scan", return_value=False), \
             patch.object(ScanCoordinator, "release_after_manual_scan") as release:
            with self.assertRaises(ScanBusyError):
                with KeepaPriority.priority_slot(timeout=1):
                    self.fail("body must not run without the lock")
        self.assertFalse(KeepaPriority.has_pending())
        release.assert_not_called()

    def test_an_error_in_the_body_still_releases_the_lock(self):
        with patch.object(ScanCoordinator, "acquire_for_manual_scan", return_value=True), \
             patch.object(ScanCoordinator, "release_after_manual_scan") as release:
            with self.assertRaises(ValueError):
                with KeepaPriority.priority_slot(timeout=1):
                    raise ValueError("boom")
        release.assert_called_once()
        self.assertFalse(KeepaPriority.has_pending())

    def test_with_the_real_lock_the_pass_waits_its_turn_then_holds_it_alone(self):
        self.assertTrue(ScanCoordinator.try_acquire_for_automated_tick())      # a brand scan is mid-tick
        entered, finish = threading.Event(), threading.Event()
        inside = {}

        def competitor_pass():
            with KeepaPriority.priority_slot(timeout=10):
                inside["pending"] = KeepaPriority.has_pending()
                entered.set()
                finish.wait(5)

        worker = threading.Thread(target=competitor_pass, daemon=True)
        worker.start()
        for _ in range(200):                                                    # wait for it to queue up
            if KeepaPriority.has_pending():
                break
            threading.Event().wait(0.02)
        self.assertTrue(KeepaPriority.has_pending())          # in-flight scans yield; automated ticks decline
        ScanCoordinator.release_after_automated_tick()         # the brand scan finishes
        self.assertTrue(entered.wait(5))                        # ...and the competitor pass gets the lock
        self.assertFalse(inside["pending"])
        self.assertFalse(ScanCoordinator.try_acquire_for_automated_tick())      # it holds it: nobody else starts
        finish.set()
        worker.join(5)
        self.assertTrue(ScanCoordinator.try_acquire_for_automated_tick())      # released afterwards
        ScanCoordinator.release_after_automated_tick()

    def test_a_waiting_competitor_pass_makes_the_scan_queue_skip_its_tick(self):
        KeepaPriority.mark_active()
        try:
            self.assertFalse(ScanCoordinator.try_acquire_for_automated_tick())
        finally:
            KeepaPriority.mark_done()
        self.assertTrue(ScanCoordinator.try_acquire_for_automated_tick())      # signal withdrawn: free again
        ScanCoordinator.release_after_automated_tick()


class CompetitorSchedulerTests(unittest.TestCase):
    def run_one_turn(self, wait_seconds=0, pass_effect=None):
        sleep = AsyncMock(side_effect=_Stop)
        run_pass = MagicMock(return_value={"checked": 26, "new_listings": 3}, side_effect=pass_effect)
        with patch.object(main, "_seconds_until_due", return_value=wait_seconds) as due, \
             patch.object(main.asyncio, "sleep", sleep), \
             patch.object(main, "_run_competitor_pass", run_pass), \
             patch.object(main.ActivityLog, "mark_tick") as mark:
            with self.assertRaises(_Stop):
                asyncio.run(main._seller_watch_scheduler())
        return sleep, run_pass, mark, due

    def test_it_runs_three_times_a_day_measured_from_the_last_successful_pass(self):
        _, _, _, due = self.run_one_turn()
        due.assert_called_once_with("seller_watch", 8.0)

    def test_a_restart_soon_after_a_pass_does_not_run_another(self):
        sleep, run_pass, mark, _ = self.run_one_turn(wait_seconds=5 * 3600)
        run_pass.assert_not_called()
        mark.assert_not_called()
        self.assertEqual(sleep.await_args.args[0], main.SELLER_WATCH_POLL_SECONDS)

    def test_a_successful_pass_is_recorded_with_its_summary(self):
        sleep, run_pass, mark, _ = self.run_one_turn()
        run_pass.assert_called_once()
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[:2], ("seller_watch", main.SELLER_WATCH_INTERVAL_SECONDS))
        self.assertEqual(mark.call_args.args[2], "26 seller(s), 3 new")

    def test_losing_the_lock_retries_in_five_minutes_not_eight_hours(self):
        sleep, _, mark, _ = self.run_one_turn(pass_effect=ScanBusyError("busy"))
        mark.assert_not_called()                                   # not a success: still due
        self.assertEqual(sleep.await_args.args[0], main.SELLER_WATCH_BUSY_RETRY_SECONDS)
        self.assertLess(main.SELLER_WATCH_BUSY_RETRY_SECONDS, main.SELLER_WATCH_INTERVAL_SECONDS)

    def test_any_other_failure_backs_off_without_recording_success(self):
        sleep, _, mark, _ = self.run_one_turn(pass_effect=RuntimeError("keepa down"))
        mark.assert_not_called()
        self.assertEqual(sleep.await_args.args[0], main.SELLER_WATCH_FAILURE_RETRY_SECONDS)

    def test_the_pass_itself_takes_a_priority_slot_for_up_to_the_batch_wait(self):
        slot = MagicMock()
        slot.return_value.__enter__ = MagicMock(return_value=None)
        slot.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(main.KeepaPriority, "priority_slot", slot), \
             patch.object(main.SellerWatchService, "run_check", return_value={"checked": 1}) as run_check:
            self.assertEqual(main._run_competitor_pass(), {"checked": 1})
        slot.assert_called_once_with(timeout=main.BATCH_WAIT_SECONDS)
        run_check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
