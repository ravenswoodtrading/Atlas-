"""
Regression test for push_decision_to_sheet's Atlas Notes fix, 2026-09-07
(Tamara: "the notes I write when commenting on a VA lead are not
feeding back into the sheet ... The rating and number of units
purchased is [feeding back] but notes ... are not").

Real bug found live: Lead Sheet already has an actively-used "Client
Notes" column (675/726 real rows) right next to Client Rating, but the
code was writing to a separate, auto-created "Atlas Notes" column at
the far right of the sheet that nobody was looking at (confirmed live:
only 3 rows had anything in it). Fixed to append into "Client Notes"
instead, never overwriting existing content, with no duplicate lines on
a repeated push of the same unchanged note.

Uses a fake in-memory worksheet double (no real Google Sheets API call
-- google_sheets_client.open_sheet is monkeypatched) so this makes NO
live network call and can never touch the real production sheet.

Run with `python test_push_notes_to_sheet.py` (plain script, no pytest).
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead
import app.services.google_sheets_lead_sync as sync_module

TEST_ASIN = "B0PUSHNOTE1"


class FakeWorksheet:
    def __init__(self, header, row):
        self.header = header
        self.row = row  # single data row, list of cell strings

    def get_all_values(self):
        return [self.header, self.row]

    def update_cell(self, row_num, col_num, value):
        target = self.header if row_num == 1 else self.row
        while len(target) < col_num:
            target.append("")
        target[col_num - 1] = str(value)


class FakeSpreadsheet:
    def __init__(self, ws):
        self._ws = ws

    def worksheet(self, name):
        return self._ws


def cleanup():
    db = SessionLocal()
    try:
        db.query(Lead).filter(Lead.asin == TEST_ASIN).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


cleanup()
original_open_sheet = sync_module.open_sheet

try:
    header = ["ASIN", "Client Rating", "Client Notes", "Purchased Qty", "Atlas Notes"]
    row = [TEST_ASIN, "", "Pre-existing manual note from the team.", "", ""]
    ws = FakeWorksheet(header, row)
    sync_module.open_sheet = lambda url: FakeSpreadsheet(ws)

    db = SessionLocal()
    try:
        lead = Lead(asin=TEST_ASIN, source="sheet", status="analyzed", verdict="WATCH",
                    atlas_notes="This is a real find, worth a second look.")
        db.add(lead)
        db.commit()
        lead_id = lead.id
    finally:
        db.close()

    # 1 -- pushing a note writes into "Client Notes", not "Atlas Notes",
    # and does NOT destroy the pre-existing manual note.
    result = sync_module.push_decision_to_sheet(lead_id)
    assert result is True, "push_decision_to_sheet should report success"
    client_notes_idx = header.index("Client Notes")
    atlas_notes_idx = header.index("Atlas Notes")
    assert "Pre-existing manual note from the team." in ws.row[client_notes_idx], (
        "existing manual note must be preserved, not overwritten"
    )
    assert "This is a real find, worth a second look." in ws.row[client_notes_idx], (
        "Atlas's note must actually appear in Client Notes"
    )
    assert ws.row[atlas_notes_idx] == "", "the old, unused Atlas Notes column must NOT be written to any more"
    print("test 1: Atlas note appended into Client Notes, existing manual note preserved, old Atlas Notes column untouched: ok")

    # 2 -- the appended line is dated and attributed.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"[Atlas {today}]" in ws.row[client_notes_idx]
    print("test 2: appended line is dated and attributed to Atlas: ok")

    # 3 -- pushing the SAME unchanged note again is a no-op (no duplicate line).
    before = ws.row[client_notes_idx]
    sync_module.push_decision_to_sheet(lead_id)
    after = ws.row[client_notes_idx]
    assert before == after, f"re-pushing an unchanged note must not duplicate it -- before={before!r} after={after!r}"
    print("test 3: re-pushing an unchanged note does not duplicate the line: ok")

    # 4 -- a genuinely NEW note appends as a second line, keeping both.
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        lead.atlas_notes = "Second, different comment added later."
        db.commit()
    finally:
        db.close()
    sync_module.push_decision_to_sheet(lead_id)
    assert "This is a real find, worth a second look." in ws.row[client_notes_idx]
    assert "Second, different comment added later." in ws.row[client_notes_idx]
    print("test 4: a genuinely new note appends alongside the earlier ones, nothing lost: ok")

    print("\nALL TESTS PASSED.")
finally:
    sync_module.open_sheet = original_open_sheet
    cleanup()
    db = SessionLocal()
    try:
        remaining = db.query(Lead).filter(Lead.asin == TEST_ASIN).count()
    finally:
        db.close()
    assert remaining == 0, "cleanup failed -- fake lead still present"
    print("cleanup verified: 0 fake rows remain.")
