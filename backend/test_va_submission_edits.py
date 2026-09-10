import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database.models import Lead, SheetLeadSubmission
from app.services.va_submission_sync import sync_submissions, match_rows
from app.routes.leads import ingest_sheet_lead_row


class SubmissionEdits(unittest.TestCase):
    def setUp(self):
        engine = self.engine = create_engine('sqlite:///:memory:')
        Lead.__table__.create(engine)
        SheetLeadSubmission.__table__.create(engine)
        self.db = Session(engine)
        self.old = {'ASIN': 'B000000001', 'Date': '1 Jul 26', 'Sale Price': '20'}
        sync_submissions(self.db, [self.old])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_webhook_edit_preserves_reviewed_lead(self):
        lead = ingest_sheet_lead_row(self.old, self.db)
        lead.status, lead.decision = 'reviewed', 'rejected'
        self.db.commit()
        changed = ingest_sheet_lead_row(dict(self.old, **{'Sale Price': '30'}), self.db)
        self.assertEqual(changed.id, lead.id)
        self.assertEqual((changed.status, changed.decision), ('reviewed', 'rejected'))

    def test_historical_edit_does_not_create_lead(self):
        result = sync_submissions(self.db, [dict(self.old, **{'Sale Price': '22'})])
        self.assertEqual(result['ingested'], 0)
        self.assertEqual(self.db.query(Lead).count(), 0)

    def test_reviewed_edit_preserves_decision_analysis_and_identity(self):
        new = dict(self.old, Date='1 Aug 26')
        sync_submissions(self.db, [self.old, new])
        self.db.commit()
        lead = self.db.query(Lead).one()
        lead.decision, lead.status, lead.verdict = 'approved', 'reviewed', 'BUY'
        lead.keepa_metrics = '{"saved": true}'
        self.db.commit()
        sync_submissions(self.db, [dict(new, **{'Sale Price': '25'}), self.old])
        self.db.commit()
        self.assertEqual(self.db.query(Lead).count(), 1)
        self.assertEqual((lead.decision, lead.status, lead.verdict), ('approved', 'reviewed', 'BUY'))
        self.assertEqual(lead.va_sale_price, 25)
        self.assertEqual(lead.keepa_metrics, '{"saved": true}')

    def test_audit_columns_do_not_update_or_queue(self):
        result = sync_submissions(self.db, [dict(self.old, **{'Buy Box on lead date': '15', 'Price drop (>5%)': 'Yes'})])
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['ingested'], 0)

    def test_corrected_date_is_edit_and_repeat_row_is_new(self):
        revised = dict(self.old, Date='2 Jul 26')
        result = sync_submissions(self.db, [revised])
        self.db.commit()
        self.assertEqual(result['ingested'], 0)
        result = sync_submissions(self.db, [revised, dict(revised, Date='1 Aug 26')])
        self.assertEqual(result['ingested'], 1)

    def test_ambiguous_changes_are_not_new_leads(self):
        previous = [self.old, dict(self.old, **{'Sale Price': '21'})]
        current = [dict(self.old, **{'Sale Price': '22'}), dict(self.old, **{'Sale Price': '23'})]
        matches, new, ambiguous, missing = match_rows(previous, current)
        self.assertEqual(new, set())
        self.assertEqual(len(ambiguous), 2)


if __name__ == '__main__':
    unittest.main()
