"""Command Centre load-time fixes (2026-09-21): the summary counts moved into one SQL statement must equal the old
load-everything-and-count version, and the purchasing snapshot must refresh in the background instead of making one
page load per hour wait for Google Sheets. In-memory SQLite, no network."""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ProductRecord
from app.routes import dashboard as dash
from app.services import product_repository as pr
from app.services.product_repository import ProductRepository

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def rec(asin, recommendation, days_ago, profit=5.0, **kw):
    return ProductRecord(asin=asin, title="t", brand="b", recommendation=recommendation, profit=profit,
                         scanned_at=NOW - timedelta(days=days_ago), **kw)


def reference_stats(sessions):
    """The OLD implementation (load every record, keep the newest per ASIN, count in Python) -- the yardstick."""
    with sessions() as db:
        recent = db.query(ProductRecord).order_by(ProductRecord.scanned_at.desc(), ProductRecord.id.desc()).all()
    seen, latest = set(), []
    for r in recent:
        if r.asin in seen:
            continue
        seen.add(r.asin)
        latest.append(r)
    cutoff = NOW - timedelta(days=7)
    return {
        "total_scanned": len(latest),
        "total_profitable": sum(1 for r in latest if r.profit > 0),
        "total_buy": sum(1 for r in latest if r.recommendation == "BUY"),
        "total_consider": sum(1 for r in latest if r.recommendation == "CONSIDER"),
        "buy_this_week": sum(1 for r in latest if r.recommendation == "BUY" and r.scanned_at and r.scanned_at >= cutoff),
        "consider_this_week": sum(1 for r in latest if r.recommendation == "CONSIDER" and r.scanned_at and r.scanned_at >= cutoff),
    }


class SummaryStatsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        p = patch.object(pr, "SessionLocal", self.sessions)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def add(self, *records):
        with self.sessions() as db:
            db.add_all(records)
            db.commit()

    def test_it_matches_the_old_implementation_on_awkward_data(self):
        self.add(
            rec("B0AAAAAAA1", "BUY", 30), rec("B0AAAAAAA1", "CONSIDER", 10), rec("B0AAAAAAA1", "IGNORE", 1, profit=-2.0),  # newest wins
            rec("B0AAAAAAA2", "BUY", 2), rec("B0AAAAAAA3", "BUY", 20, profit=-1.0), rec("B0AAAAAAA4", "CONSIDER", 3),
            rec("B0AAAAAAA5", "CONSIDER", 9), rec("B0AAAAAAA6", "WATCH", 0),
            rec("B0AAAAAAA7", "BUY", 6.9), rec("B0AAAAAAA8", "BUY", 7.1),      # either side of the 7-day line
            rec("B0AAAAAAAZ", "WATCH", 4, profit=0.0),                          # zero profit is NOT profitable
        )
        same_moment = NOW - timedelta(days=3)
        self.add(ProductRecord(asin="B0AAAAAAA9", title="t", brand="b", recommendation="BUY", profit=5.0, scanned_at=same_moment),
                 ProductRecord(asin="B0AAAAAAA9", title="t", brand="b", recommendation="CONSIDER", profit=5.0, scanned_at=same_moment))
        self.assertEqual(ProductRepository.get_summary_stats(), reference_stats(self.sessions))

    def test_the_numbers_are_the_expected_ones(self):
        self.add(rec("B0AAAAAAA1", "BUY", 30), rec("B0AAAAAAA1", "CONSIDER", 1), rec("B0AAAAAAA2", "BUY", 2),
                 rec("B0AAAAAAA3", "BUY", 20, profit=-1.0))
        self.assertEqual(ProductRepository.get_summary_stats(), {
            "total_scanned": 3, "total_profitable": 2, "total_buy": 2, "total_consider": 1,
            "buy_this_week": 1, "consider_this_week": 1,
        })

    def test_an_empty_table_is_all_zeros(self):
        self.assertEqual(ProductRepository.get_summary_stats(), {
            "total_scanned": 0, "total_profitable": 0, "total_buy": 0, "total_consider": 0,
            "buy_this_week": 0, "consider_this_week": 0,
        })


GOOD = {"available": True, "error": None, "spend": 1, "month_label": "September 2026", "updated_at": "09:00"}
FAILED = {"available": False, "error": "Google unavailable", "spend": 0, "month_label": "September 2026", "updated_at": "10:00"}


class PurchasingSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(dash._purchasing_snapshot_cache)
        self.addCleanup(lambda: dash._purchasing_snapshot_cache.update(self.saved))
        dash._purchasing_snapshot_cache.update({"created_at": None, "value": None})
        dash._purchasing_refreshing = False
        self.builds = 0

    def stale(self, value):
        dash._purchasing_snapshot_cache.update(
            {"created_at": datetime.now() - dash.PURCHASING_CACHE_TTL - timedelta(minutes=1), "value": value})

    def wait_for_refresh(self, timeout=5.0):
        end = time.time() + timeout
        while dash._purchasing_refreshing and time.time() < end:
            time.sleep(0.01)
        self.assertFalse(dash._purchasing_refreshing, "background refresh did not finish")

    def builder(self, result, gate=None):
        def build():
            self.builds += 1
            if gate is not None:
                gate.wait(5)
            return dict(result)
        return build

    def test_a_fresh_snapshot_is_served_without_rebuilding(self):
        dash._purchasing_snapshot_cache.update({"created_at": datetime.now(), "value": GOOD})
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(FAILED)):
            self.assertIs(dash._purchasing_snapshot(), GOOD)
        self.assertEqual(self.builds, 0)

    def test_the_first_ever_load_builds_inline_and_caches(self):
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(GOOD)):
            self.assertEqual(dash._purchasing_snapshot()["spend"], 1)
            dash._purchasing_snapshot()
        self.assertEqual(self.builds, 1)

    def test_a_stale_snapshot_is_served_immediately_and_refreshed_behind_the_page(self):
        old = dict(GOOD, spend=1)
        self.stale(old)
        gate = threading.Event()                                   # the rebuild is "slow": blocked until we release it
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(dict(GOOD, spend=2), gate)):
            started = time.time()
            served = dash._purchasing_snapshot()
            self.assertLess(time.time() - started, 1.0)            # the page did NOT wait for the rebuild
            self.assertIs(served, old)
            gate.set()
            self.wait_for_refresh()
        self.assertEqual(dash._purchasing_snapshot_cache["value"]["spend"], 2)
        self.assertLess(datetime.now() - dash._purchasing_snapshot_cache["created_at"], timedelta(seconds=5))

    def test_only_one_refresh_runs_at_a_time(self):
        self.stale(GOOD)
        gate = threading.Event()
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(GOOD, gate)):
            for _ in range(10):
                dash._purchasing_snapshot()
            gate.set()
            self.wait_for_refresh()
        self.assertEqual(self.builds, 1)

    def test_a_failed_refresh_keeps_the_last_good_numbers_and_retries_soon(self):
        self.stale(GOOD)
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(FAILED)):
            dash._purchasing_snapshot()
            self.wait_for_refresh()
        self.assertIs(dash._purchasing_snapshot_cache["value"], GOOD)                     # not replaced by the error
        age = datetime.now() - dash._purchasing_snapshot_cache["created_at"]
        self.assertGreater(age, dash.PURCHASING_CACHE_TTL - dash.PURCHASING_RETRY_AFTER_FAILURE - timedelta(seconds=5))
        self.assertLess(age, dash.PURCHASING_CACHE_TTL)                                   # due again within minutes, not an hour

    def test_a_background_prewarm_fills_an_empty_cache(self):
        """What server start does, so the first page load after a restart doesn't build it inline."""
        with patch.object(dash, "_build_purchasing_snapshot", self.builder(GOOD)):
            dash._refresh_purchasing_snapshot_in_background()
            self.wait_for_refresh()
            self.assertEqual(dash._purchasing_snapshot()["spend"], 1)      # served from the cache, no second build
        self.assertEqual(self.builds, 1)

    def test_a_crash_inside_the_refresh_never_wedges_it(self):
        self.stale(GOOD)
        with patch.object(dash, "_build_purchasing_snapshot", side_effect=RuntimeError("boom")):
            dash._purchasing_snapshot()
            self.wait_for_refresh()
        self.assertIs(dash._purchasing_snapshot_cache["value"], GOOD)
        self.assertFalse(dash._purchasing_refreshing)                                     # can start another next time


if __name__ == "__main__":
    unittest.main()
