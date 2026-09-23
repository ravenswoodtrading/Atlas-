"""No live Keepa, SP-API or database access: every collaborator is faked."""
import unittest
from unittest.mock import MagicMock, patch

from app.services import eu_drop_scan_service as svc
from app.services.eu_drop_scan_service import EuDropScanService, MIN_PRICE_RATIO


class FakeSp:
    """prices = {(asin, marketplace): price}; a missing key means SP-API had no answer."""

    def __init__(self, prices):
        self.prices = prices
        self.calls = []

    def get_item_offers(self, asin, marketplace):
        self.calls.append((asin, marketplace))
        if (asin, marketplace) not in self.prices:
            return None
        return {"status": "Success", "price": self.prices[(asin, marketplace)]}


class RatioPrecheckTests(unittest.TestCase):
    def setUp(self):
        # EUR -> GBP at a flat 0.85 so the arithmetic in the assertions is obvious.
        patcher = patch.object(svc.CurrencyService, "to_gbp", side_effect=lambda amount, currency: amount * 0.85)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_drops_only_when_both_prices_exist_and_ratio_is_low(self):
        sp = FakeSp({("LOW", "UK"): 20.0, ("LOW", "DE"): 20.0,      # 20 / 17 = 1.18x -> drop
                     ("OK", "UK"): 50.0, ("OK", "DE"): 30.0})       # 50 / 25.5 = 1.96x -> keep
        kept, stats = EuDropScanService.ratio_precheck(["LOW", "OK"], sp, "DE")
        self.assertEqual(kept, ["OK"])
        self.assertEqual(stats, dict(checked=2, dropped_low_ratio=1, kept_ratio_ok=1, kept_unknown=0))

    def test_ratio_exactly_at_the_bar_is_kept(self):
        sp = FakeSp({("EDGE", "UK"): 17.0 * MIN_PRICE_RATIO, ("EDGE", "DE"): 20.0})  # 20 EUR = 17 GBP
        kept, _ = EuDropScanService.ratio_precheck(["EDGE"], sp, "DE")
        self.assertEqual(kept, ["EDGE"])

    def test_missing_uk_answer_is_kept_and_skips_the_source_call(self):
        sp = FakeSp({("NOUK", "DE"): 10.0})
        kept, stats = EuDropScanService.ratio_precheck(["NOUK"], sp, "DE")
        self.assertEqual(kept, ["NOUK"])
        self.assertEqual(stats["kept_unknown"], 1)
        self.assertEqual(sp.calls, [("NOUK", "UK")])

    def test_missing_source_answer_is_kept_not_dropped(self):
        sp = FakeSp({("NODE", "UK"): 30.0})
        kept, stats = EuDropScanService.ratio_precheck(["NODE"], sp, "DE")
        self.assertEqual(kept, ["NODE"])
        self.assertEqual(stats["kept_unknown"], 1)

    def test_stops_once_enough_have_been_kept(self):
        sp = FakeSp({(a, m): p for a in "ABCD" for m, p in (("UK", 50.0), ("DE", 30.0))})
        kept, stats = EuDropScanService.ratio_precheck(list("ABCD"), sp, "DE", stop_after=2)
        self.assertEqual(kept, ["A", "B"])
        self.assertEqual(stats["checked"], 2)


    def test_time_budget_stops_the_check_and_leaves_the_rest_unexamined(self):
        sp = FakeSp({(a, m): p for a in "ABCD" for m, p in (("UK", 50.0), ("DE", 30.0))})
        with patch.object(svc.time, "monotonic", side_effect=[0, 0, 100, 100, 100]):
            kept, stats = EuDropScanService.ratio_precheck(list("ABCD"), sp, "DE", budget_seconds=60)
        self.assertEqual(kept, ["A"])
        self.assertEqual((stats["checked"], stats["budget_hit"]), (1, True))

    def test_progress_is_reported_as_the_check_advances(self):
        sp = FakeSp({(a, m): p for a in "ABCDEFG" for m, p in (("UK", 50.0), ("DE", 30.0))})
        stages = []
        EuDropScanService.ratio_precheck(list("ABCDEFG"), sp, "DE", progress=stages.append)
        self.assertEqual(stages, ["Free price check 0/7", "Free price check 5/7"])

