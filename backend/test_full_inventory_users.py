"""The three features that consume SPAPIClient.get_inventory_summaries, now that it returns the FULL
inventory instead of the first 50 SKUs (2026-09-20): Inventory Cleanup, Storage Fee Watch, and Lead
Analysis's stock snapshot. Isolated in-memory SQLite and fake SP-API clients -- no live calls or writes."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import OutOfStockListing, StorageFeeWatch
from app.services import inventory_cleanup_service as ics
from app.services import lead_analysis_service as las
from app.services import storage_fee_service as sfs
from app.services.inventory_cleanup_service import InventoryCleanupService
from app.services.lead_analysis_service import LeadAnalysisService
from app.services.storage_fee_service import StorageFeeService

NOW = datetime.now(timezone.utc)


def sku_dated(days_ago, tag="AMA_10.00_20.00_3_"):
    d = NOW - timedelta(days=days_ago)
    return f"{tag}{d.day:02d}{d.strftime('%b').upper()}"


def inv(asin, fulfillable=0, inbound=0, reserved=0, title="Thing"):
    return {"asin": asin, "title": title, "fulfillable": fulfillable, "inbound_working": inbound,
            "inbound_shipped": 0, "inbound_receiving": 0, "reserved": reserved,
            "total": fulfillable + inbound + reserved}


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for module in (ics, sfs):
            for target, value in (("SessionLocal", self.sessions), ("ActivityLog", MagicMock())):
                patcher = patch.object(module, target, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def sp(self, inventory, shipments=None, **extra):
        sp = MagicMock()
        sp.get_inventory_summaries.return_value = inventory
        sp.get_fba_fulfilled_shipments_units.return_value = {} if shipments is None else shipments
        for name, value in extra.items():
            getattr(sp, name).return_value = value
        return sp

    def rows(self):
        with self.sessions() as db:
            out = {r.sku: (r.status, r.sold_evidence) for r in db.query(OutOfStockListing).all()}
        return out


class InventoryCleanupFullListTests(_Db):
    def sweep(self, sp):
        with patch.object(ics, "get_sp_api_client", return_value=sp):
            return InventoryCleanupService.detect_out_of_stock_candidates()

    def big_inventory(self):
        """60 old zero-stock SKUs -- more than the 50 a first-page-only read could ever have shown."""
        inventory = {sku_dated(30 + i % 20, f"AMA_{i}_20_3_"): inv(f"B0{i:08d}") for i in range(60)}
        inventory[sku_dated(40, "AMA_STOCKED_")] = inv("B0STOCKED01", fulfillable=4)
        inventory[sku_dated(40, "AMA_INBOUND_")] = inv("B0INBOUND01", inbound=6)
        inventory[sku_dated(40, "AMA_RESERVED_")] = inv("B0RESERVED1", reserved=1)
        inventory[sku_dated(5, "AMA_NEWLIST_")] = inv("B0NEWLIST01")          # listed 5 days ago: never stocked yet
        return inventory

    def test_every_old_zero_stock_sku_is_tracked_not_just_the_first_50(self):
        result = self.sweep(self.sp(self.big_inventory()))
        self.assertEqual(result["checked"], 64)
        self.assertEqual(result["flagged"], 60)
        rows = self.rows()
        self.assertEqual(len(rows), 60)
        self.assertTrue(all(status == "monitoring" for status, _ in rows.values()))

    def test_stocked_inbound_reserved_and_recently_listed_skus_are_never_flagged(self):
        self.sweep(self.sp(self.big_inventory()))
        tracked = self.rows()
        for tag in ("AMA_STOCKED_", "AMA_INBOUND_", "AMA_RESERVED_", "AMA_NEWLIST_"):
            self.assertFalse(any(tag in sku for sku in tracked), tag)

    def test_undated_sku_needs_a_shipment_in_the_sales_report(self):
        inventory = {"BRANDEDSKU1": inv("B0AAAAAAAA"), "BRANDEDSKU2": inv("B0BBBBBBBB")}
        self.sweep(self.sp(inventory, {"BRANDEDSKU1": {"units_shipped": 3, "last_shipment_date": "2026-09-10"}}))
        self.assertEqual(self.rows(), {"BRANDEDSKU1": ("monitoring", "sales_report")})

    def test_a_restocked_sku_is_cleared(self):
        inventory = self.big_inventory()
        self.sweep(self.sp(inventory))
        restocked = next(iter(self.rows()))
        inventory[restocked] = inv("B0RESTOCKED", fulfillable=10)
        result = self.sweep(self.sp(inventory))
        self.assertEqual(result["restocked_cleared"], 1)
        self.assertNotIn(restocked, self.rows())

    def test_failed_inventory_call_flags_and_clears_nothing(self):
        self.sweep(self.sp(self.big_inventory()))
        before = self.rows()
        result = self.sweep(self.sp(None))
        self.assertEqual(result["checked"], 0)
        self.assertEqual(self.rows(), before)

    def test_only_a_human_can_move_a_listing_to_deleted(self):
        # Even a SKU flagged 30 days ago just sits in pending_review after a sweep; nothing calls delete.
        inventory = {sku_dated(60): inv("B0OLD000001")}
        sp = self.sp(inventory)
        self.sweep(sp)
        with self.sessions() as db:
            row = db.query(OutOfStockListing).one()
            row.first_out_of_stock_at = NOW - timedelta(days=30)
            db.commit()
        self.sweep(sp)
        self.assertEqual(list(self.rows().values())[0][0], "pending_review")
        sp.delete_listing_item.assert_not_called()
        sp.get_inventory_summaries.assert_called_with("UK")


class StorageFeeFullListTests(_Db):
    def test_asin_to_sku_join_works_beyond_the_first_50_skus(self):
        inventory = {f"SKU{i}": inv(f"B0{i:08d}") for i in range(120)}
        fees = {"B0" + f"{110:08d}": {"storage_fee_amount": 5.0, "average_quantity_on_hand": 3,
                                        "month_of_charge": "2026-08", "currency": "GBP", "title": "Far down the list"}}
        sp = self.sp(inventory, {}, get_storage_fee_charges=fees, get_longterm_storage_fee_charges={})
        with patch.object(sfs, "get_sp_api_client", return_value=sp):
            result = StorageFeeService.refresh()
        self.assertEqual(result["checked"], 1)
        with self.sessions() as db:
            row = db.query(StorageFeeWatch).one()
        self.assertEqual((row.sku, row.asin, row.storage_fee_amount), ("SKU110", "B0" + f"{110:08d}", 5.0))
        self.assertTrue(row.should_consider_shifting)


class LeadInventorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(las._inventory_snapshot_cache)
        las._inventory_snapshot_cache.update(at=None, data={}, retry_at=None)
        self.addCleanup(lambda: las._inventory_snapshot_cache.update(self.saved))
        clock = patch.object(las.time, "monotonic", side_effect=lambda: self.now)
        self.now = 1000.0
        clock.start()
        self.addCleanup(clock.stop)

    def snapshot(self, sp):
        with patch.object(las, "get_sp_api_client", return_value=sp):
            return LeadAnalysisService._fetch_inventory_snapshot()

    def sp(self, summaries):
        sp = MagicMock()
        sp.get_inventory_summaries.return_value = summaries
        return sp

    def test_keyed_by_asin_keeping_the_sku_with_most_fulfillable(self):
        sp = self.sp({"S1": inv("B0AAAAAAAA", fulfillable=2), "S2": inv("B0AAAAAAAA", fulfillable=9)})
        self.assertEqual(self.snapshot(sp)["B0AAAAAAAA"]["fulfillable"], 9)

    def test_repeated_ticks_within_the_ttl_make_one_sp_api_pull(self):
        sp = self.sp({"S1": inv("B0AAAAAAAA", fulfillable=2)})
        for _ in range(5):                       # the lead scheduler ticks every 30s
            self.now += 30
            self.snapshot(sp)
        self.assertEqual(sp.get_inventory_summaries.call_count, 1)
        self.now += las.INVENTORY_SNAPSHOT_TTL_SECONDS
        self.snapshot(sp)
        self.assertEqual(sp.get_inventory_summaries.call_count, 2)

    def test_failed_refresh_reuses_the_last_good_snapshot_and_backs_off(self):
        good = self.sp({"S1": inv("B0AAAAAAAA", fulfillable=2)})
        self.snapshot(good)
        self.now += las.INVENTORY_SNAPSHOT_TTL_SECONDS + 1
        failing = self.sp(None)
        self.assertIn("B0AAAAAAAA", self.snapshot(failing))            # stale beats nothing
        for _ in range(5):                                             # ...and it doesn't retry every 30s
            self.now += 30
            self.snapshot(failing)
        self.assertEqual(failing.get_inventory_summaries.call_count, 1)
        self.now += las.INVENTORY_SNAPSHOT_RETRY_SECONDS
        self.snapshot(failing)
        self.assertEqual(failing.get_inventory_summaries.call_count, 2)

    def test_snapshot_too_old_is_not_reused(self):
        self.snapshot(self.sp({"S1": inv("B0AAAAAAAA", fulfillable=2)}))
        self.now += las.INVENTORY_SNAPSHOT_MAX_STALE_SECONDS + 1
        self.assertEqual(self.snapshot(self.sp(None)), {})

    def test_never_raises_and_returns_empty_when_unconfigured(self):
        with patch.object(las, "get_sp_api_client", return_value=None):
            self.assertEqual(LeadAnalysisService._fetch_inventory_snapshot(), {})
        boom = MagicMock()
        boom.get_inventory_summaries.side_effect = RuntimeError("down")
        self.assertEqual(self.snapshot(boom), {})


if __name__ == "__main__":
    unittest.main()
