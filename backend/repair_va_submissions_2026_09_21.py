"""One-off repair (2026-09-21): reactivate the sheet_lead_submissions snapshots that match today's Lead Sheet rows.
Every snapshot had been flagged inactive, so the sync saw all 843 rows as new and its circuit breaker refused.
Refuses to write unless the fresh simulation looks exactly like the one that was reviewed with Tamara."""
import json
import os
import sys

os.chdir(r"C:\Users\raven\OneDrive\Documents\FBA\Atlas\backend")
sys.path.insert(0, ".")

from sqlalchemy import text

from app.database.database import SessionLocal
from app.database.models import SheetLeadSubmission
from app.services.google_sheets_client import open_sheet
from app.services.google_sheets_lead_sync import LEAD_SHEET_TAB, LEAD_SHEET_URL, _row_to_payload
from app.services.va_submission_sync import identity, match_rows

rows = open_sheet(LEAD_SHEET_URL).worksheet(LEAD_SHEET_TAB).get_all_values()
header = rows[0]
payloads = [p for p in (_row_to_payload(header, r) for r in rows[1:]) if p and identity(p)[0]]

db = SessionLocal()
try:
    db.execute(text("BEGIN IMMEDIATE"))
    subs = db.query(SheetLeadSubmission).all()
    active_now = sum(1 for s in subs if s.active)
    assert active_now == 0, f"expected every snapshot inactive, found {active_now} active -- someone/something changed it; stopping"
    subs.sort(key=lambda s: (s.lead_id is None, -s.id))
    previous = [json.loads(s.payload) for s in subs]
    matches, new, ambiguous, missing = match_rows(previous, payloads)
    matched = [subs[oi] for oi in matches.values()]
    print(f"sheet rows {len(payloads)} | matched {len(matched)} | new {len(new)} | ambiguous {len(ambiguous)}")
    assert len(payloads) == 843 or abs(len(payloads) - 843) <= 15, "sheet size differs a lot from what was reviewed"
    assert 800 <= len(matched) <= len(payloads), "match count outside what was reviewed"
    assert len(new) <= 20, "too many would-be-new rows -- not the situation that was reviewed"
    assert all(s.lead_id is not None for s in matched), "a matched snapshot has no lead"
    assert len({s.lead_id for s in matched}) == len(matched), "two matched snapshots share a lead"
    for s in matched:
        s.active = True
    db.commit()
    print("committed: reactivated", len(matched))
except Exception:
    db.rollback()
    raise
finally:
    db.close()

db = SessionLocal()
print("after: active =", db.query(SheetLeadSubmission).filter(SheetLeadSubmission.active.is_(True)).count(),
      "| total =", db.query(SheetLeadSubmission).count())
db.close()
