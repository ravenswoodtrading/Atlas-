"""Isolated SQLite tests: no live database writes or paid API calls."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from jinja2 import Environment, FileSystemLoader

from app.database.base import Base
from app.database.models import (ScanQueueItem, AutomationSettings, ScanBrandSchedule,
    ScanCampaignProgress, ScanTierReview, ScanQueueRun)
from app.services import scan_schedule_service as schedule
from app.services import scan_queue_service as queue


class ScanQueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patches = [patch.object(schedule, 'SessionLocal', self.sessions),
                        patch.object(queue, 'SessionLocal', self.sessions)]
        for p in self.patches:
            p.start()
        with self.sessions() as db:
            db.add_all([ScanQueueItem(id=1, brand='alpha', position=0, target_count=999),
                        ScanQueueItem(id=2, brand='beta', position=1, target_count=999),
                        AutomationSettings(id=1, paused=False)])
            db.commit()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.engine.dispose()

    def test_less_regular_waits_then_gets_a_turn(self):
        schedule.set_brand_tier('beta', 'less_regular')
        with self.sessions() as db:
            db.add(ScanCampaignProgress(item_id=2, last_attempt_at=datetime.utcnow()))
            db.commit()
            settings = db.get(AutomationSettings, 1)
            settings.last_scan_queue_item_id = 1
            self.assertEqual(queue.ScanQueueService._next_round_robin_item(db, settings).brand, 'alpha')
            db.get(ScanCampaignProgress, 2).last_attempt_at -= timedelta(minutes=9)
            db.flush()
            self.assertEqual(queue.ScanQueueService._next_round_robin_item(db, settings).brand, 'beta')

    def test_weekly_suggestion_requires_approval_and_is_idempotent(self):
        candidate = dict(brand='alpha', lane='REDUCE_QUIET', reason='No useful results in the evidence window.')
        with patch('app.services.attention_engine_service.get_attention_candidates', return_value=[candidate]) as evidence:
            schedule.refresh_weekly_reviews()
            schedule.refresh_weekly_reviews()
            evidence.assert_called_once()
        review = schedule.pending_reviews()[0]
        with self.sessions() as db:
            self.assertIsNone(db.get(ScanBrandSchedule, 'alpha'))
        schedule.decide_review(review.id, True)
        with self.sessions() as db:
            self.assertEqual(db.get(ScanBrandSchedule, 'alpha').tier, 'less_regular')
        with self.assertRaises(LookupError):
            schedule.decide_review(review.id, True)

    def test_manual_tier_supersedes_pending_suggestion(self):
        with self.sessions() as db:
            db.add(ScanTierReview(brand='alpha', week='2026-W37', current_tier='regular',
                                 proposed_tier='less_regular', reason='Test'))
            db.commit()
        schedule.set_brand_tier('alpha', 'regular')
        self.assertEqual(schedule.pending_reviews(), [])
        with self.assertRaises(ValueError):
            schedule.set_brand_tier('alpha', 'invalid')

    def test_removal_covers_all_brand_campaigns(self):
        queue.ScanQueueService.add_item('alpha', category_ids=['123'])
        queue.ScanQueueService.delete_item(1)
        self.assertEqual([i.brand for i in queue.ScanQueueService.list_items()], ['beta'])

    def test_progress_only_completes_after_whole_page(self):
        with self.sessions() as db, patch.object(queue, 'BrandScanService') as scanner, patch.object(queue.ActivityLog, 'record'):
            item = db.get(ScanQueueItem, 1)
            scanner.return_value.scan.return_value = dict(asins_scanned=2, count=1, raw_page_count=10, uk_ran_out=True)
            queue.ScanQueueService._execute_scan_for_item(db, item)
            self.assertEqual(item.next_page, 0)
            self.assertIsNone(db.get(ScanCampaignProgress, 1).last_full_pass_at)
            scanner.return_value.scan.return_value = dict(asins_scanned=8, count=1, raw_page_count=10)
            queue.ScanQueueService._execute_scan_for_item(db, item)
            progress = db.get(ScanCampaignProgress, 1)
            self.assertEqual(progress.filtered_count, 10)
            self.assertIsNotNone(progress.last_full_pass_at)
            self.assertEqual(db.query(ScanQueueRun).count(), 2)

    def test_error_does_not_fabricate_coverage(self):
        with self.sessions() as db, patch.object(queue, 'BrandScanService') as scanner:
            scanner.return_value.scan.return_value = {'error': 'Not enough tokens'}
            queue.ScanQueueService._execute_scan_for_item(db, db.get(ScanQueueItem, 1))
            self.assertIsNone(db.get(ScanCampaignProgress, 1))
            self.assertEqual(db.query(ScanQueueRun).count(), 0)

    def test_rows_group_brand_without_double_counting_categories(self):
        queue.ScanQueueService.add_item('alpha', category_ids=['123'])
        rows = schedule.queue_rows(queue.ScanQueueService.list_items())
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]['filtered_count'])
        self.assertEqual(len(rows[0]['campaigns']), 2)

    def test_busy_tick_does_not_consume_a_turn(self):
        with patch.object(queue.ScanCoordinator, 'try_acquire_for_automated_tick', return_value=False):
            queue.ScanQueueService.run_next_tick()
        with self.sessions() as db:
            self.assertIsNone(db.get(AutomationSettings, 1).last_scan_queue_item_id)
            self.assertIsNone(db.get(ScanCampaignProgress, 1))

    def test_source_links_and_missing_url(self):
        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=True)
        macro = env.get_template('_source_listing_link.html').module.source_listing_link
        for item, expected in [
            ({'va_info': {'source_url': 'https://shop.example/product'}, 'asin': 'B012345678'}, 'https://shop.example/product'),
            ({'best_source_marketplace': 'DE', 'asin': 'B012345678'}, 'https://www.amazon.de/dp/B012345678'),
        ]:
            self.assertIn(expected, str(macro(item)))
        self.assertIn('not recorded', str(macro({'asin': 'B012345678'})))
        self.assertNotIn('href=', str(macro({'source_url': 'javascript:alert(1)'})))
        self.assertIn('https://retailer.example/p', str(macro({}, {'retailer_url': 'https://retailer.example/p'})))

    def test_pages_and_tier_forms(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import scan_queue as routes
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        with patch.object(routes.ProductRepository, 'get_brand_performance', return_value={'alpha': {'buy': 3, 'consider': 2}}):
            response = client.get('/scan-queue')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Products after filters', response.text)
            detail = client.get('/scan-queue/brand/1')
            self.assertEqual(detail.status_code, 200)
            self.assertIn('BUY leads from scans', detail.text)
            self.assertNotIn('Add brand', detail.text)
            self.assertEqual(client.post('/scan-queue/tier', data={'item_id': 1, 'tier': 'invalid'}).status_code, 422)
            self.assertEqual(client.post('/scan-queue/tier', data={'item_id': 1, 'tier': 'less_regular'}, follow_redirects=False).status_code, 303)
            self.assertEqual(client.get('/scan-queue/brand/999').status_code, 404)
            self.assertEqual(client.post('/scan-queue/add', data={'brand': '  '}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