class RunTests(unittest.TestCase):
    def setUp(self):
        self.finder = MagicMock()
        self.finder.api.tokens_left = 1000
        self.finder.find_eu_price_drops.return_value = ["EXCL", "RECENT", "A", "B", "C"]
        self.scanner = MagicMock()
        self.scanner.scan.return_value = {
            "asins_scanned": 2, "opportunities": [
                dict(product=dict(asin="A", title="Thing", roi=41.234, profit=9.5, best_source_marketplace="DE", buy_box_now=60.0),
                     report=dict(recommendation="BUY")),
                dict(product=dict(asin="B"), report=dict(recommendation="IGNORE")),
            ]}
        patches = [
            patch.object(svc, "ProductFinder", return_value=self.finder),
            patch.object(svc, "get_sp_api_client", return_value=None),
            patch.object(svc.ProductRepository, "get_excluded_asins", return_value={"EXCL"}),
            patch.object(svc.ProductRepository, "get_recently_scanned_asins", return_value={"RECENT"}),
            patch.object(svc, "BrandScanService", return_value=self.scanner),
            patch.object(svc, "ScanCoordinator"),
            patch.object(svc.RestrictionService, "filter_unrestricted", side_effect=lambda asins, **kw: (list(asins), [])),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_gated_candidates_are_dropped_before_the_scan_and_remembered_as_examined(self):
        svc.ScanCoordinator.acquire_for_manual_scan.return_value = True
        svc.RestrictionService.filter_unrestricted.side_effect = lambda asins, **kw: (
            [a for a in asins if a != "A"], [a for a in asins if a == "A"])
        row = EuDropScanService.run_category("DE", "340843031", "Computers", "computers", per_category=5)
        self.assertEqual(row["restricted_dropped"], 1)
        self.assertNotIn("A", self.scanner.scan.call_args.kwargs["asins"])
        self.assertIn("A", row["examined_asins"])

    def test_excluded_and_recently_scanned_never_reach_the_scan_and_results_are_summarised(self):
        report = EuDropScanService.run(categories=["computers"], per_category=2)
        row = report["categories"][0]
        self.assertEqual((row["finder_results"], row["fresh_candidates"], row["sent_to_scan"]), (5, 3, 2))
        args, kwargs = self.scanner.scan.call_args
        self.assertEqual(kwargs["asins"], ["A", "B"])
        self.assertEqual(args[0], "eu-drop:de:computers")
        self.assertEqual(row["by_recommendation"], {"BUY": 1, "IGNORE": 1})
        self.assertEqual(row["good"][0]["roi"], 41.2)

    def test_gives_up_cleanly_when_another_scan_never_releases_the_lock(self):
        svc.ScanCoordinator.acquire_for_manual_scan.return_value = False
        svc.ScanCoordinator.busy_reason.return_value = "Waiting for _weekly_recheck_scheduler (900s; Starting)"
        stages = []
        with patch.object(svc, "LOCK_WAIT_SECONDS", 60), patch.object(svc, "LOCK_POLL_SECONDS", 30):
            row = EuDropScanService.run_category("DE", "340843031", "Computers", "computers",
                                                 per_category=2, progress=stages.append)
        self.scanner.scan.assert_not_called()
        self.assertIn("nothing was scanned", row["error"])
        self.assertIn("_weekly_recheck_scheduler", row["error"])
        # the candidates that were never scanned must stay eligible for the next run
        self.assertNotIn("A", row["examined_asins"])
        self.assertIn("EXCL", row["examined_asins"])
        self.assertEqual(sum(1 for s in stages if s.startswith("Waiting for another scan")), 2)

    def test_reports_each_stage_of_a_normal_run(self):
        svc.ScanCoordinator.acquire_for_manual_scan.return_value = True
        stages = []
        EuDropScanService.run_category("DE", "340843031", "Computers", "computers", per_category=2, progress=stages.append)
        self.assertEqual(stages[0], "Searching Keepa for price drops")
        self.assertIn("Scanning 2 products (uses Keepa tokens)", stages)

    def test_only_scanned_candidates_are_remembered_as_examined(self):
        svc.ScanCoordinator.acquire_for_manual_scan.return_value = True
        row = EuDropScanService.run_category("DE", "340843031", "Computers", "computers", per_category=2)
        self.assertEqual(set(row["examined_asins"]), {"EXCL", "RECENT", "A", "B"})  # C was cut off by the cap

    def test_toys_use_the_higher_price_floor(self):
        EuDropScanService.run(categories=["toys"], per_category=1)
        self.assertEqual(self.finder.find_eu_price_drops.call_args.kwargs["min_price"], 25)

    def test_unmapped_marketplace_is_reported_not_scanned(self):
        report = EuDropScanService.run(categories=["diy"], marketplace="FR")
        self.assertIn("No FR category ID", report["categories"][0]["error"])
        self.scanner.scan.assert_not_called()

    def test_failed_finder_call_is_reported_not_scanned(self):
        self.finder.find_eu_price_drops.return_value = None
        report = EuDropScanService.run(categories=["games"])
        self.assertEqual(report["categories"][0]["error"], "Product Finder request failed")
        self.scanner.scan.assert_not_called()

    def test_unknown_category_key_is_rejected_up_front(self):
        with self.assertRaises(ValueError):
            EuDropScanService.run(categories=["nonsense"])


if __name__ == "__main__":
    unittest.main()
