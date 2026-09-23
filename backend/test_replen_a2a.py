"""Replen (EU A2A): Buy Sheet parsing, status verdicts, rolling batch selection, stock refresh,
Keepa apply, alerts, and the SP-API inventory pagination fix. Isolated in-memory SQLite and
mocks -- no live database writes, Sheets, SP-API or Keepa calls."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ReplenA2AItem
from app.services import replen_a2a_service as svc
from app.services.replen_a2a_service import ReplenA2AService, classify, select_batch
from app.sp_api import client as client_module
from app.sp_api.client import SPAPIClient

NOW = datetime(2026, 9, 20, 12, 0)

HEADER = ["Date Ordered", "Product Name", "ASIN", "Cost Price", "Sale Price", "Potential ROI", "Profit (Per Unit)",
          "Quantity", "Cost Total", "Profit (Total)", "Source URL", "Amazon URL", "Category", "Brand", "Store", "SKU"]


def sheet_row(date, asin, cost, sale, qty, store="Amazon.es", sku="", name="Thing", brand="Brand", category="Cat"):
    return [date, name, asin, cost, sale, "", "", str(qty), "", "", "", "", category, brand, store, sku]


def item(**kw):
    """A fully-priced, profitable item unless overridden."""
    base = dict(
        asin="B0AAAAAAAA", ignored=False, gated=False, no_keepa_data=False, filtered_reason="",
        last_checked_at=NOW - timedelta(days=1), last_bought_at=NOW - timedelta(days=40),
        last_cost_gbp=12.21, last_sale_price_gbp=32.0,
        current_source_cost_gbp=12.5, current_source_marketplace="ES", current_roi=60.0,
        buy_box_now=32.0, buy_box_90d=31.0, stock_total=None, stock_inbound=None, units_30d=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class StoreAndParsingTests(unittest.TestCase):
    def test_eu_store_spellings_seen_in_the_real_sheet(self):
        for store in ("Amazon.de", "amazon.fr", "Amazon.es ", "Amazon.it", "AMIT (Amazon.it)",
                      "Amazon EU (Germany)", "Amazon.italy", "Amazon,de"):
            self.assertTrue(svc.is_eu_a2a_store(store), store)

    def test_uk_us_bare_and_retail_stores_are_not_eu_a2a(self):
        for store in ("Amazon.co.uk", "Amazon.co.uk ", "Amazon.uk", "Amazon,co.uk", "Amazon.com",
                      "Amazon", "boots", "ADE", "", None):
            self.assertFalse(svc.is_eu_a2a_store(store), store)

    def test_money_survives_the_mangled_pound_sign_and_commas(self):
        self.assertEqual(svc.parse_money("�6.33"), 6.33)
        self.assertEqual(svc.parse_money("£1,240.50"), 1240.50)
        self.assertEqual(svc.parse_money("32"), 32.0)
        self.assertIsNone(svc.parse_money(""))
        self.assertIsNone(svc.parse_money("n/a"))

    def test_dates_iso_dmy_and_slash_and_garbage(self):
        # ISO must not be day/month-flipped -- 5 September, not 9 May.
        self.assertEqual(svc.parse_date("2026-09-05"), datetime(2026, 9, 5))
        self.assertEqual(svc.parse_date("16 Feb 26"), datetime(2026, 2, 16))
        self.assertEqual(svc.parse_date("6/1/2026"), datetime(2026, 1, 6))     # day first, as the sheet is UK
        self.assertIsNone(svc.parse_date("KLORA"))
        self.assertIsNone(svc.parse_date(""))

    def test_parse_keeps_only_eu_rows_and_aggregates_per_asin(self):
        values = [HEADER,
                  sheet_row("2026-06-01", "B0AAAAAAAA", "10.00", "30.00", 4, "Amazon.de", "AMA_10_30_4_1JUN", "Old title"),
                  sheet_row("2026-08-26", "B0AAAAAAAA", "£12.21", "32", 5, "Amazon.es", "AMA_12.21_32.00_5_26AUG", "New title"),
                  sheet_row("2026-08-01", "B0BBBBBBBB", "5", "15", 3, "Amazon.co.uk"),       # UK -> excluded
                  sheet_row("2026-08-02", "B0CCCCCCCC", "5", "15", 3, "boots")]                # retail -> excluded
        purchases, stats = svc.parse_buy_sheet_rows(values)
        self.assertEqual(set(purchases), {"B0AAAAAAAA"})
        p = purchases["B0AAAAAAAA"]
        self.assertEqual((p["times_bought"], p["total_units_bought"]), (2, 9))
        self.assertEqual(p["first_bought_at"], datetime(2026, 6, 1))
        self.assertEqual(p["last_bought_at"], datetime(2026, 8, 26))
        # The most recent purchase is the baseline for "what we paid / planned".
        self.assertEqual((p["last_qty"], p["last_cost_gbp"], p["last_sale_price_gbp"]), (5, 12.21, 32.0))
        self.assertEqual((p["last_source_store"], p["last_sku"], p["title"]), ("Amazon.es", "AMA_12.21_32.00_5_26AUG", "New title"))
        self.assertEqual(stats["eu_rows"], 2)

    def test_literal_duplicate_rows_count_once(self):
        row = sheet_row("2026-08-26", "B0AAAAAAAA", "12.21", "32", 5, "Amazon.es", "AMA_12.21_32.00_5_26AUG")
        purchases, stats = svc.parse_buy_sheet_rows([HEADER, row, list(row)])
        self.assertEqual(purchases["B0AAAAAAAA"]["total_units_bought"], 5)
        self.assertEqual(purchases["B0AAAAAAAA"]["times_bought"], 1)
        self.assertEqual(stats["duplicate_rows"], 1)

    def test_same_asin_same_day_different_sku_is_a_separate_buy(self):
        a = sheet_row("2026-08-26", "B0AAAAAAAA", "12.21", "32", 5, "Amazon.es", "SKU_ONE")
        b = sheet_row("2026-08-26", "B0AAAAAAAA", "12.21", "32", 5, "Amazon.es", "SKU_TWO")
        purchases, _ = svc.parse_buy_sheet_rows([HEADER, a, b])
        self.assertEqual(purchases["B0AAAAAAAA"]["total_units_bought"], 10)

    def test_bad_rows_are_skipped_and_counted_not_crashed_on(self):
        values = [HEADER,
                  sheet_row("KLORA", "B0AAAAAAAA", "5", "15", 3),               # garbage date
                  sheet_row("2026-08-01", "NOTANASIN", "5", "15", 3),           # bad ASIN
                  sheet_row("2026-08-01", "B0BBBBBBBB", "5", "15", 0),          # zero qty
                  ["2026-08-01", "short row"]]                                  # ragged row
        purchases, stats = svc.parse_buy_sheet_rows(values)
        self.assertEqual(purchases, {})
        self.assertEqual((stats["skipped_bad_date"], stats["skipped_bad_asin"], stats["skipped_bad_qty"]), (1, 1, 1))


class ClassifyTests(unittest.TestCase):
    def test_never_priced(self):
        self.assertEqual(classify(item(last_checked_at=None))[0], svc.UNCHECKED)

    def test_blockers_beat_any_roi(self):
        self.assertEqual(classify(item(gated=True))[0], svc.GATED)
        self.assertEqual(classify(item(no_keepa_data=True))[0], svc.DEAD)
        self.assertEqual(classify(item(filtered_reason="dead_listing"))[0], svc.DEAD)
        self.assertEqual(classify(item(filtered_reason="excluded_category"))[0], svc.DEAD)
        self.assertEqual(classify(item(filtered_reason="unprofitable_ceiling"))[0], svc.WAIT_PRICE)

    def test_no_source(self):
        self.assertEqual(classify(item(current_source_cost_gbp=None, current_roi=None))[0], svc.NO_SOURCE)

    def test_profitable_with_no_stock_info_is_buy_more_and_says_stock_unknown(self):
        status, reason = classify(item())
        self.assertEqual(status, svc.BUY_MORE)
        self.assertIn("stock unknown", reason)

    def test_profitable_and_selling_through_is_buy_more(self):
        status, reason = classify(item(stock_total=2, units_30d=8, stock_inbound=0))
        self.assertEqual(status, svc.BUY_MORE)
        self.assertIn("2 in stock", reason)

    def test_profitable_but_lots_of_stock_at_current_sales_rate_is_stocked(self):
        # 60 in stock, 6 a month => ~300 days of cover.
        status, reason = classify(item(stock_total=60, units_30d=6))
        self.assertEqual(status, svc.STOCKED)
        self.assertIn("days of stock", reason)

    def test_profitable_with_stock_and_no_sales_in_30_days_is_stocked(self):
        self.assertEqual(classify(item(stock_total=5, units_30d=0))[0], svc.STOCKED)

    def test_profitable_with_zero_stock_and_no_recent_sales_is_still_buy_more(self):
        # Sold out and nothing shipped: nothing on the shelf to be "well stocked" with.
        self.assertEqual(classify(item(stock_total=0, units_30d=0))[0], svc.BUY_MORE)

    def test_cover_boundary(self):
        # exactly 45 days of cover is still a buy; just over is stocked.
        self.assertEqual(classify(item(stock_total=45, units_30d=30))[0], svc.BUY_MORE)
        self.assertEqual(classify(item(stock_total=46, units_30d=30))[0], svc.STOCKED)

    def test_roi_bands(self):
        self.assertEqual(classify(item(current_roi=25.0))[0], svc.BUY_MORE)
        self.assertEqual(classify(item(current_roi=24.9))[0], svc.MARGINAL)
        self.assertEqual(classify(item(current_roi=17.0))[0], svc.MARGINAL)
        self.assertNotIn(classify(item(current_roi=16.9))[0], (svc.BUY_MORE, svc.MARGINAL))

    def test_andis_case_cost_up_and_price_down(self):
        # B0002ZFOS6: paid 12.21, planned 32.00; now source 22.47 (IT), Buy Box 23.88, ROI -20.7%.
        status, reason = classify(item(current_source_cost_gbp=22.47, current_source_marketplace="IT",
                                       current_roi=-20.7, buy_box_now=23.88))
        self.assertEqual(status, svc.WAIT_BOTH)
        self.assertIn("EU cost up", reason)
        self.assertIn("Buy Box down 25%", reason)

    def test_cost_up_only(self):
        status, reason = classify(item(current_source_cost_gbp=22.47, current_roi=-5, buy_box_now=32.0))
        self.assertEqual(status, svc.WAIT_COST)
        self.assertIn("22.47", reason)

    def test_price_down_only(self):
        self.assertEqual(classify(item(current_roi=-5, buy_box_now=20.0))[0], svc.WAIT_PRICE)

    def test_small_drift_is_not_a_cause(self):
        # cost +5%, price -5%: both inside tolerance, so no named cause.
        status, reason = classify(item(current_source_cost_gbp=12.82, buy_box_now=30.4, current_roi=10))
        self.assertEqual(status, svc.WAIT)
        self.assertIn("no single cause", reason)

    def test_missing_purchase_baseline_does_not_crash(self):
        status, _ = classify(item(last_cost_gbp=None, last_sale_price_gbp=None, current_roi=5))
        self.assertEqual(status, svc.WAIT)


class SelectBatchTests(unittest.TestCase):
    def make(self, n, checked_hours_ago=None, **kw):
        out = []
        for i in range(n):
            hours = checked_hours_ago(i) if callable(checked_hours_ago) else checked_hours_ago
            out.append(SimpleNamespace(
                asin=f"B{i:09d}", ignored=False, units_30d=0,
                last_bought_at=NOW - timedelta(days=200),
                last_checked_at=None if hours is None else NOW - timedelta(hours=hours), **kw))
        return out

    def test_quota_is_a_quarter_rounded_up(self):
        self.assertEqual(len(select_batch(self.make(244, 100), NOW)), 61)
        self.assertEqual(len(select_batch(self.make(3, 100), NOW)), 1)
        self.assertEqual(select_batch([], NOW), [])

    def test_never_checked_go_first_then_oldest(self):
        items = self.make(8, lambda i: None if i < 2 else 10 * i)
        picked = [i.asin for i in select_batch(items, NOW)]
        self.assertEqual(len(picked), 2)
        self.assertEqual(set(picked), {"B000000000", "B000000001"})
        items = self.make(8, lambda i: 10 * (i + 1))         # asin 7 is oldest, then 6
        self.assertEqual([i.asin for i in select_batch(items, NOW)], ["B000000007", "B000000006"])

    def test_ignored_items_are_never_selected_or_counted(self):
        items = self.make(8, 100)
        for i in items[:4]:
            i.ignored = True
        picked = select_batch(items, NOW)
        self.assertEqual(len(picked), 1)          # 25% of the 4 eligible
        self.assertTrue(all(not p.ignored for p in picked))

    def test_hot_stale_item_jumps_the_queue_but_fresh_hot_item_does_not(self):
        items = self.make(40, lambda i: 1000 - i)            # ordered: asin 0 is the oldest
        stale_hot, fresh_hot = items[30], items[31]
        stale_hot.last_checked_at = NOW - timedelta(hours=60)
        stale_hot.last_bought_at = NOW - timedelta(days=5)
        fresh_hot.last_checked_at = NOW - timedelta(hours=10)
        fresh_hot.last_bought_at = NOW - timedelta(days=5)
        picked = {i.asin for i in select_batch(items, NOW)}
        self.assertIn(stale_hot.asin, picked)
        self.assertNotIn(fresh_hot.asin, picked)

    def test_hot_items_cannot_starve_the_rest(self):
        items = self.make(40, lambda i: 1000 + i)
        for i in items:
            i.units_30d = 3                                  # everything is "hot" and stale
        picked = select_batch(items, NOW)
        self.assertEqual(len(picked), 10)                    # quota unchanged

    def test_every_asin_is_repriced_within_four_days(self):
        items = self.make(101, None)
        clock = NOW
        for day in range(4):
            for picked in select_batch(items, clock):
                picked.last_checked_at = clock
            clock += timedelta(days=1)
        self.assertTrue(all(i.last_checked_at is not None for i in items))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        # ActivityLog opens its OWN SessionLocal, so patching svc.SessionLocal alone let
        # run_daily's activity rows leak into the live atlas.db (found 2026-09-20: 16 rows).
        for target, value in (("SessionLocal", self.sessions), ("ActivityLog", MagicMock())):
            patcher = patch.object(svc, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def add(self, **kw):
        base = dict(asin="B0AAAAAAAA", title="Thing", last_sku="SKU_A", last_cost_gbp=12.21, last_sale_price_gbp=32.0)
        base.update(kw)
        with self.sessions() as db:
            db.add(ReplenA2AItem(**base))
            db.commit()

    def get(self, asin="B0AAAAAAAA"):
        with self.sessions() as db:
            row = db.query(ReplenA2AItem).filter_by(asin=asin).one()
            db.expunge(row)
            return row


class SyncTests(_DbCase):
    VALUES = [HEADER, sheet_row("2026-08-26", "B0AAAAAAAA", "12.21", "32", 5, "Amazon.es", "SKU_A", "Andis blade", "Andis", "Pet")]

    def test_sync_adds_then_updates_and_never_touches_stock_or_status(self):
        self.assertEqual(ReplenA2AService.sync_purchases(self.VALUES)["added"], 1)
        with self.sessions() as db:
            row = db.query(ReplenA2AItem).one()
            row.stock_total, row.status, row.title = 7, "BUY_MORE", "Keepa title"
            db.commit()

        again = [HEADER, sheet_row("2026-09-10", "B0AAAAAAAA", "13", "33", 6, "Amazon.es", "SKU_B", "Sheet title")]
        result = ReplenA2AService.sync_purchases(again)
        self.assertEqual((result["added"], result["updated"]), (0, 1))
        row = self.get()
        self.assertEqual((row.last_qty, row.last_cost_gbp, row.last_sku), (6, 13.0, "SKU_B"))
        self.assertEqual((row.stock_total, row.status), (7, "BUY_MORE"))
        self.assertEqual(row.title, "Keepa title")            # a sheet title never overwrites a real one

    def test_asin_missing_from_a_later_sheet_is_kept(self):
        ReplenA2AService.sync_purchases(self.VALUES)
        ReplenA2AService.sync_purchases([HEADER])
        self.assertEqual(self.get().asin, "B0AAAAAAAA")


class StockTests(_DbCase):
    def sp(self, inventory, shipments):
        sp = MagicMock()
        sp.get_inventory_summaries.return_value = inventory
        sp.get_fba_fulfilled_shipments_units.return_value = shipments
        return sp

    def inv(self, asin, total, fulfillable=0, inbound=0):
        return {"asin": asin, "total": total, "fulfillable": fulfillable, "inbound_working": inbound,
                "inbound_shipped": 0, "inbound_receiving": 0}

    def test_stock_sums_skus_and_shipments_map_via_sku(self):
        self.add(asin="B0AAAAAAAA", last_sku="SKU_A")
        sp = self.sp(
            {"SKU_A": self.inv("B0AAAAAAAA", 1, 0, 0), "SKU_A2": self.inv("B0AAAAAAAA", 3, 3, 2),
             "OTHER": self.inv("B0ZZZZZZZZ", 99)},
            {"SKU_A": {"units_shipped": 4, "last_shipment_date": "2026-09-19"},
             "SKU_A2": {"units_shipped": 1, "last_shipment_date": "2026-09-20"}})
        result = ReplenA2AService.refresh_stock(sp)
        row = self.get()
        self.assertEqual((row.stock_total, row.stock_fulfillable, row.stock_inbound), (4, 3, 2))
        self.assertEqual((row.units_30d, row.last_sale_date), (5, "2026-09-20"))
        self.assertIsNotNone(row.stock_checked_at)
        self.assertTrue(result["stock"] and result["shipments"])
        sp.get_inventory_summaries.assert_called_once_with("UK", all_pages=True)

    def test_shipment_sku_missing_from_inventory_falls_back_to_the_buy_sheet_sku(self):
        self.add(last_sku="SKU_A")
        ReplenA2AService.refresh_stock(self.sp({}, {"SKU_A": {"units_shipped": 2, "last_shipment_date": "2026-09-01"}}))
        self.assertEqual(self.get().units_30d, 2)

    def test_asin_absent_from_a_complete_listing_has_zero_stock_and_zero_sales(self):
        self.add(stock_total=9, units_30d=9)
        ReplenA2AService.refresh_stock(self.sp({"X": self.inv("B0ZZZZZZZZ", 5)}, {}))
        row = self.get()
        self.assertEqual((row.stock_total, row.units_30d), (0, 0))

    def test_failed_calls_leave_existing_numbers_alone(self):
        self.add(stock_total=9, units_30d=7)
        result = ReplenA2AService.refresh_stock(self.sp(None, None))
        row = self.get()
        self.assertEqual((row.stock_total, row.units_30d), (9, 7))
        self.assertIn("error", result)

    def test_one_failed_half_does_not_wipe_the_other(self):
        self.add(stock_total=9, units_30d=7)
        ReplenA2AService.refresh_stock(self.sp({"SKU_A": self.inv("B0AAAAAAAA", 2)}, None))
        row = self.get()
        self.assertEqual((row.stock_total, row.units_30d), (2, 7))

    def test_unconfigured_sp_api_is_reported_not_raised(self):
        with patch("app.sp_api.client.get_sp_api_client", return_value=None):
            self.assertIn("error", ReplenA2AService.refresh_stock())


class CheckTests(_DbCase):
    def opp(self, asin, **product):
        base = dict(asin=asin, best_source_cost_gbp=12.5, best_source_marketplace="ES", buy_box_now=31.0,
                    buy_box_90d=30.0, offers_now=4, monthly_sales=50, roi=55.0, roi_90d=60.0, profit=8.0,
                    gated=False, title="Keepa title", brand="Andis")
        base.update(product)
        return {"product": base, "report": {"filtered_reason": base.pop("_reason", "")}}

    def scan_returning(self, opportunities, **extra):
        result = {"opportunities": opportunities, "completed_asins": [o["product"]["asin"] for o in opportunities],
                  "uk_ran_out": False, "tokens_remaining": 500}
        result.update(extra)
        return patch("app.services.brand_scan_service.BrandScanService",
                     return_value=MagicMock(scan=MagicMock(return_value=result)))

    def test_priced_asin_is_recorded_and_todays_roi_is_not_max_of_90d(self):
        self.add(title="")
        with self.scan_returning([self.opp("B0AAAAAAAA", roi=-4.0, roi_90d=40.0)]) as scanner:
            out = ReplenA2AService.check_asins(["B0AAAAAAAA"])
        row = self.get()
        self.assertEqual((out["checked"], out["attempted"]), (1, 1))
        self.assertEqual((row.current_roi, row.current_roi_90d), (-4.0, 40.0))
        self.assertEqual((row.current_source_marketplace, row.current_source_cost_gbp, row.title), ("ES", 12.5, "Keepa title"))
        self.assertIsNotNone(row.last_checked_at)
        kwargs = scanner.return_value.scan.call_args.kwargs
        self.assertTrue(kwargs["force_rescan"] and kwargs["include_no_eu_source"])

    def test_filtered_asin_keeps_its_reason_and_clears_stale_roi(self):
        self.add(current_roi=50.0, current_source_cost_gbp=10.0)
        o = self.opp("B0AAAAAAAA", best_source_cost_gbp=0, roi=0, profit=0)
        o["report"]["filtered_reason"] = "dead_listing"
        with self.scan_returning([o]):
            ReplenA2AService.check_asins(["B0AAAAAAAA"])
        row = self.get()
        self.assertEqual(row.filtered_reason, "dead_listing")
        self.assertIsNone(row.current_roi)
        self.assertIsNone(row.current_source_cost_gbp)

    def test_looked_up_but_not_returned_is_no_keepa_data(self):
        self.add()
        with self.scan_returning([], completed_asins=[]):
            ReplenA2AService.check_asins(["B0AAAAAAAA"])
        row = self.get()
        self.assertTrue(row.no_keepa_data)
        self.assertIsNotNone(row.last_checked_at)

    def test_asin_never_reached_when_tokens_ran_out_is_left_for_retry(self):
        self.add(asin="B0AAAAAAAA")
        self.add(asin="B0BBBBBBBB")
        with self.scan_returning([self.opp("B0AAAAAAAA")], completed_asins=["B0AAAAAAAA"], uk_ran_out=True):
            out = ReplenA2AService.check_asins(["B0AAAAAAAA", "B0BBBBBBBB"])
        self.assertTrue(out["stopped_early"])
        self.assertIsNotNone(self.get("B0AAAAAAAA").last_checked_at)
        untouched = self.get("B0BBBBBBBB")
        self.assertIsNone(untouched.last_checked_at)
        self.assertFalse(untouched.no_keepa_data)

    def test_scan_error_stops_and_changes_nothing(self):
        self.add()
        with self.scan_returning([], error="Only 3 Keepa tokens left"):
            out = ReplenA2AService.check_asins(["B0AAAAAAAA"])
        self.assertTrue(out["stopped_early"])
        self.assertIsNone(self.get().last_checked_at)

    def test_asins_go_to_keepa_in_batches_of_twenty(self):
        for i in range(45):
            self.add(asin=f"B{i:09d}")
        scanner = MagicMock(scan=MagicMock(return_value={"opportunities": [], "completed_asins": [], "uk_ran_out": False}))
        with patch("app.services.brand_scan_service.BrandScanService", return_value=scanner):
            ReplenA2AService.check_asins([f"B{i:09d}" for i in range(45)])
        self.assertEqual([len(c.kwargs["asins"]) for c in scanner.scan.call_args_list], [20, 20, 5])


class VerdictAndAlertTests(_DbCase):
    def priced(self, **kw):
        base = dict(last_checked_at=NOW, current_source_cost_gbp=12.5, current_source_marketplace="ES", current_roi=60.0,
                    buy_box_now=32.0, buy_box_90d=31.0, status=svc.UNCHECKED)
        base.update(kw)
        self.add(**base)

    def run_reclassify(self):
        with patch("app.services.discord_notifier.DiscordNotifier.notify_replen_buy_more", return_value=True) as ping:
            return ReplenA2AService.reclassify_all(), ping

    def test_first_ever_status_never_pings(self):
        self.priced()
        result, ping = self.run_reclassify()
        self.assertEqual(self.get().status, svc.BUY_MORE)
        ping.assert_not_called()
        self.assertEqual(result["alerts"], 0)

    def test_flip_into_buy_more_pings_once_and_respects_the_cooldown(self):
        self.priced(status=svc.WAIT_COST)
        result, ping = self.run_reclassify()
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(ping.call_args.args[0]["asin"], "B0AAAAAAAA")
        self.assertIsNotNone(self.get().alerted_at)

        # Already BUY_MORE -> no repeat.
        result, ping = self.run_reclassify()
        ping.assert_not_called()

        # Drops back, then flips up again inside the 7-day cooldown -> still quiet.
        with self.sessions() as db:
            db.query(ReplenA2AItem).one().status = svc.WAIT_COST
            db.commit()
        result, ping = self.run_reclassify()
        ping.assert_not_called()

        # ...but after the cooldown it announces again.
        with self.sessions() as db:
            row = db.query(ReplenA2AItem).one()
            row.status = svc.WAIT_COST
            row.alerted_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
            db.commit()
        result, ping = self.run_reclassify()
        self.assertEqual(result["alerts"], 1)

    def test_failed_ping_is_retried_next_time_not_swallowed(self):
        self.priced(status=svc.WAIT_COST)
        with patch("app.services.discord_notifier.DiscordNotifier.notify_replen_buy_more", return_value=False):
            ReplenA2AService.reclassify_all()
        self.assertIsNone(self.get().alerted_at)

    def test_ignored_items_never_ping(self):
        self.priced(status=svc.WAIT_COST, ignored=True)
        result, ping = self.run_reclassify()
        ping.assert_not_called()

    def test_status_change_is_timestamped_only_when_it_changes(self):
        self.priced(status=svc.WAIT_COST)
        self.run_reclassify()
        first = self.get().status_changed_at
        self.assertIsNotNone(first)
        self.run_reclassify()
        self.assertEqual(self.get().status_changed_at, first)


class RunDailyTests(_DbCase):
    def test_lock_busy_defers_keepa_but_still_reclassifies(self):
        self.add(last_checked_at=None)
        with patch.object(ReplenA2AService, "sync_purchases", return_value={"asins": 1, "added": 0}), \
             patch.object(ReplenA2AService, "_stock_is_fresh", return_value=True), \
             patch.object(ReplenA2AService, "check_asins") as check, \
             patch("app.services.scan_coordinator.ScanCoordinator.try_acquire_for_automated_tick", return_value=False):
            out = ReplenA2AService.run_daily()
        self.assertTrue(out["deferred"])
        check.assert_not_called()
        self.assertIn("verdicts", out)

    def test_lock_is_released_even_if_the_check_raises(self):
        self.add(last_checked_at=None)
        with patch.object(ReplenA2AService, "sync_purchases", return_value={"asins": 1, "added": 0}), \
             patch.object(ReplenA2AService, "_stock_is_fresh", return_value=True), \
             patch.object(ReplenA2AService, "check_asins", side_effect=RuntimeError("boom")), \
             patch("app.services.scan_coordinator.ScanCoordinator.try_acquire_for_automated_tick", return_value=True), \
             patch("app.services.scan_coordinator.ScanCoordinator.release_after_automated_tick") as release:
            with self.assertRaises(RuntimeError):
                ReplenA2AService.run_daily()
        release.assert_called_once()

    def test_sheet_failure_does_not_stop_the_rest(self):
        self.add(last_checked_at=None)
        with patch.object(ReplenA2AService, "sync_purchases", side_effect=RuntimeError("sheets down")), \
             patch.object(ReplenA2AService, "_stock_is_fresh", return_value=True), \
             patch.object(ReplenA2AService, "check_asins", return_value={"attempted": 1, "checked": 1, "total": 1,
                                                                        "stopped_early": False}), \
             patch("app.services.scan_coordinator.ScanCoordinator.try_acquire_for_automated_tick", return_value=True), \
             patch("app.services.scan_coordinator.ScanCoordinator.release_after_automated_tick"):
            out = ReplenA2AService.run_daily()
        self.assertIn("error", out["sync"])
        self.assertEqual(out["check"]["checked"], 1)
        self.assertIn("FAILED", ReplenA2AService.summarise(out))


class DailyCapTests(_DbCase):
    """Automated runs share ONE quota per ~20h, so a failed-then-retried run can't burn the list."""

    def run_auto(self):
        with patch.object(ReplenA2AService, "sync_purchases", return_value={"asins": 1, "added": 0}), \
             patch.object(ReplenA2AService, "_stock_is_fresh", return_value=True), \
             patch.object(ReplenA2AService, "check_asins",
                          return_value={"attempted": 1, "checked": 1, "total": 1, "stopped_early": False}) as check, \
             patch("app.services.scan_coordinator.ScanCoordinator.try_acquire_for_automated_tick", return_value=True), \
             patch("app.services.scan_coordinator.ScanCoordinator.release_after_automated_tick"):
            out = ReplenA2AService.run_daily()
        return out, check

    def seed(self, n, priced_recently):
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        for i in range(n):
            self.add(asin=f"B{i:09d}", last_checked_at=recent if i < priced_recently else None)

    def test_fresh_day_takes_the_full_quota(self):
        self.seed(40, 0)
        out, check = self.run_auto()
        self.assertEqual(len(check.call_args.args[0]), 10)          # 25% of 40

    def test_partial_earlier_run_only_tops_up_the_remainder(self):
        self.seed(40, 6)                                            # 6 of today's 10 already done
        out, check = self.run_auto()
        self.assertEqual(len(check.call_args.args[0]), 4)
        self.assertEqual(out["already_priced_today"], 6)

    def test_quota_already_met_skips_keepa_entirely(self):
        self.seed(40, 10)
        out, check = self.run_auto()
        check.assert_not_called()
        self.assertIn("already priced today", ReplenA2AService.summarise(out))

    def test_manual_run_ignores_the_cap(self):
        self.seed(40, 10)
        with patch.object(ReplenA2AService, "sync_purchases", return_value={"asins": 1, "added": 0}), \
             patch.object(ReplenA2AService, "_stock_is_fresh", return_value=True), \
             patch.object(ReplenA2AService, "check_asins",
                          return_value={"attempted": 1, "checked": 1, "total": 1, "stopped_early": False}) as check, \
             patch("app.services.scan_coordinator.ScanCoordinator.acquire_for_manual_scan"), \
             patch("app.services.scan_coordinator.ScanCoordinator.release_after_manual_scan"):
            ReplenA2AService.run_daily(manual=True)
        self.assertEqual(len(check.call_args.args[0]), 10)


