"""Review UI regressions: no production database or external API calls."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import review_queue, competitors, leads, watchlist


class ReviewReasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        for module in (review_queue, competitors, leads, watchlist):
            app.include_router(module.router)
        cls.client = TestClient(app)

    def test_invalid_rejections_never_reach_writes(self):
        cases = [
            ('/review-queue/resolve', {'asin': 'B000000001', 'decision': 'rejected'}),
            ('/review/set', {'asin': 'B000000001', 'verdict': 'down'}),
            ('/review/decide', {'lead_id': '1', 'decision': 'rejected'}),
        ]
        with patch.object(review_queue, 'SessionLocal') as queue_db, \
             patch.object(leads, 'SessionLocal') as lead_db, \
             patch.object(watchlist.ProductRepository, 'set_review') as set_review, \
             patch.object(review_queue.ReviewQueueService, 'resolve_item') as resolve:
            for path, payload in cases:
                for extra in ({}, {'reason': '   '}, {'reason_category': 'OTHER', 'reason': '  '}, {'reason_category': 'invalid'}):
                    with self.subTest(path=path, extra=extra):
                        response = self.client.post(path, data={**payload, **extra, 'atlas_notes': 'must not save'})
                        self.assertEqual(response.status_code, 422)
            for mocked in (queue_db, lead_db, set_review, resolve):
                mocked.assert_not_called()

    def test_valid_category_and_other_reach_resolver(self):
        with patch.object(review_queue.ReviewQueueService, 'resolve_item', return_value={'scan': True, 'competitor': [], 'lead': []}) as resolve:
            for reason, category in [('', 'GATED'), ('  wrong size  ', 'OTHER')]:
                response = self.client.post('/review-queue/resolve', data={'asin': 'B000000001', 'decision': 'rejected', 'reason': reason, 'reason_category': category})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(resolve.call_args.kwargs['reason_category'], category)
                self.assertEqual(resolve.call_args.kwargs['reason'], reason.strip() or None)

    def test_buy_and_watch_still_allow_no_reason(self):
        with patch.object(review_queue.ReviewQueueService, 'resolve_item', return_value={'scan': True, 'competitor': [], 'lead': []}):
            for decision in ('approved', 'watch', 'unsure'):
                self.assertEqual(self.client.post('/review-queue/resolve', data={'asin': 'B000000001', 'decision': decision}).status_code, 200)

    def test_competitor_uses_canonical_panel(self):
        with patch.object(review_queue.ReviewQueueService, 'get_queue_item', return_value=None):
            shared = self.client.get('/review-queue/item/B000000001')
            competitor = self.client.get('/competitors/opportunity/B000000001')
            self.assertEqual(shared.status_code, 200)
            self.assertEqual(competitor.text, shared.text)

    def test_confirmed_source_is_distinct_from_likely_source(self):
        template = review_queue.templates.env.get_template('_review_known_source.html')
        candidate = dict(retailer_price_gbp=10, delivery_gbp=0, retailer_domain='retailer.test', retailer_url='https://retailer.test/product', price_source='manual', source_confidence='High', notes='Order evidence')
        html = template.render(item={'asin': 'B000000001'}, manual_candidate=candidate)
        self.assertIn('Confirmed source', html)
        self.assertIn('Order evidence', html)
        candidate['source_confidence'] = 'Medium'
        html = template.render(item={'asin': 'B000000001'}, manual_candidate=candidate)
        self.assertNotIn('<strong>Confirmed source', html)
        self.assertIn('Saved source', html)


if __name__ == '__main__':
    unittest.main()
