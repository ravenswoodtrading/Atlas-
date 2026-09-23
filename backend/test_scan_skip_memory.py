"""Isolated SQLite tests: no live database writes or paid API calls."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import ExcludedCategory, ScanSkipMemory
from app.services import product_repository as repo
from app.services.brand_scan_service import SKIP_MEMORY_DAYS
from app.services.product_repository import ProductRepository


class ScanSkipMemoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patch = patch.object(repo, 'SessionLocal', self.sessions)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.engine.dispose()

    def test_remembered_asin_is_skipped_and_others_are_not(self):
        ProductRepository.remember_scan_skips([('B0AAA', 'no_eu_source', 14)])
        self.assertEqual(ProductRepository.get_scan_skip_asins(['B0AAA', 'B0BBB']), {'B0AAA'})

    def test_expired_memory_is_ignored(self):
        with self.sessions() as db:
            past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            db.add(ScanSkipMemory(asin='B0OLD', reason='dead_listing', revisit_after=past))
            db.commit()
        self.assertEqual(ProductRepository.get_scan_skip_asins(['B0OLD']), set())

    def test_remembering_again_refreshes_the_same_row(self):
        ProductRepository.remember_scan_skips([('B0AAA', 'no_current_price', 7)])
        ProductRepository.remember_scan_skips([('B0AAA', 'no_eu_source', 14)])
        with self.sessions() as db:
            rows = db.query(ScanSkipMemory).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].reason, 'no_eu_source')
            remaining = rows[0].revisit_after - datetime.now(timezone.utc).replace(tzinfo=None)
            self.assertGreater(remaining, timedelta(days=13))

    def test_lookup_handles_more_asins_than_one_query_chunk(self):
        asins = [f'B0{i:05d}' for i in range(1200)]
        ProductRepository.remember_scan_skips([(a, 'no_eu_source', 14) for a in asins[::3]])
        self.assertEqual(ProductRepository.get_scan_skip_asins(asins), set(asins[::3]))

    def test_empty_inputs_are_harmless(self):
        ProductRepository.remember_scan_skips([])
        self.assertEqual(ProductRepository.get_scan_skip_asins([]), set())

    def test_clear_only_removes_the_named_reason(self):
        ProductRepository.remember_scan_skips([('B0AAA', 'excluded_category', 30), ('B0BBB', 'no_eu_source', 14)])
        ProductRepository.clear_scan_skips('excluded_category')
        self.assertEqual(ProductRepository.get_scan_skip_asins(['B0AAA', 'B0BBB']), {'B0BBB'})

    def test_removing_a_category_exclusion_forgets_the_asins_it_dropped(self):
        with self.sessions() as db:
            row = ExcludedCategory(category_id='123', category_name='Toys', reason='test')
            db.add(row)
            db.commit()
            exclusion_id = row.id
        ProductRepository.remember_scan_skips([('B0AAA', 'excluded_category', 30), ('B0BBB', 'dead_listing', 30)])
        ProductRepository.remove_category_exclusion(exclusion_id)
        self.assertEqual(ProductRepository.get_scan_skip_asins(['B0AAA', 'B0BBB']), {'B0BBB'})

    def test_every_recorded_reason_has_a_revisit_window(self):
        self.assertEqual(
            set(SKIP_MEMORY_DAYS),
            {'no_eu_source', 'dead_listing', 'excluded_category', 'unprofitable_ceiling', 'no_current_price'},
        )
        self.assertTrue(all(days > 0 for days in SKIP_MEMORY_DAYS.values()))


if __name__ == '__main__':
    unittest.main()
