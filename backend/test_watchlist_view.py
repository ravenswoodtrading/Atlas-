"""Watchlist page (2026-09-21): reading stored scans instead of scanning on every load, the background refresh (one at a
time, under the manual scan lock, errors recorded), and that opening the page can never start a scan or spend tokens.
In-memory SQLite; the scanner and scan lock are replaced by fakes -- no Keepa, no network."""
import json
import threading
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
from app.database.models import ProductRecord, WatchedProduct
from app.routes import watchlist as watchlist_route
from app.services import product_repository as pr
from app.services import watchlist_view_service as wv

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def rec(asin, days_ago=1, roi=30.0, score=70, recommendation="BUY", report=None, **kw):
    fields = dict(asin=asin, title=f"Title {asin}", brand="Brand", best_source_marketplace="DE", best_source_cost_gbp=10.0,
                  buy_box_now=25.0, buy_box_90d=27.0, profit=5.0, roi=roi, profit_90d=6.0, roi_90d=roi + 2, monthly_sales=40,
                  recommendation=recommendation, score=score, scanned_at=NOW - timedelta(days=days_ago),
                  report_json=json.dumps(report if report is not None else {"recommendation": recommendation, "score": score,
                                                                            "peak_price": 31.0}))
    fields.update(kw)
    return ProductRecord(**fields)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for module in (wv, pr):
            p = patch.object(module, "SessionLocal", self.sessions)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        self.reset_state()
        self.addCleanup(self.reset_state)

    def reset_state(self):
        with wv._STATE_LOCK:
            wv._STATE.update({"running": False, "started_at": None, "finished_at": None, "force": False, "error": None,
                              "checked": None, "tokens_remaining": None})

    def add(self, *objs):
        with self.sessions() as db:
            db.add_all(objs)
            db.commit()


class StoredResultTests(_Db):
    def test_the_latest_record_per_asin_is_used_and_shaped_for_the_page(self):
        self.add(rec("B0AAAAAAA1", days_ago=9, roi=5.0, score=10), rec("B0AAAAAAA1", days_ago=1, roi=41.0, score=80))
        out = wv.stored_result(["B0AAAAAAA1"])
        self.assertEqual(out["count"], 1)
        product, report = out["opportunities"][0]["product"], out["opportunities"][0]["report"]
        self.assertEqual((product["roi"], product["asin"], product["offers_now"]), (41.0, "B0AAAAAAA1", 0))
        self.assertEqual((report["recommendation"], report["score"], report["peak_price"]), ("BUY", 80, 31.0))
        self.assertIsNone(out["error"])
        self.assertEqual(out["skipped_recently_scanned"], 0)

    def test_never_scanned_asins_are_reported_missing_not_dropped_silently(self):
        self.add(rec("B0AAAAAAA1"))
        out = wv.stored_result(["B0AAAAAAA1", "B0NEVERSCAN", "B0AAAAAAA1"])          # a duplicate in the input is harmless
        self.assertEqual((out["count"], out["missing"]), (1, ["B0NEVERSCAN"]))

    def test_only_watched_asins_are_returned(self):
        self.add(rec("B0AAAAAAA1"), rec("B0NOTWATCHD"))
        self.assertEqual([o["product"]["asin"] for o in wv.stored_result(["B0AAAAAAA1"])["opportunities"]], ["B0AAAAAAA1"])

    def test_best_score_first(self):
        self.add(rec("B0LOWSCORE0", score=20), rec("B0HIGHSCORE", score=90), rec("B0MIDSCORE0", score=55))
        got = [o["product"]["asin"] for o in wv.stored_result(["B0LOWSCORE0", "B0HIGHSCORE", "B0MIDSCORE0"])["opportunities"]]
        self.assertEqual(got, ["B0HIGHSCORE", "B0MIDSCORE0", "B0LOWSCORE0"])

    def test_the_columns_beat_the_json_for_recommendation_and_a_broken_json_still_renders(self):
        self.add(rec("B0COLUMNS00", recommendation="CONSIDER", score=60, report={"recommendation": "BUY", "score": 99}),
                 rec("B0BADJSON000", recommendation="IGNORE", score=5))
        with self.sessions() as db:
            db.query(ProductRecord).filter(ProductRecord.asin == "B0BADJSON000").update({"report_json": "{not json"})
            db.commit()
        by = {o["product"]["asin"]: o["report"] for o in wv.stored_result(["B0COLUMNS00", "B0BADJSON000"])["opportunities"]}
        self.assertEqual((by["B0COLUMNS00"]["recommendation"], by["B0COLUMNS00"]["score"]), ("CONSIDER", 60))
        self.assertEqual((by["B0BADJSON000"]["recommendation"], by["B0BADJSON000"]["score"]), ("IGNORE", 5))

    def test_null_numbers_become_zero_so_the_template_cannot_crash(self):
        self.add(rec("B0NULLS0000", best_source_cost_gbp=None, buy_box_now=None, roi_90d=None, title=None, brand=None))
        product = wv.stored_result(["B0NULLS0000"])["opportunities"][0]["product"]
        self.assertEqual((product["best_source_cost_gbp"], product["buy_box_now"], product["roi_90d"], product["title"], product["brand"]),
                         (0, 0, 0, "", ""))

    def test_how_old_the_numbers_are(self):
        self.add(rec("B0FRESH0000", days_ago=0.1), rec("B0OLDISH0000", days_ago=5))
        out = wv.stored_result(["B0FRESH0000", "B0OLDISH0000"], now=NOW)
        self.assertEqual(out["stale"], 1)
        self.assertLess(out["newest"], NOW)
        self.assertLess(out["oldest"], out["newest"])

    def test_an_empty_watchlist_is_fine(self):
        out = wv.stored_result([])
        self.assertEqual((out["count"], out["opportunities"], out["missing"], out["newest"]), (0, [], [], None))


