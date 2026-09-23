"""Amazon listing restrictions: client call, cache/filter semantics, Review Queue guard.
Isolated SQLite and mocked HTTP -- no live database writes or API calls."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import Lead, ListingRestriction, ProductRecord, SellerNewListing
from app.services import restriction_service as rs
from app.services import review_queue_service as rqs
from app.services.restriction_service import RestrictionService
from app.sp_api import client as client_module
from app.sp_api.client import SPAPIClient


def _response(status=200, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    return response


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = SPAPIClient("id", "secret", "token", seller_id="SELLER1")
        for target, value in (("_get_access_token", "tok"), ("_pace", None)):
            patcher = patch.object(self.client, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch.object(client_module.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def _get(self, *responses):
        return patch.object(client_module.requests, "get", side_effect=list(responses))

    def test_empty_restrictions_list_means_sellable(self):
        with self._get(_response(payload={"restrictions": []})) as get:
            answer = self.client.get_listing_restrictions("B0AAA")
        self.assertEqual(answer, {"restricted": False, "reason_code": "", "message": ""})
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["asin"], params["sellerId"], params["conditionType"]), ("B0AAA", "SELLER1", "new_new"))

    def test_any_reason_means_restricted_and_codes_are_joined(self):
        payload = {"restrictions": [{"reasons": [
            {"reasonCode": "NOT_ELIGIBLE", "message": "Not eligible"},
            {"reasonCode": "APPROVAL_REQUIRED", "message": "You need approval"}]}]}
        with self._get(_response(payload=payload)):
            answer = self.client.get_listing_restrictions("B0AAA")
        self.assertTrue(answer["restricted"])
        self.assertEqual(answer["reason_code"], "APPROVAL_REQUIRED+NOT_ELIGIBLE")
        self.assertEqual(answer["message"], "Not eligible")

    def test_rate_limit_is_retried_then_answered(self):
        with self._get(_response(429), _response(payload={"restrictions": []})) as get:
            answer = self.client.get_listing_restrictions("B0AAA")
        self.assertEqual(answer["restricted"], False)
        self.assertEqual(get.call_count, 2)

    def test_exhausted_rate_limit_is_no_answer_not_sellable(self):
        with self._get(*[_response(429)] * client_module.MAX_RETRIES):
            self.assertIsNone(self.client.get_listing_restrictions("B0AAA"))

    def test_http_error_is_no_answer_not_sellable(self):
        with self._get(_response(403)):
            self.assertIsNone(self.client.get_listing_restrictions("B0AAA"))

    def test_network_error_is_no_answer(self):
        with patch.object(client_module.requests, "get", side_effect=OSError("down")):
            self.assertIsNone(self.client.get_listing_restrictions("B0AAA"))

    def test_no_seller_id_or_unknown_marketplace_is_no_answer(self):
        self.client.seller_id = ""
        self.assertIsNone(self.client.get_listing_restrictions("B0AAA"))
        self.client.seller_id = "SELLER1"
        self.assertIsNone(self.client.get_listing_restrictions("B0AAA", marketplace="MOON"))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        patcher = patch.object(rs, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)
        rs._restricted_set_cache.update(at=0.0, value=frozenset(), marketplace=None)

    def store(self, asin, restricted, age_days=0):
        with self.sessions() as db:
            db.add(ListingRestriction(
                asin=asin, marketplace="UK", restricted=restricted, reason_code="APPROVAL_REQUIRED" if restricted else "",
                checked_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)))
            db.commit()

    @staticmethod
    def sp(answers):
        """A fake SP-API client answering from {asin: True/False/None}."""
        sp = MagicMock()
        sp.get_listing_restrictions.side_effect = lambda asin, marketplace: (
            None if answers.get(asin) is None else {"restricted": answers[asin], "reason_code": "X", "message": ""})
        return sp


class CacheTests(_DbCase):
    def test_fresh_answers_are_used_and_expired_ones_ignored(self):
        self.store("R_FRESH", True, age_days=6)
        self.store("R_OLD", True, age_days=8)      # restricted answers expire after 7 days
        self.store("OK_FRESH", False, age_days=13)
        self.store("OK_OLD", False, age_days=15)   # sellable answers expire after 14 days
        self.assertEqual(RestrictionService.cached(["R_FRESH", "R_OLD", "OK_FRESH", "OK_OLD", "NEW"]),
                         {"R_FRESH": True, "OK_FRESH": False})

    def test_check_asks_only_for_what_is_missing_and_saves_answers(self):
        self.store("KNOWN", True)
        sp = self.sp({"NEW1": False, "NEW2": True})
        result = RestrictionService.check(["KNOWN", "NEW1", "NEW2"], sp_client=sp)
        self.assertEqual(result, {"KNOWN": True, "NEW1": False, "NEW2": True})
        self.assertEqual(sorted(c.args[0] for c in sp.get_listing_restrictions.call_args_list), ["NEW1", "NEW2"])
        self.assertEqual(RestrictionService.cached(["NEW1", "NEW2"]), {"NEW1": False, "NEW2": True})

    def test_failed_call_is_none_and_not_cached(self):
        result = RestrictionService.check(["FAILS"], sp_client=self.sp({}))
        self.assertEqual(result, {"FAILS": None})
        self.assertEqual(RestrictionService.cached(["FAILS"]), {})

    def test_no_client_means_unknown_for_everything_uncached(self):
        with patch.object(rs, "get_sp_api_client", return_value=None):
            self.assertEqual(RestrictionService.check(["A"]), {"A": None})

    def test_time_budget_stops_the_calls_and_leaves_the_rest_unknown(self):
        sp = self.sp({"A": False, "B": False, "C": False})
        with patch.object(rs.time, "monotonic", side_effect=[0, 0, 0, 99, 99]):
            result = RestrictionService.check(["A", "B", "C"], budget_seconds=10, sp_client=sp)
        self.assertEqual(result, {"A": False, "B": False, "C": None})

    def test_restricted_set_only_holds_fresh_restricted_asins_and_refreshes_after_a_save(self):
        self.store("R", True)
        self.store("OK", False)
        self.store("R_OLD", True, age_days=9)
        self.assertEqual(RestrictionService.restricted_asin_set(), frozenset({"R"}))
        RestrictionService.check(["NEWLY_GATED"], sp_client=self.sp({"NEWLY_GATED": True}))
        self.assertEqual(RestrictionService.restricted_asin_set(), frozenset({"R", "NEWLY_GATED"}))


class FilterTests(_DbCase):
    def test_restricted_are_split_out_and_unknown_pass_through_in_order(self):
        sp = self.sp({"A": False, "B": True, "C": None, "D": False})
        kept, restricted = RestrictionService.filter_unrestricted(["A", "B", "C", "D"], sp_client=sp)
        self.assertEqual((kept, restricted), (["A", "C", "D"], ["B"]))

    def test_cached_restricted_asins_are_dropped_without_an_api_call(self):
        self.store("B", True)
        sp = self.sp({"A": False})
        kept, restricted = RestrictionService.filter_unrestricted(["A", "B"], sp_client=sp)
        self.assertEqual((kept, restricted), (["A"], ["B"]))
        self.assertEqual([c.args[0] for c in sp.get_listing_restrictions.call_args_list], ["A"])

    def test_needed_stops_api_calls_once_enough_are_confirmed_listable(self):
        sp = self.sp({a: False for a in "ABCDE"})
        kept, restricted = RestrictionService.filter_unrestricted(list("ABCDE"), needed=2, sp_client=sp)
        self.assertEqual(sp.get_listing_restrictions.call_count, 2)
        self.assertEqual(kept, list("ABCDE"))   # the unchecked tail is passed through, never dropped
        self.assertEqual(restricted, [])

    def test_restricted_asins_do_not_count_towards_needed(self):
        sp = self.sp({"A": True, "B": True, "C": False, "D": False, "E": False})
        kept, restricted = RestrictionService.filter_unrestricted(list("ABCDE"), needed=1, sp_client=sp)
        self.assertEqual(restricted, ["A", "B"])
        self.assertEqual(sp.get_listing_restrictions.call_count, 3)

    def test_budget_exhaustion_passes_the_rest_through(self):
        sp = self.sp({"A": True, "B": True})
        with patch.object(rs.time, "monotonic", side_effect=[0, 0, 99]):
            kept, restricted = RestrictionService.filter_unrestricted(["A", "B"], budget_seconds=5, sp_client=sp)
        self.assertEqual((kept, restricted), (["B"], ["A"]))

    def test_no_client_drops_nothing(self):
        with patch.object(rs, "get_sp_api_client", return_value=None):
            kept, restricted = RestrictionService.filter_unrestricted(["A", "B"])
        self.assertEqual((kept, restricted), (["A", "B"], []))


class SweepTests(_DbCase):
    def test_sweep_checks_only_asins_waiting_for_a_human_and_skips_known_ones(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.sessions() as db:
            db.add(ProductRecord(asin="PENDING_BUY", recommendation="BUY", review=None, scanned_at=now))
            db.add(ProductRecord(asin="DECIDED", recommendation="BUY", review="rejected", scanned_at=now))
            db.add(ProductRecord(asin="IGNORED", recommendation="IGNORE", review=None, scanned_at=now))
            db.add(ProductRecord(asin="STALE", recommendation="BUY", review=None, scanned_at=now - timedelta(days=90)))
            db.add(ProductRecord(asin="ALREADY_KNOWN", recommendation="BUY", review=None, scanned_at=now))
            db.add(Lead(asin="UNDECIDED_LEAD", source="sheet", decision=None))
            db.add(Lead(asin="DECIDED_LEAD", source="sheet", decision="rejected"))
            db.add(SellerNewListing(tracked_seller_id=1, asin="COMPETITOR", review=None, dismissed=False))
            db.add(SellerNewListing(tracked_seller_id=1, asin="DISMISSED", review=None, dismissed=True))
            db.commit()
        self.store("ALREADY_KNOWN", False)
        sp = self.sp({"PENDING_BUY": True, "UNDECIDED_LEAD": False, "COMPETITOR": None})
        with patch.object(rs, "get_sp_api_client", return_value=sp):
            summary = RestrictionService.sweep_review_candidates(limit=10)
        checked = sorted(c.args[0] for c in sp.get_listing_restrictions.call_args_list)
        self.assertEqual(checked, ["COMPETITOR", "PENDING_BUY", "UNDECIDED_LEAD"])
        self.assertEqual((summary["waiting"], summary["already_known"], summary["checked"], summary["restricted"],
                          summary["couldnt_check"], summary["still_to_check"]), (4, 1, 2, 1, 1, 0))

    def test_limit_bounds_one_sweep(self):
        with self.sessions() as db:
            for i in range(5):
                db.add(Lead(asin=f"L{i}", source="sheet", decision=None))
            db.commit()
        sp = self.sp({f"L{i}": False for i in range(5)})
        with patch.object(rs, "get_sp_api_client", return_value=sp):
            summary = RestrictionService.sweep_review_candidates(limit=3)
        self.assertEqual((summary["checked"], summary["still_to_check"]), (3, 2))


class ReviewQueueGuardTests(unittest.TestCase):
    ITEMS = [dict(asin="GATED_SCAN", sources=["scan"]), dict(asin="GATED_MERGED", sources=["scan", "competitor"]),
             dict(asin="GATED_VA", sources=["lead"]), dict(asin="GATED_VA_MERGED", sources=["scan", "lead"]),
             dict(asin="FINE", sources=["scan"])]

    def drop(self, restricted):
        with patch.object(rqs.RestrictionService, "restricted_asin_set", return_value=frozenset(restricted)):
            return [i["asin"] for i in rqs.ReviewQueueService._drop_listing_restricted(list(self.ITEMS))]

    def test_gated_scan_and_competitor_items_are_hidden(self):
        kept = self.drop({"GATED_SCAN", "GATED_MERGED"})
        self.assertEqual(kept, ["GATED_VA", "GATED_VA_MERGED", "FINE"])

    def test_va_sheet_leads_are_never_hidden(self):
        kept = self.drop({"GATED_VA", "GATED_VA_MERGED"})
        self.assertEqual(len(kept), 5)

    def test_nothing_restricted_returns_the_list_untouched(self):
        self.assertEqual(len(self.drop(set())), 5)

    def test_a_failing_cache_read_never_hides_anything(self):
        with patch.object(rqs.RestrictionService, "restricted_asin_set", side_effect=RuntimeError("db locked")):
            self.assertEqual(len(rqs.ReviewQueueService._drop_listing_restricted(list(self.ITEMS))), 5)


if __name__ == "__main__":
    unittest.main()