class SchedulerRuleTests(unittest.TestCase):
    def ok(self, **overrides):
        result = {"sync": {"asins": 5}, "stock": {"stock": True, "shipments": True},
                  "check": {"stopped_early": False}}
        result.update(overrides)
        return result

    def test_success_and_the_failures_that_must_not_count_as_success(self):
        from app.main import _replen_a2a_run_succeeded as done
        self.assertTrue(done(self.ok()))
        self.assertFalse(done(self.ok(deferred=True)))
        self.assertFalse(done(self.ok(sync={"error": "sheets down"})))
        self.assertFalse(done(self.ok(stock={"error": "sp-api down"})))
        self.assertFalse(done(self.ok(stock={"stock": False, "shipments": False})))
        self.assertFalse(done(self.ok(check={"stopped_early": True})))

    def test_sales_report_failing_alone_is_tolerated_and_reused_stock_is_fine(self):
        from app.main import _replen_a2a_run_succeeded as done
        self.assertTrue(done(self.ok(stock={"stock": True, "shipments": False})))
        self.assertTrue(done(self.ok(stock={"skipped": "refreshed recently"})))
        self.assertTrue(done({"sync": {"asins": 5}, "stock": {"skipped": "x"}}))     # quota already met: no check


class ListRowsTests(_DbCase):
    def test_ordering_filtering_search_and_ignored(self):
        self.add(asin="B0AAAAAAAA", title="Andis blade", status=svc.WAIT_BOTH, current_roi=-20.0)
        self.add(asin="B0BBBBBBBB", title="Kettle", status=svc.BUY_MORE, current_roi=40.0)
        self.add(asin="B0CCCCCCCC", title="Toaster", status=svc.BUY_MORE, current_roi=70.0)
        self.add(asin="B0DDDDDDDD", title="Hidden", status=svc.BUY_MORE, current_roi=90.0, ignored=True)
        data = ReplenA2AService.list_rows()
        self.assertEqual([r["item"].asin for r in data["rows"]], ["B0CCCCCCCC", "B0BBBBBBBB", "B0AAAAAAAA"])
        self.assertEqual((data["counts"]["buy"], data["counts"]["wait"], data["ignored_count"]), (2, 1, 1))
        self.assertEqual([r["item"].asin for r in ReplenA2AService.list_rows(group="wait")["rows"]], ["B0AAAAAAAA"])
        self.assertEqual([r["item"].asin for r in ReplenA2AService.list_rows(query="kett")["rows"]], ["B0BBBBBBBB"])
        self.assertEqual(len(ReplenA2AService.list_rows(show_ignored=True)["rows"]), 4)

    def test_derived_values(self):
        self.add(status=svc.BUY_MORE, stock_total=3, units_30d=6, current_source_cost_gbp=15.21,
                 buy_box_now=28.8, last_bought_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=12))
        r = ReplenA2AService.list_rows()["rows"][0]
        self.assertEqual(r["cover_days"], 15)
        self.assertAlmostEqual(r["cost_delta"], 3.0)
        self.assertAlmostEqual(r["price_delta_pct"], -10.0)
        self.assertEqual(r["days_since_bought"], 12)

    def test_set_ignored_round_trip(self):
        self.add()
        ReplenA2AService.set_ignored("b0aaaaaaaa", True)
        self.assertTrue(self.get().ignored)
        ReplenA2AService.set_ignored("B0AAAAAAAA", False)
        self.assertFalse(self.get().ignored)