class FakeCoordinator:
    events = []

    @staticmethod
    def acquire_for_manual_scan(timeout=None):
        FakeCoordinator.events.append("acquire")
        return True

    @staticmethod
    def release_after_manual_scan():
        FakeCoordinator.events.append("release")


class RefreshTests(_Db):
    def setUp(self):
        super().setUp()
        FakeCoordinator.events = []
        self.calls = []
        self.gate = threading.Event()
        self.result = {"count": 3, "tokens_remaining": 5000}
        test = self

        class FakeScanner:
            def __init__(self, usage_category=None):
                test.calls.append(("init", usage_category))

            def scan(self, brand, limit=20, force_rescan=False, asins=None, **kw):
                test.calls.append(("scan", brand, limit, force_rescan, list(asins)))
                test.gate.wait(5)
                if isinstance(test.result, Exception):
                    raise test.result
                return test.result

        for target, fake in (("app.services.brand_scan_service.BrandScanService", FakeScanner),
                             ("app.services.scan_coordinator.ScanCoordinator", FakeCoordinator)):
            p = patch(target, fake)
            p.start()
            self.addCleanup(p.stop)

    def wait_done(self):
        end = time.time() + 5
        while wv.refresh_state()["running"] and time.time() < end:
            time.sleep(0.01)
        self.assertFalse(wv.refresh_state()["running"], "refresh did not finish")

    def test_it_runs_the_scan_in_the_background_under_the_manual_scan_lock(self):
        started = time.time()
        self.assertTrue(wv.start_refresh(["B0AAAAAAA1", "B0AAAAAAA2"], force=True))
        self.assertLess(time.time() - started, 1.0)                        # the caller did not wait for the scan
        self.assertTrue(wv.refresh_state()["running"])
        self.gate.set()
        self.wait_done()
        self.assertEqual(self.calls, [("init", "watchlist"), ("scan", "watchlist", 2, True, ["B0AAAAAAA1", "B0AAAAAAA2"])])
        self.assertEqual(FakeCoordinator.events, ["acquire", "release"])
        state = wv.refresh_state()
        self.assertEqual((state["error"], state["checked"], state["tokens_remaining"]), (None, 3, 5000))
        self.assertIsNotNone(state["finished_at"])

    def test_a_normal_refresh_is_not_forced(self):
        self.gate.set()
        wv.start_refresh(["B0AAAAAAA1"])
        self.wait_done()
        self.assertFalse(self.calls[1][3])

    def test_only_one_refresh_at_a_time(self):
        self.assertTrue(wv.start_refresh(["B0AAAAAAA1"]))
        self.assertFalse(wv.start_refresh(["B0AAAAAAA1"]))
        self.assertFalse(wv.start_refresh(["B0AAAAAAA2"], force=True))
        self.gate.set()
        self.wait_done()
        self.assertEqual(sum(1 for c in self.calls if c[0] == "scan"), 1)
        self.assertTrue(wv.start_refresh(["B0AAAAAAA1"]))                  # free again afterwards
        self.wait_done()

    def test_nothing_to_refresh_does_nothing(self):
        self.assertFalse(wv.start_refresh([]))
        self.assertFalse(wv.refresh_state()["running"])
        self.assertEqual(self.calls, [])

    def test_a_crash_is_recorded_and_the_lock_is_still_released(self):
        self.result = RuntimeError("keepa exploded")
        self.gate.set()
        wv.start_refresh(["B0AAAAAAA1"])
        self.wait_done()
        self.assertEqual(wv.refresh_state()["error"], "keepa exploded")
        self.assertEqual(FakeCoordinator.events, ["acquire", "release"])

    def test_an_error_result_is_recorded(self):
        self.result = {"error": "Keepa tokens too low", "count": 0}
        self.gate.set()
        wv.start_refresh(["B0AAAAAAA1"])
        self.wait_done()
        self.assertEqual(wv.refresh_state()["error"], "Keepa tokens too low")


