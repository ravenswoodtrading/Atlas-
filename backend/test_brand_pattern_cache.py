"""Page-load brand pattern lookup (2026-09-21): brand_sourcing_pattern_cached must give the SAME answer as the
per-brand query it replaces on the Command Centre / Review Queue path, from one query, and refresh after its TTL.
Isolated in-memory SQLite -- no live database."""
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services import review_queue_service as rq
from app.services import seller_watch_service as sw
from app.services.seller_watch_service import SellerWatchService


class BrandPatternCacheTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        p = patch.object(sw, "SessionLocal", self.sessions)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        sw._BRAND_PATTERN_CACHE.update({"table": None, "at": 0.0})
        self.addCleanup(lambda: sw._BRAND_PATTERN_CACHE.update({"table": None, "at": 0.0}))
        with self.sessions() as db:
            db.add(TrackedSeller(seller_id="A1SELLER", nickname="Seller"))
            db.commit()
            self.seller_id = db.query(TrackedSeller.id).scalar()
        self.n = 0

    def listing(self, brand, tag, dismissed=False):
        self.n += 1
        with self.sessions() as db:
            rec = ProductRecord(asin=f"B0PAT{self.n:05d}", title="t", brand=brand, scanned_at=datetime.now(timezone.utc).replace(tzinfo=None))
            db.add(rec)
            db.flush()
            db.add(SellerNewListing(tracked_seller_id=self.seller_id, asin=rec.asin, product_record_id=rec.id,
                                    sourcing_tag=tag, dismissed=dismissed))
            db.commit()

    def populate(self):
        for _ in range(3): self.listing("Worx", "EU A2A")
        self.listing("WORX", "OA")                       # different case, same brand
        for _ in range(4): self.listing("Philips", "OA")
        self.listing("Philips", "EU A2A")
        for _ in range(5): self.listing("Makita", "EU A2A", dismissed=True)   # dismissed: doesn't count
        self.listing("Makita", "EU A2A")
        self.listing("Makita", None)                     # no tag: doesn't count
        self.listing("Bosch", "EU A2A")                  # below the minimum sample
        self.listing(None, "EU A2A")                     # no brand at all
        self.listing("Société", "EU A2A")
        for _ in range(2): self.listing("société", "OA")

    def test_it_agrees_with_the_per_brand_query_for_every_kind_of_brand(self):
        self.populate()
        for brand in ("Worx", "WORX", "worx", "Philips", "Makita", "Bosch", "Unknown", "", None, "Société", "société", "SOCIÉTÉ"):
            with self.subTest(brand=brand):
                sw._BRAND_PATTERN_CACHE.update({"table": None, "at": 0.0})
                self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached(brand),
                                 SellerWatchService.brand_sourcing_pattern(brand))

    def test_the_numbers_are_the_expected_ones(self):
        self.populate()
        self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached("worx"), {"sample_size": 4, "eu_a2a_pct": 75.0})
        self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached("Philips"), {"sample_size": 5, "eu_a2a_pct": 20.0})
        self.assertIsNone(SellerWatchService.brand_sourcing_pattern_cached("Makita"))   # 1 counted row: under the floor
        self.assertIsNone(SellerWatchService.brand_sourcing_pattern_cached("Bosch"))

    def test_many_lookups_cost_one_query(self):
        self.populate()
        statements = []
        event.listen(self.engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
        for brand in ["Worx", "Philips", "Makita", "Bosch", "Unknown"] * 50:
            SellerWatchService.brand_sourcing_pattern_cached(brand)
        self.assertEqual(len(statements), 1)

    def test_it_refreshes_after_its_time_is_up_and_not_before(self):
        self.populate()
        self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached("Philips")["sample_size"], 5)
        self.listing("Philips", "OA")
        self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached("Philips")["sample_size"], 5)   # still cached
        real = sw.time.monotonic()
        with patch.object(sw.time, "monotonic", return_value=real + sw.BRAND_PATTERN_CACHE_SECONDS + 1):
            self.assertEqual(SellerWatchService.brand_sourcing_pattern_cached("Philips")["sample_size"], 6)

    def test_the_live_lookup_is_untouched_and_never_stale(self):
        self.populate()
        SellerWatchService.brand_sourcing_pattern_cached("Philips")
        self.listing("Philips", "OA")
        self.assertEqual(SellerWatchService.brand_sourcing_pattern("Philips")["sample_size"], 6)

    def test_the_review_queue_page_path_uses_the_cached_lookup(self):
        src = inspect.getsource(rq.ReviewQueueService._competitor_lead_dict)
        self.assertIn("brand_sourcing_pattern_cached", src)
        self.assertNotIn("brand_sourcing_pattern(", src)


if __name__ == "__main__":
    unittest.main()
