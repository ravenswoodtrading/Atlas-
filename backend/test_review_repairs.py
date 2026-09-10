"""Regression cases from CODEX_REVIEW_NOTES; no production writes or API calls."""
import json
import unittest
from unittest.mock import patch, Mock
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database.models import Lead, SheetLeadSubmission, ScanQueueRun, ScanCampaignProgress, ScanQueuePage
from app.routes.leads import ingest_sheet_lead_row
from app.services.va_submission_sync import sync_submissions
from app.services.lead_analysis_service import LeadAnalysisService
from app.services.actual_performance_service import _parse_summary
from app.services.brand_scan_service import BrandScanService
from app.services.scan_coordinator import ScanCoordinator
from app.services.scan_queue_service import ScanQueueService
from test_scan_queue_redesign import ScanQueueTests
from test_va_actuals import SUMMARY


class LeadRepairs(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Lead.__table__.create(self.engine)
        SheetLeadSubmission.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.old = {'ASIN': 'B000000001', 'Date': '1 Jul 26', 'Sale Price': '20'}
        sync_submissions(self.db, [self.old]); self.db.commit()
        self.new = dict(self.old, Date='1 Aug 26')

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def test_webhook_then_sync_links_one_lead_and_replay_is_idempotent(self):
        lead = ingest_sheet_lead_row(self.new, self.db); self.db.commit()
        sync_submissions(self.db, [self.old, self.new]); self.db.commit()
        sync_submissions(self.db, [self.old, self.new]); self.db.commit()
        self.assertEqual(self.db.query(Lead).count(), 1)
        self.assertEqual(self.db.query(SheetLeadSubmission).filter_by(lead_id=lead.id).count(), 1)

    def test_sync_then_webhook_and_real_repeat_submission(self):
        sync_submissions(self.db, [self.old, self.new]); self.db.commit()
        ingest_sheet_lead_row(self.new, self.db); self.db.commit()
        self.assertEqual(self.db.query(Lead).count(), 1)
        sync_submissions(self.db, [self.old, self.new, dict(self.new, Date='1 Sep 26')]); self.db.commit()
        self.assertEqual(self.db.query(Lead).count(), 2)

    def test_meaningful_edit_requeues_but_audit_edit_does_not(self):
        sync_submissions(self.db, [self.old, self.new]); self.db.commit()
        lead = self.db.query(Lead).one()
        lead.status='analyzed'; lead.verdict='BUY'; lead.keepa_metrics='{}'; lead.analysis_attempts=3
        self.db.commit()
        ingest_sheet_lead_row(dict(self.new, **{'Price drop (>5%)':'Yes'}), self.db)
        self.assertEqual(lead.status, 'analyzed')
        ingest_sheet_lead_row(dict(self.new, **{'Sale Price':'30'}), self.db)
        self.assertEqual(lead.status, 'queued')
        self.assertIsNone(lead.verdict); self.assertIsNone(lead.keepa_metrics)
        self.assertEqual(lead.analysis_attempts, 0)

    def test_analysis_in_flight_cannot_overwrite_edit(self):
        lead = ingest_sheet_lead_row(self.new, self.db); self.db.commit()
        lead._analysis_source_payload = lead.raw_sheet_data
        ingest_sheet_lead_row(dict(self.new, **{'Sale Price':'30'}), self.db); self.db.commit()
        self.assertFalse(LeadAnalysisService._save_if_current(self.db, lead, {'verdict':'BUY','status':'analyzed'}))
        self.assertEqual(lead.status, 'queued')

    def test_decided_record_preserves_inputs_and_retains_new_version(self):
        lead = ingest_sheet_lead_row(self.new, self.db)
        lead.decision='approved'; lead.status='reviewed'; lead.verdict='BUY'; self.db.commit()
        ingest_sheet_lead_row(dict(self.new, **{'Sale Price':'30'}), self.db)
        self.assertEqual((lead.va_sale_price, lead.verdict, lead.decision), (20,'BUY','approved'))
        self.assertEqual(json.loads(lead.raw_sheet_data)['_atlas_latest_sheet_data']['Sale Price'],'30')


class ImportRepairs(unittest.TestCase):
    def test_percentages_are_parsed_and_bad_values_rejected(self):
        parsed = _parse_summary(SUMMARY.replace('\t30\t20\t', '\t30%\t20%\t'))[0]
        self.assertEqual((parsed.roi_pct,parsed.margin_pct), (30,20))
        for bad in ('N/A', 'NaN', 'inf', '', 'broken'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'RoI.*row 2'):
                _parse_summary(SUMMARY.replace('\t30\t20\t', f'\t{bad}\t20\t'))
        with self.assertRaisesRegex(ValueError, 'Units.*row 2'):
            _parse_summary(SUMMARY.replace('\t12\t', '\t1.5\t'))


class ScanRepairs(ScanQueueTests):
    def test_failed_call_is_recorded_and_lock_released(self):
        with patch('app.services.scan_queue_service.BrandScanService', side_effect=RuntimeError('failure')):
            result=ScanQueueService.run_next_tick()
        self.assertIn('error',result)
        self.assertFalse(ScanCoordinator.is_busy())
        self.assertIsNone(ScanCoordinator.status()['owner'])
        with self.sessions() as db:
            self.assertEqual(db.query(ScanQueueRun).one().outcome,'Failed: RuntimeError')

    def test_checkpoint_survives_partial_page_and_clears_on_completion(self):
        with patch('app.services.scan_queue_service.BrandScanService') as scanner, patch('app.services.scan_queue_service.ActivityLog.record'):
            scanner.return_value.scan.return_value=dict(asins_scanned=10,count=0,raw_page_count=15,uk_ran_out=True,completed_asins=['A','B'])
            ScanQueueService.run_next_tick()
            with self.sessions() as db:
                self.assertEqual(json.loads(db.get(ScanQueuePage,1).completed_asins),['A','B'])
            # Use the same campaign directly, with a fresh database session.
            from app.database.models import ScanQueueItem
            scanner.return_value.scan.return_value=dict(asins_scanned=5,count=0,raw_page_count=15,completed_asins=['C'])
            with self.sessions() as db:
                ScanQueueService._execute_scan_for_item(db,db.get(ScanQueueItem,1))
                self.assertIsNone(db.get(ScanQueuePage,1))
            self.assertEqual(scanner.return_value.scan.call_args.kwargs['already_checked'],{'A','B','C'})

    def test_sp_api_deadline_and_priority_stop_optional_calls(self):
        client=Mock()
        with patch('app.services.brand_scan_service.time.monotonic',return_value=100):
            self.assertEqual(BrandScanService._sp_api_find_source_market(client,'A',Mock(),'category',deadline=99),(None,None))
        client.get_item_offers.assert_not_called()
        with patch('app.services.brand_scan_service.KeepaPriority.has_pending',return_value=True):
            BrandScanService._sp_api_find_source_market(client,'A',Mock(),'category',deadline=float('inf'))
        client.get_item_offers.assert_not_called()

    def test_lock_holder_is_visible_and_cleared(self):
        self.assertTrue(ScanCoordinator.try_acquire_for_automated_tick())
        try:
            ScanCoordinator.progress('EU pricing')
            self.assertEqual(ScanCoordinator.status()['stage'],'EU pricing')
            self.assertIn('EU pricing',ScanCoordinator.busy_reason())
        finally:
            ScanCoordinator.release_after_automated_tick()


if __name__ == '__main__':
    unittest.main()
