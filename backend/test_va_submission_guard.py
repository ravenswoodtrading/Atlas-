"""VA submission sync guard (2026-09-21): a bad/partial read of the Lead Sheet must not deactivate the known
submissions (that left every sheet row looking new and stopped VA leads syncing). In-memory SQLite only."""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database.models import Lead, SheetLeadSubmission
from app.services import va_submission_sync as vs
from app.services.va_submission_sync import sync_submissions


def rows(n, prefix="B0GUARD"):
    return [{'ASIN': f'{prefix}{i:04d}', 'Date': '1 Jul 26', 'Sale Price': str(10 + i)} for i in range(n)]


class GuardTests(unittest.TestCase):
    def setUp(self):
        engine = self.engine = create_engine('sqlite:///:memory:')
        Lead.__table__.create(engine)
        SheetLeadSubmission.__table__.create(engine)
        self.db = Session(engine)
        self.sheet = rows(200)
        sync_submissions(self.db, self.sheet)          # first run: baselines all 200
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def active(self):
        return self.db.query(SheetLeadSubmission).filter(SheetLeadSubmission.active.is_(True)).count()

    def test_an_empty_read_is_refused_and_nothing_is_deactivated(self):
        with self.assertRaises(RuntimeError) as ctx:
            sync_submissions(self.db, [])
        self.assertIn("Refusing to sync", str(ctx.exception))
        self.db.rollback()
        self.assertEqual(self.active(), 200)

    def test_a_partial_read_is_refused_too(self):
        with self.assertRaises(RuntimeError):
            sync_submissions(self.db, self.sheet[:20])   # 180 of 200 "missing"
        self.db.rollback()
        self.assertEqual(self.active(), 200)

    def test_a_completely_different_read_is_refused(self):
        with self.assertRaises(RuntimeError):
            sync_submissions(self.db, rows(200, prefix="B0OTHERSHT"))
        self.db.rollback()
        self.assertEqual(self.active(), 200)

    def test_normal_churn_still_works(self):
        """The VA deletes a few rows and adds a few: still handled exactly as before."""
        current = self.sheet[5:] + rows(3, prefix="B0NEWROWS")            # 5 removed, 3 new
        result = sync_submissions(self.db, current)
        self.db.commit()
        self.assertEqual(result['ingested'], 3)
        self.assertEqual(self.active(), 195 + 3)                          # the 5 removed are deactivated

    def test_a_small_sheet_is_not_blocked_by_the_absolute_bar(self):
        """Few submissions: losing them all is under the 50-row bar, so the guard leaves small sheets alone."""
        db = Session(create_engine('sqlite:///:memory:'))
        Lead.__table__.create(db.get_bind())
        SheetLeadSubmission.__table__.create(db.get_bind())
        sync_submissions(db, rows(10))
        db.commit()
        result = sync_submissions(db, rows(10, prefix="B0SMALLNEW"))
        self.assertEqual(result['ingested'], 10)
        db.close()

    def test_the_bars_are_the_documented_ones(self):
        self.assertEqual((vs.MAX_MISSING_ROWS, vs.MAX_MISSING_FRACTION), (50, 0.25))


if __name__ == "__main__":
    unittest.main()