class FilterSortPageTests(_DbCase):
    """The filter bar, column sorting and pagination behind list_rows."""

    def setUp(self):
        super().setUp()
        n = datetime.now(timezone.utc).replace(tzinfo=None)
        ago = lambda **kw: n - timedelta(**kw)
        # cost paid 10.00, planned 30.00 unless overridden
        base = dict(last_cost_gbp=10.0, last_sale_price_gbp=30.0, current_source_cost_gbp=10.0, buy_box_now=30.0,
                    current_source_marketplace="DE", category="Toys", brand="Alpha", times_bought=1,
                    last_checked_at=ago(hours=3), last_bought_at=ago(days=100))
        rows = [
            dict(asin="B0000000A1", title="Apple",  status=svc.BUY_MORE, current_roi=60.0, current_profit=8.0, stock_total=0,
                 units_30d=6, last_bought_at=ago(days=10), category="Toys", brand="Alpha", times_bought=3),
            dict(asin="B0000000A2", title="Banana", status=svc.BUY_MORE, current_roi=30.0, current_profit=4.0, stock_total=5,
                 stock_inbound=2, units_30d=3, last_bought_at=ago(days=45), brand="Beta", current_source_marketplace="FR"),
            dict(asin="B0000000A3", title="Cherry", status=svc.STOCKED, current_roi=40.0, current_profit=5.0, stock_total=50,
                 units_30d=0, last_bought_at=ago(days=200), brand="Beta", current_source_marketplace="ES", category="Home"),
            dict(asin="B0000000A4", title="Damson", status=svc.WAIT_COST, current_roi=-15.0, current_profit=-3.0,
                 current_source_cost_gbp=13.0, stock_total=None, units_30d=None, brand="Gamma", category="Home"),
            dict(asin="B0000000A5", title="Elder",  status=svc.WAIT_PRICE, current_roi=5.0, buy_box_now=24.0,
                 last_checked_at=ago(days=9), stock_total=2, units_30d=1, brand="Gamma"),
            dict(asin="B0000000A6", title="Fig",    status=svc.NO_SOURCE, current_roi=None, current_profit=None,
                 current_source_cost_gbp=None, current_source_marketplace="", stock_total=0, units_30d=0),
            dict(asin="B0000000A7", title="Grape",  status=svc.UNCHECKED, current_roi=None, current_source_cost_gbp=None,
                 current_source_marketplace="", last_checked_at=None, stock_total=None, units_30d=None, brand=""),
            dict(asin="B0000000A8", title="Hidden", status=svc.BUY_MORE, current_roi=99.0, ignored=True, stock_total=0),
        ]
        for r in rows:
            self.add(**{**base, **r})

    def asins(self, **kw):
        return [r["item"].asin[-2:] for r in ReplenA2AService.list_rows(**kw)["rows"]]

    def test_stock_filters(self):
        self.assertEqual(sorted(self.asins(stock="instock")), ["A2", "A3", "A5"])
        self.assertEqual(sorted(self.asins(stock="out")), ["A1", "A6"])
        self.assertEqual(self.asins(stock="inbound"), ["A2"])
        self.assertEqual(sorted(self.asins(stock="unknown")), ["A4", "A7"])

    def test_sold_filter_treats_unknown_as_neither(self):
        self.assertEqual(sorted(self.asins(sold="yes")), ["A1", "A2", "A5"])
        self.assertEqual(sorted(self.asins(sold="no")), ["A3", "A6"])          # A4/A7 are None: unknown, not "no"

    def test_bought_filters(self):
        self.assertEqual(self.asins(bought="30"), ["A1"])
        self.assertEqual(sorted(self.asins(bought="90")), ["A1", "A2"])
        self.assertEqual(sorted(self.asins(bought="older")), ["A3", "A4", "A5", "A6", "A7"])
        self.assertEqual(self.asins(bought="repeat"), ["A1"])

    def test_source_market_filter_and_no_source(self):
        self.assertEqual(self.asins(market="fr"), ["A2"])                      # case-insensitive
        self.assertEqual(sorted(self.asins(market="none")), ["A6", "A7"])

    def test_roi_bands(self):
        self.assertEqual(sorted(self.asins(roi="25")), ["A1", "A2", "A3"])
        self.assertEqual(sorted(self.asins(roi="pos")), ["A1", "A2", "A3", "A5"])
        self.assertEqual(self.asins(roi="neg"), ["A4"])
        self.assertEqual(sorted(self.asins(roi="none")), ["A6", "A7"])

    def test_trend_filters_and_their_ten_percent_boundary(self):
        self.assertEqual(self.asins(trend="costup"), ["A4"])                   # 13.00 vs 10.00 paid
        self.assertEqual(self.asins(trend="pricedown"), ["A5"])                # 24 vs 30 planned
        self.assertEqual(self.asins(trend="costdown"), [])
        self.add(asin="B0000000B1", title="Edge", last_cost_gbp=10.0, last_sale_price_gbp=30.0,
                 current_source_cost_gbp=11.0, buy_box_now=27.0, status=svc.WAIT)
        # exactly +10% cost and exactly -10% price are noise, not a trend
        self.assertNotIn("B1", self.asins(trend="costup"))
        self.assertNotIn("B1", self.asins(trend="pricedown"))

    def test_priced_filters(self):
        self.assertEqual(self.asins(checked="never"), ["A7"])
        self.assertEqual(self.asins(checked="7d"), ["A5"])
        self.assertNotIn("A7", self.asins(checked="24h"))
        self.assertIn("A1", self.asins(checked="24h"))

    def test_brand_and_category_exact_and_case_insensitive(self):
        self.assertEqual(sorted(self.asins(brand="BETA")), ["A2", "A3"])
        self.assertEqual(sorted(self.asins(category="home")), ["A3", "A4"])

    def test_filters_combine_with_and_and_with_the_verdict_group(self):
        self.assertEqual(self.asins(group="buy", stock="out"), ["A1"])
        self.assertEqual(self.asins(stock="instock", brand="beta"), ["A2", "A3"])
        self.assertEqual(self.asins(group="wait", roi="neg"), ["A4"])
        self.assertEqual(self.asins(query="ban", stock="instock"), ["A2"])
        self.assertEqual(self.asins(query="ban", stock="out"), [])

    def test_chip_counts_respect_the_other_filters_but_not_the_group(self):
        data = ReplenA2AService.list_rows(group="wait", stock="instock")
        self.assertEqual(data["counts"]["buy"], 1)             # A2 only (A1 is out of stock)
        self.assertEqual(data["counts"]["stocked"], 1)
        self.assertEqual(data["counts"]["wait"], 1)            # A5 -- the group itself is NOT applied to its own count
        self.assertEqual(data["matching"], 3)
        self.assertEqual(data["filtered"], 1)
        self.assertEqual(data["total"], 7)                     # hidden item excluded from the headline

    def test_unknown_filter_and_sort_values_are_ignored_not_errors(self):
        everything = self.asins()
        self.assertEqual(self.asins(stock="banana", sold="x", roi="?", trend="!", checked="~", bought="zz"), everything)
        self.assertEqual(self.asins(sort="nonsense", order="sideways"), everything)
        self.assertEqual(self.asins(group="nonsense"), everything)

    def test_default_sort_is_verdict_then_roi(self):
        self.assertEqual(self.asins(), ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])   # BUY(60),BUY(30),STOCKED,WAIT_COST,WAIT_PRICE,NO_SOURCE,UNCHECKED

    def test_each_sort_key_and_its_default_direction(self):
        self.assertEqual(self.asins(sort="roi")[:3], ["A1", "A3", "A2"])              # highest first
        self.assertEqual(self.asins(sort="title")[:3], ["A1", "A2", "A3"])            # A-Z
        self.assertEqual(self.asins(sort="bought")[:2], ["A1", "A2"])                 # most recent first
        self.assertEqual(self.asins(sort="stock")[:2], ["A3", "A2"])                  # most stock first
        self.assertEqual(self.asins(sort="sold")[:2], ["A1", "A2"])
        self.assertEqual(self.asins(sort="cover")[:2], ["A2", "A5"])                   # least cover first: 50d then 60d

    def test_order_overrides_the_default_direction(self):
        self.assertEqual(self.asins(sort="roi", order="asc")[:2], ["A4", "A5"])
        self.assertEqual(self.asins(sort="title", order="desc")[:2], ["A7", "A6"])

    def test_rows_with_no_value_always_sort_last_in_either_direction(self):
        for order in ("asc", "desc"):
            ordered = self.asins(sort="roi", order=order)
            self.assertEqual(set(ordered[-2:]), {"A6", "A7"}, order)

    def test_pagination_slices_clamps_and_reports(self):
        page1 = ReplenA2AService.list_rows(page=1, page_size=3)
        self.assertEqual((len(page1["rows"]), page1["pages"], page1["filtered"]), (3, 3, 7))
        page3 = ReplenA2AService.list_rows(page=3, page_size=3)
        self.assertEqual(len(page3["rows"]), 1)
        clamped = ReplenA2AService.list_rows(page=99, page_size=3)
        self.assertEqual((clamped["page"], len(clamped["rows"])), (3, 1))
        self.assertEqual(ReplenA2AService.list_rows(page=-4, page_size=3)["page"], 1)
        all_rows = ReplenA2AService.list_rows(page=5, page_size=0)
        self.assertEqual((all_rows["page"], all_rows["pages"], len(all_rows["rows"])), (1, 1, 7))

    def test_pages_cover_every_row_exactly_once_in_sort_order(self):
        seen = []
        for p in (1, 2, 3):
            seen += [r["item"].asin for r in ReplenA2AService.list_rows(sort="roi", page=p, page_size=3)["rows"]]
        whole = [r["item"].asin for r in ReplenA2AService.list_rows(sort="roi")["rows"]]
        self.assertEqual(seen, whole)
        self.assertEqual(len(set(seen)), 7)

    def test_dropdown_options_are_tallied_from_the_visible_list(self):
        options = ReplenA2AService.list_rows()["options"]
        self.assertEqual(dict(options["brands"]), {"Alpha": 2, "Beta": 2, "Gamma": 2})   # blank brand and hidden row excluded
        self.assertEqual(dict(options["categories"])["Home"], 2)
        self.assertEqual(dict(options["markets"]), {"DE": 3, "FR": 1, "ES": 1})


