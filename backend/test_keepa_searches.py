"""Isolated SQLite tests: no live database writes, Keepa or SP-API calls."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import SignalRun, TokenUsageEvent
from app.routes import signals as signals_routes
from app.services import product_repository as repo
from app.services import signal_service as svc
from app.services.product_repository import ProductRepository
from app.services.signal_service import EU_PRICE_DROP, SignalService, migrate_signal_schema


def _naive_utc(**delta):
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**delta)


class MigrationTests(unittest.TestCase):
    def test_adds_the_new_columns_to_an_existing_signal_queries_table(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE signal_queries (id INTEGER PRIMARY KEY, name VARCHAR, signal_type VARCHAR, "
                              "category_ids VARCHAR, enabled BOOLEAN, last_checked_at DATETIME, "
                              "last_match_snapshot VARCHAR, created_at DATETIME)"))
            conn.execute(text("INSERT INTO signal_queries (id, name, signal_type, category_ids, enabled) "
                              "VALUES (1, 'Old stock-out', 'stock_out', '', 1)"))

        migrate_signal_schema(engine)
        migrate_signal_schema(engine)  # idempotent

        with engine.connect() as conn:
            row = conn.execute(text("SELECT marketplace, min_price, max_price, drop30_pct, drop90_pct, "
                                    "min_rank_drops30, per_run_cap FROM signal_queries WHERE id = 1")).one()
        self.assertEqual(tuple(row), ("UK", 20, 150, 20, 15, 10, 60))
        engine.dispose()

    def test_adds_stage_to_an_existing_signal_runs_table(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE signal_runs (id INTEGER PRIMARY KEY, signal_query_id INTEGER, status VARCHAR)"))
            conn.execute(text("INSERT INTO signal_runs (id, signal_query_id, status) VALUES (1, 3, 'running')"))
        migrate_signal_schema(engine)
        migrate_signal_schema(engine)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT stage FROM signal_runs WHERE id = 1")).scalar(), "")
        engine.dispose()

    def test_no_table_yet_is_a_no_op(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        migrate_signal_schema(engine)
        engine.dispose()


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for target in (repo, svc):
            p = patch.object(target, "SessionLocal", self.sessions)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def _query(self, **kw):
        defaults = dict(name="Computers DE", signal_type=EU_PRICE_DROP, category_ids="340843031", marketplace="DE")
        defaults.update(kw)
        name, signal_type, category_ids = defaults.pop("name"), defaults.pop("signal_type"), defaults.pop("category_ids")
        return ProductRepository.create_signal_query(name, signal_type, category_ids, **defaults)


class RepositoryTests(_DbCase):
    def test_eu_query_stores_its_search_settings(self):
        qid = self._query(min_price=25, max_price=90, drop30_pct=30, drop90_pct=20, min_rank_drops30=5, per_run_cap=40)
        q = ProductRepository.get_signal_query(qid)
        self.assertEqual((q.marketplace, q.min_price, q.max_price, q.drop30_pct, q.drop90_pct, q.min_rank_drops30, q.per_run_cap),
                         ("DE", 25, 90, 30, 20, 5, 40))

    def test_existing_query_types_still_get_uk_defaults(self):
        qid = ProductRepository.create_signal_query("Stock-outs", "stock_out", "123")
        q = ProductRepository.get_signal_query(qid)
        self.assertEqual((q.marketplace, q.per_run_cap), ("UK", 60))

    def test_run_lifecycle_and_latest_per_query(self):
        qid = self._query()
        first = ProductRepository.create_signal_run(qid)
        ProductRepository.finish_signal_run(first, "done", leads=3, tokens_spent=120.5)
        second = ProductRepository.create_signal_run(qid)
        latest = ProductRepository.latest_signal_run_by_query()[qid]
        self.assertEqual((latest.id, latest.status), (second, "running"))
        done = [r for r in ProductRepository.list_signal_runs() if r.id == first][0]
        self.assertEqual((done.status, done.leads, done.tokens_spent), ("done", 3, 120.5))
        self.assertIsNotNone(done.finished_at)

    def test_stage_updates_only_while_a_run_is_running(self):
        qid = self._query()
        run_id = ProductRepository.create_signal_run(qid)
        ProductRepository.set_signal_run_stage(run_id, "Free price check 5/40")
        self.assertEqual(ProductRepository.list_signal_runs()[0].stage, "Free price check 5/40")
        ProductRepository.finish_signal_run(run_id, "done")
        self.assertEqual(ProductRepository.list_signal_runs()[0].stage, "")  # cleared on finish
        ProductRepository.set_signal_run_stage(run_id, "too late")
        self.assertEqual(ProductRepository.list_signal_runs()[0].stage, "")

    def test_deleting_a_search_removes_its_run_history(self):
        qid = self._query()
        ProductRepository.create_signal_run(qid)
        ProductRepository.delete_signal_query(qid)
        self.assertEqual(ProductRepository.list_signal_runs(), [])

    def test_stale_running_rows_are_failed_but_fresh_ones_are_left(self):
        qid = self._query()
        stale, fresh = ProductRepository.create_signal_run(qid), ProductRepository.create_signal_run(qid)
        with self.sessions() as db:
            db.get(SignalRun, stale).started_at = _naive_utc(minutes=-90)
            db.commit()
        ProductRepository.fail_stale_signal_runs(45)
        with self.sessions() as db:
            self.assertEqual(db.get(SignalRun, stale).status, "failed")
            self.assertIn("interrupted", db.get(SignalRun, stale).summary_json)
            self.assertEqual(db.get(SignalRun, fresh).status, "running")


class ExecuteRunTests(_DbCase):
    def _row(self, **kw):
        row = dict(key="computers-de", label="Computers DE", finder_results=76, fresh_candidates=70,
                   precheck=dict(checked=76, dropped_low_ratio=35, kept_ratio_ok=24, kept_unknown=17),
                   sent_to_scan=41, plug_risk_dropped=5, plug_risk_examples=["Some Router"], asins_scanned=41, saved=20,
                   by_recommendation={"BUY": 2, "CONSIDER": 6, "IGNORE": 12}, error=None,
                   good=[dict(asin="B0X", title="Mouse", recommendation="BUY", roi=46.2, profit=14.2, source="DE", uk_price=57.0)],
                   examined_asins=["A1", "A2"])
        row.update(kw)
        return row

    def _run(self, qid, row):
        run_id = ProductRepository.create_signal_run(qid)
        with patch.object(svc.EuDropScanService, "run_category", return_value=row) as run_category, \
                patch.object(svc.ActivityLog, "record"):
            SignalService._execute_eu_price_drop(qid, run_id)
        return run_id, run_category

    def test_records_the_funnel_leads_summary_and_real_token_spend(self):
        qid = self._query(per_run_cap=45, min_price=25)
        with self.sessions() as db:
            db.add_all([
                TokenUsageEvent(occurred_at=_naive_utc(minutes=-600), category="eu_drop_scan", call_type="keepa_query", tokens=999),
                TokenUsageEvent(occurred_at=_naive_utc(minutes=1), category="eu_drop_scan", call_type="keepa_query", tokens=300),
                TokenUsageEvent(occurred_at=_naive_utc(minutes=1), category="eu_drop_scan", call_type="sp_api_saved", tokens=50),
                TokenUsageEvent(occurred_at=_naive_utc(minutes=1), category="scan_queue", call_type="keepa_query", tokens=70),
            ])
            db.commit()

        run_id, run_category = self._run(qid, self._row())

        with self.sessions() as db:
            run = db.get(SignalRun, run_id)
            self.assertEqual(run.status, "done")
            self.assertEqual((run.candidates_found, run.precheck_dropped, run.plug_dropped, run.sent_to_scan, run.saved, run.leads),
                             (76, 35, 5, 41, 20, 8))
            self.assertEqual(run.tokens_spent, 300.0)  # not the old row, the saved-estimate row, or another feature
            summary = json.loads(run.summary_json)
            self.assertNotIn("examined_asins", summary)
            self.assertEqual(summary["good"][0]["asin"], "B0X")
        kwargs = run_category.call_args.kwargs
        self.assertEqual((kwargs["per_category"], kwargs["min_price"], kwargs["skip_asins"]), (45, 25, set()))
        kwargs["progress"]("Free price check 5/40")  # the callback the page's live stage comes from
        self.assertEqual(ProductRepository.list_signal_runs()[0].stage, "")  # run already finished -> ignored
        self.assertEqual(run_category.call_args.args[:2], ("DE", ["340843031"]))

    def test_snapshot_is_replaced_by_this_runs_examined_set_and_fed_to_the_next_run(self):
        qid = self._query()
        self._run(qid, self._row(examined_asins=["A1", "A2"]))
        self.assertEqual(set(json.loads(ProductRepository.get_signal_query(qid).last_match_snapshot)), {"A1", "A2"})

        _run_id, run_category = self._run(qid, self._row(examined_asins=["A2", "A3"]))
        self.assertEqual(run_category.call_args.kwargs["skip_asins"], {"A1", "A2"})
        self.assertEqual(set(json.loads(ProductRepository.get_signal_query(qid).last_match_snapshot)), {"A2", "A3"})

    def test_finder_failure_marks_the_run_failed_and_leaves_the_snapshot_alone(self):
        qid = self._query()
        self._run(qid, self._row(examined_asins=["A1"]))
        run_id, _ = self._run(qid, dict(key="k", label="l", error="Product Finder request failed"))
        with self.sessions() as db:
            self.assertEqual(db.get(SignalRun, run_id).status, "failed")
        self.assertEqual(json.loads(ProductRepository.get_signal_query(qid).last_match_snapshot), ["A1"])

    def test_an_error_with_nothing_scanned_is_a_failed_run_but_a_partial_scan_is_not(self):
        qid = self._query()
        lock_row = self._row(error="Another scan (weekly recheck) was still running after 15 minutes",
                             sent_to_scan=1, asins_scanned=0, saved=0, by_recommendation={}, good=[])
        run_id, _ = self._run(qid, lock_row)
        with self.sessions() as db:
            run = db.get(SignalRun, run_id)
            self.assertEqual((run.status, run.stage), ("failed", ""))
            self.assertIn("15 minutes", run.summary_json)

        partial = self._row(error="Only 20 Keepa tokens left", asins_scanned=12)
        run_id, _ = self._run(qid, partial)
        with self.sessions() as db:
            self.assertEqual(db.get(SignalRun, run_id).status, "done")

    def test_worker_turns_an_unexpected_crash_into_a_failed_run_and_releases_the_lock(self):
        qid = self._query()
        run_id = ProductRepository.create_signal_run(qid)
        self.assertTrue(SignalService._eu_run_lock.acquire(blocking=False))
        with patch.object(svc.EuDropScanService, "run_category", side_effect=RuntimeError("boom")):
            SignalService._eu_worker(qid, run_id)
        with self.sessions() as db:
            run = db.get(SignalRun, run_id)
            self.assertEqual(run.status, "failed")
            self.assertIn("boom", run.summary_json)
        self.assertTrue(SignalService._eu_run_lock.acquire(blocking=False))  # was released by the worker
        SignalService._eu_run_lock.release()


class StartRunTests(_DbCase):
    def tearDown(self):
        if SignalService._eu_run_lock.locked():
            SignalService._eu_run_lock.release()

    def test_rejects_unknown_wrong_type_disabled_and_category_less_searches(self):
        self.assertFalse(SignalService.start_eu_price_drop_run(999)[0])
        stock = ProductRepository.create_signal_query("Stock", "stock_out", "1")
        self.assertFalse(SignalService.start_eu_price_drop_run(stock)[0])
        disabled = self._query()
        ProductRepository.set_signal_query_enabled(disabled, False)
        self.assertIn("disabled", SignalService.start_eu_price_drop_run(disabled)[1])
        nocat = self._query(category_ids="")
        self.assertIn("category", SignalService.start_eu_price_drop_run(nocat)[1])
        self.assertEqual(ProductRepository.list_signal_runs(), [])

    def test_only_one_run_at_a_time(self):
        qid = self._query()
        SignalService._eu_run_lock.acquire()
        started, message = SignalService.start_eu_price_drop_run(qid)
        self.assertFalse(started)
        self.assertIn("still running", message)
        self.assertEqual(ProductRepository.list_signal_runs(), [])

    def test_a_valid_run_creates_a_running_row_and_hands_off_to_the_worker(self):
        qid = self._query()
        with patch.object(SignalService, "_eu_worker") as worker:
            started, message = SignalService.start_eu_price_drop_run(qid)
            for thread in __import__("threading").enumerate():
                if thread is not __import__("threading").current_thread():
                    thread.join(timeout=2)
        self.assertTrue(started)
        self.assertIn("background", message)
        run = ProductRepository.list_signal_runs()[0]
        self.assertEqual(run.status, "running")
        worker.assert_called_once_with(qid, run.id)


class FormValidationTests(unittest.TestCase):
    def _add(self, **overrides):
        args = dict(name="Computers DE", signal_type=EU_PRICE_DROP, category_ids="340843031", marketplace="DE",
                    min_price=20, max_price=150, drop30_pct=20, drop90_pct=15, min_rank_drops30=10, per_run_cap=60)
        args.update(overrides)
        with patch.object(signals_routes.ProductRepository, "create_signal_query") as create:
            response = signals_routes.signal_query_add(**args)
        return response, create

    def test_a_valid_search_is_created_with_all_its_settings(self):
        response, create = self._add(category_ids="340843031, 3167641", per_run_cap=40)
        self.assertEqual(response.headers["location"], "/signals/queries")
        args, kwargs = create.call_args
        self.assertEqual(args, ("Computers DE", EU_PRICE_DROP, "340843031,3167641"))
        self.assertEqual((kwargs["marketplace"], kwargs["per_run_cap"]), ("DE", 40))

    def test_bad_input_is_refused_with_a_message_and_nothing_is_saved(self):
        for overrides in (
            dict(marketplace="US"), dict(category_ids=""), dict(category_ids="Computers"),
            dict(min_price=200, max_price=100), dict(drop30_pct=0), dict(drop90_pct=95),
            dict(min_rank_drops30=-1), dict(per_run_cap=0), dict(per_run_cap=101), dict(signal_type="bogus"),
        ):
            response, create = self._add(**overrides)
            self.assertIn("check_result=", response.headers["location"], overrides)
            create.assert_not_called()

    def test_existing_signal_types_are_unaffected_by_the_new_fields(self):
        response, create = self._add(signal_type="stock_out", category_ids="", marketplace="XX", per_run_cap=0)
        create.assert_called_once_with("Computers DE", "stock_out", "")
        self.assertEqual(response.headers["location"], "/signals/queries")

    def test_the_results_feed_still_only_lists_the_three_match_producing_types(self):
        self.assertEqual(signals_routes.SIGNAL_TYPES, ["stock_out", "price_spike", "ceiling_recheck"])
        self.assertIn(EU_PRICE_DROP, signals_routes.QUERY_SIGNAL_TYPES)


if __name__ == "__main__":
    unittest.main()