class PageTests(_Db):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(watchlist_route.router)
        self.client = TestClient(app, follow_redirects=False)
        self.add(WatchedProduct(asin="B0AAAAAAA1", title="Watched one"), rec("B0AAAAAAA1", roi=41.0),
                 WatchedProduct(asin="B0NEVERSCAN", title="Watched never scanned"))

    def refuse_scan(self):
        def boom(*a, **k):
            raise AssertionError("opening the watchlist must never scan")
        return patch("app.services.brand_scan_service.BrandScanService.scan", boom)

    def test_opening_the_page_shows_stored_numbers_and_never_scans(self):
        with self.refuse_scan():
            r = self.client.get("/watchlist")
        self.assertEqual(r.status_code, 200)
        self.assertIn("B0AAAAAAA1", r.text)
        self.assertIn("41.0%", r.text)
        self.assertIn("Prices last checked", r.text)
        self.assertIn("Not scanned yet", r.text)

    def test_the_old_force_rescan_link_no_longer_scans_or_starts_anything(self):
        with self.refuse_scan(), patch.object(wv, "start_refresh", side_effect=AssertionError("must not start")):
            r = self.client.get("/watchlist?force_rescan=true")
        self.assertEqual(r.status_code, 200)

    def test_the_refresh_button_starts_a_background_refresh_and_comes_straight_back(self):
        with patch.object(wv, "start_refresh", return_value=True) as start:
            r = self.client.post("/watchlist/refresh", data={"force": "true", "profitable_only": "true"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("/watchlist?", r.headers["location"])
        args, kwargs = start.call_args
        self.assertEqual(sorted(args[0]), ["B0AAAAAAA1", "B0NEVERSCAN"])
        self.assertTrue(kwargs["force"])

    def test_a_second_press_while_running_says_so(self):
        with patch.object(wv, "start_refresh", return_value=False):
            r = self.client.post("/watchlist/refresh", data={})
        self.assertIn("already", r.headers["location"].lower())

    def test_the_page_shows_a_running_refresh(self):
        with wv._STATE_LOCK:
            wv._STATE.update({"running": True, "started_at": NOW, "force": False})
        r = self.client.get("/watchlist")
        self.assertIn("Refreshing prices in the background", r.text)

    def test_the_page_shows_a_failed_refresh(self):
        with wv._STATE_LOCK:
            wv._STATE.update({"running": False, "finished_at": NOW, "error": "Keepa tokens too low"})
        r = self.client.get("/watchlist")
        self.assertIn("Keepa tokens too low", r.text)

    def test_an_empty_watchlist_still_renders(self):
        with self.sessions() as db:
            db.query(WatchedProduct).delete()
            db.commit()
        self.assertIn("Nothing on your watchlist yet", self.client.get("/watchlist").text)


if __name__ == "__main__":
    unittest.main()