class ReplenRouteTests(_DbCase):
    """The page itself: links carry the view, bad input can't 500, buttons return you to where you were."""

    def setUp(self):
        super().setUp()
        for i in range(120):
            self.add(asin=f"B{i:09d}", title=f"Item {i:03d}", status=svc.BUY_MORE if i % 4 == 0 else svc.WAIT,
                     current_roi=float(i), brand="Alpha" if i % 2 else "Beta", stock_total=i % 3, units_30d=1,
                     last_checked_at=NOW, current_source_cost_gbp=10.0, current_source_marketplace="DE")
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def get(self, url, **kw):
        response = self.client.get(url, **kw)
        self.assertEqual(response.status_code, 200, url)
        return response.text

    def test_default_page_is_fifty_rows_and_pages_link_forward(self):
        html = self.get("/replen")
        self.assertEqual(html.count('<tr id="replen-'), 50)
        self.assertIn("Showing 1&ndash;50 of", html)
        self.assertIn("page=2", html)
        self.assertEqual(self.get("/replen?page=3").count('<tr id="replen-'), 20)
        self.assertEqual(self.get("/replen?per=0").count('<tr id="replen-'), 120)
        self.assertEqual(self.get("/replen?per=25").count('<tr id="replen-'), 25)

    def test_filters_show_as_removable_pills_and_survive_paging_and_sorting(self):
        html = self.get("/replen?stock=instock&brand=Alpha&sort=roi&per=25")
        self.assertIn("Stock: In stock", html)
        self.assertIn("Brand: Alpha", html)
        self.assertIn("Clear all", html)
        # next-page and re-sort links keep the filters
        self.assertRegex(html, r'href="/replen\?[^"]*stock=instock[^"]*page=2')
        self.assertRegex(html, r'href="/replen\?[^"]*brand=Alpha[^"]*sort=title')

    def test_changing_a_filter_or_sort_returns_to_page_one(self):
        html = self.get("/replen?page=3&stock=instock")
        pill_link = __import__("re").search(r'href="([^"]+)" title="Remove this filter"', html).group(1)
        self.assertNotIn("page=", pill_link)
        self.assertNotIn("stock=", pill_link)

    def test_clicking_the_sorted_column_flips_direction(self):
        self.assertIn("order=desc", self.get("/replen?sort=title"))          # title runs A-Z, so next click is desc
        self.assertIn("order=asc", self.get("/replen?sort=roi"))             # roi runs high-low, next click is asc
        html = self.get("/replen?sort=roi&order=asc")
        self.assertIn("sorted low to high", html.lower())

    def test_hostile_or_garbled_query_strings_do_not_error(self):
        for url in ("/replen?sort=<script>", "/replen?per=abc", "/replen?page=-1", "/replen?page=99999",
                    "/replen?stock=%27%20OR%201=1", "/replen?brand=%00", "/replen?group=zzz&roi=zzz"):
            self.get(url)
        self.assertNotIn("<script>alert(1)</script>", self.get("/replen?q=%3Cscript%3Ealert(1)%3C/script%3E"))

    def test_nothing_matches_offers_a_way_out(self):
        html = self.get("/replen?q=zzzznomatch")
        self.assertIn("Nothing matches those filters", html)
        self.assertIn("Clear all filters", html)

    def test_buttons_return_to_the_same_view(self):
        view = "group=buy&stock=instock&sort=roi&page=2&per=25"
        r = self.client.post("/replen/ignore", data={"asin": "B000000000", "view": view}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        for part in ("group=buy", "stock=instock", "sort=roi", "page=2", "per=25", "message="):
            self.assertIn(part, r.headers["location"])
        r = self.client.post("/replen/unignore", data={"asin": "B000000000"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)                                  # no view posted: still fine

    def test_row_buttons_carry_the_current_view_in_a_hidden_field(self):
        html = self.get("/replen?stock=instock&page=2&per=25")
        self.assertRegex(html, r'name="view" value="[^"]*stock=instock[^"]*page=2')

    def test_brand_name_in_a_row_is_a_filter_link(self):
        self.assertIn('href="/replen?brand=Alpha"', self.get("/replen"))


def _response(status=200, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    return response


class InventoryPaginationTests(unittest.TestCase):
    """Amazon puts `pagination` beside `payload`, and a nextToken call must repeat the marketplace params."""

    def setUp(self):
        self.client = SPAPIClient("id", "secret", "token", seller_id="SELLER1")
        for target, value in (("_get_access_token", "tok"), ("_pace", None)):
            patcher = patch.object(self.client, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def page(self, skus, token=None):
        body = {"payload": {"inventorySummaries": [{"sellerSku": s, "asin": "A" + s, "totalQuantity": 1} for s in skus]}}
        if token:
            body["pagination"] = {"nextToken": token}
        return _response(payload=body)

    def test_all_pages_follows_the_top_level_token_and_repeats_marketplace_params(self):
        with patch.object(client_module.requests, "get",
                          side_effect=[self.page(["S1", "S2"], "TOK1"), self.page(["S3"])]) as get:
            result = self.client.get_inventory_summaries("UK", all_pages=True)
        self.assertEqual(set(result), {"S1", "S2", "S3"})
        first, second = get.call_args_list[0].kwargs["params"], get.call_args_list[1].kwargs["params"]
        self.assertNotIn("nextToken", first)
        self.assertEqual(second["nextToken"], "TOK1")
        self.assertEqual(second["marketplaceIds"], first["marketplaceIds"])
        self.assertEqual(second["granularityId"], first["granularityId"])

    def test_all_pages_is_the_default_and_first_page_only_is_opt_in(self):
        with patch.object(client_module.requests, "get",
                          side_effect=[self.page(["S1"], "TOK1"), self.page(["S2"])]) as get:
            self.assertEqual(set(self.client.get_inventory_summaries("UK")), {"S1", "S2"})
        self.assertEqual(get.call_count, 2)

        with patch.object(client_module.requests, "get", side_effect=[self.page(["S1", "S2"], "TOK1")]) as get:
            result = self.client.get_inventory_summaries("UK", all_pages=False)
        self.assertEqual(set(result), {"S1", "S2"})
        self.assertEqual(get.call_count, 1)

    def test_a_failed_later_page_is_no_answer_not_a_partial_list(self):
        with patch.object(client_module.requests, "get", side_effect=[self.page(["S1"], "TOK1"), _response(500)]):
            self.assertIsNone(self.client.get_inventory_summaries("UK", all_pages=True))


if __name__ == "__main__":
    unittest.main()
