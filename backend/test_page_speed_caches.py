"""Page speed changes (2026-09-21): Token Usage totals in SQL must equal the old load-everything version; the Amazon
listings page and Scan Intelligence must serve their last result while refreshing behind the page. In-memory SQLite and
fakes -- no Google, no Keepa."""
import inspect
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import TokenUsageEvent
from app.routes import amazon_listing_uploads as listings
from app.routes import scan_intelligence as intel
from app.services import token_usage_service as tus
from app.services.token_usage_service import TokenUsageService

NOW = datetime.now(timezone.utc).replace(tzinfo=None)
ORIGINAL_RUN_PENDING_UPLOADS = listings.svc.run_pending_uploads      # captured before any test patches it


def event(hours_ago, category="scan_queue", call_type="product", tokens=10.0):
    return TokenUsageEvent(occurred_at=NOW - timedelta(hours=hours_ago), category=category, call_type=call_type, tokens=tokens)


def old_daily_summary(sessions, days):
    """The previous implementation, kept as the yardstick."""
    with sessions() as db:
        cutoff = NOW - timedelta(days=days)
        rows = db.query(TokenUsageEvent).filter(TokenUsageEvent.occurred_at >= cutoff).all()
    buckets = {}
    for row in rows:
        key = (row.occurred_at.date().isoformat(), row.category, row.call_type)
        buckets[key] = buckets.get(key, 0.0) + row.tokens
    return [{"date": d, "category": c, "call_type": t, "tokens": round(v, 1)} for (d, c, t), v in sorted(buckets.items())]


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        p = patch.object(tus, "SessionLocal", self.sessions)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def add(self, *events):
        with self.sessions() as db:
            db.add_all(events)
            db.commit()

    def test_it_matches_the_old_implementation_across_days_categories_and_call_types(self):
        self.add(event(1), event(2, tokens=3.25), event(30, tokens=7.5), event(26, category="watchlist", tokens=99.9),
                 event(27, category="watchlist", call_type="sp_api_saved", tokens=4.4), event(50, category="signals", tokens=0.05),
                 event(51, category="signals", tokens=0.04), event(24 * 40, tokens=500), event(0.01, category="replen", tokens=12.3456))
        for days in (1, 3, 30):
            self.assertEqual(TokenUsageService.daily_summary(days=days), old_daily_summary(self.sessions, days), days)

    def test_events_outside_the_window_are_left_out(self):
        self.add(event(1, tokens=5), event(24 * 40, tokens=500))
        self.assertEqual([r["tokens"] for r in TokenUsageService.daily_summary(days=30)], [5.0])

    def test_rows_are_oldest_first_and_shaped_as_before(self):
        self.add(event(1, category="b"), event(1, category="a"), event(60, category="a"))
        rows = TokenUsageService.daily_summary(days=30)
        self.assertEqual([r["date"] for r in rows], sorted(r["date"] for r in rows))
        self.assertEqual(set(rows[0]), {"date", "category", "call_type", "tokens"})

    def test_an_empty_table_gives_an_empty_list(self):
        self.assertEqual(TokenUsageService.daily_summary(days=30), [])


class ListingsPageTests(unittest.TestCase):
    def setUp(self):
        listings._PENDING_COUNT.invalidate()
        self.addCleanup(listings._PENDING_COUNT.invalidate)
        self.reads = 0
        self.pending = [1, 2, 3]
        test = self

        def fake_pending():
            test.reads += 1
            if isinstance(test.pending, Exception):
                raise test.pending
            return test.pending

        for target, fake in (("batch_history", lambda: []), ("batch_failures", lambda batch_id: []),
                             ("pending_rows", fake_pending), ("run_pending_uploads", lambda preview=False: {})):
            p = patch.object(listings.svc, target, fake)
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(listings.router)
        self.client = TestClient(app, follow_redirects=False)

    def test_the_count_shows_and_repeat_loads_do_not_re_read_the_sheet(self):
        for _ in range(4):
            r = self.client.get("/automation/amazon-listings")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.reads, 1)

    def test_a_failed_sheet_read_still_renders_the_page(self):
        self.pending = RuntimeError("OAuth token expired")
        r = self.client.get("/automation/amazon-listings")
        self.assertEqual(r.status_code, 200)

    def test_a_stale_count_is_served_at_once_and_refreshed_behind_the_page(self):
        self.client.get("/automation/amazon-listings")
        listings._PENDING_COUNT._entries["count"][1] -= 10_000
        self.pending = [1]
        self.client.get("/automation/amazon-listings")
        end = time.time() + 5
        while listings._PENDING_COUNT.refreshing() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(self.reads, 2)
        self.assertEqual(listings._PENDING_COUNT.peek("count"), 1)

    def test_running_an_upload_now_drops_the_cached_count(self):
        self.client.get("/automation/amazon-listings")
        self.pending = []
        r = self.client.post("/automation/amazon-listings/run-now")
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(listings._PENDING_COUNT.peek("count"))
        self.client.get("/automation/amazon-listings")
        self.assertEqual(listings._PENDING_COUNT.peek("count"), 0)

    def test_the_upload_itself_still_reads_the_sheet_fresh(self):
        src = inspect.getsource(ORIGINAL_RUN_PENDING_UPLOADS)
        self.assertIn("pending_rows()", src)
        self.assertNotIn("_PENDING_COUNT", src)


class IntelBundleTests(unittest.TestCase):
    def setUp(self):
        intel._INTEL_BUNDLE.invalidate()
        self.addCleanup(intel._INTEL_BUNDLE.invalidate)
        self.calls = {"targets": 0, "economics": 0, "attention": 0}
        test = self

        def counted(name, value):
            def fn(*a, **k):
                test.calls[name] += 1
                return value
            return fn

        for module, attr, name, value in (
            (intel.DiscoveryIntelligenceService, "list_discovery_targets", "targets", [{"brand": "x"}]),
            (intel, "get_brand_economics", "economics", {"x": {}}),
            (intel, "get_attention_candidates", "attention", [{"brand": "x"}]),
        ):
            p = patch.object(module, attr, counted(name, value))
            p.start()
            self.addCleanup(p.stop)

    def test_the_three_roll_ups_are_built_together_once_and_reused(self):
        first = intel._intel_bundle()
        for _ in range(5):
            intel._intel_bundle()
        self.assertEqual(self.calls, {"targets": 1, "economics": 1, "attention": 1})
        self.assertEqual(set(first), {"targets", "economics", "attention"})
        self.assertEqual(first["targets"], [{"brand": "x"}])

    def test_a_stale_bundle_is_served_at_once_and_rebuilt_behind_the_page(self):
        intel._intel_bundle()
        intel._INTEL_BUNDLE._entries["bundle"][1] -= 10_000
        served = intel._intel_bundle()
        self.assertEqual(served["economics"], {"x": {}})
        end = time.time() + 5
        while intel._INTEL_BUNDLE.refreshing() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(self.calls, {"targets": 2, "economics": 2, "attention": 2})

    def test_prewarm_builds_it_in_the_background_and_only_once(self):
        self.assertTrue(intel.prewarm_intel_bundle())
        end = time.time() + 5
        while intel._INTEL_BUNDLE.refreshing() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(self.calls["targets"], 1)
        intel._intel_bundle()
        self.assertEqual(self.calls["targets"], 1)                 # already warm: the page reads the prewarmed bundle

    def test_the_page_reads_the_bundle_not_the_services_directly(self):
        src = inspect.getsource(intel.scan_intelligence)
        self.assertRegex(src, r"(?<![A-Za-z_])_intel_bundle\(\)")        # not _compute_intel_bundle()
        self.assertNotIn("_compute_intel_bundle(", src)
        self.assertNotIn("get_brand_economics(", src)
        self.assertNotIn("get_attention_candidates(", src)
        self.assertNotIn("list_discovery_targets(", src)


if __name__ == "__main__":
    unittest.main()
