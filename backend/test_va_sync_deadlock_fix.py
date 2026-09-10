"""
Regression test for a real self-deadlock in pull_and_ingest_va_leads,
2026-09-08 (Tamara: "The VA queue isn't coming into atlas I have VA
leads added today on the sheet that aren't showing").

Root cause, confirmed live: Pass 1 used to end with a bare db.flush()
(not a commit), leaving that write transaction open for the whole of
Pass 2. Pass 2's apply_lead_decision can trigger ProductRepository.
add_watch for an "oos"/"watch" decision, which opens its OWN separate
SessionLocal() and commits immediately -- a second writer, in the SAME
process, colliding with the still-open first one on the same SQLite
file. This reproduced on EVERY run for over 22 hours straight (any
sheet with at least one pending oos/watch-rated row triggers it every
time) -- not ordinary lock contention that clears on retry, a genuine
self-deadlock, since the outer transaction can never release the lock
while it's itself blocked waiting on the inner commit.

Fixed by committing after Pass 1 instead of just flushing, releasing
the lock before Pass 2 (and anything it calls) can ever need to write
again.

Uses a fake in-memory worksheet double (no real Google Sheets API call
-- google_sheets_client.open_sheet is monkeypatched) so this makes NO
live network call and can never touch the real production sheet.

Run with `python test_va_sync_deadlock_fix.py` (plain script, no pytest).
"""
from app.database.database import SessionLocal
from app.database.models import Lead, SheetLeadSyncState, SheetLeadSubmission, WatchedProduct
import app.services.google_sheets_lead_sync as sync_module

TEST_ASIN = "B0VADEADLOCK1"


class FakeWorksheet:
    def __init__(self, header, rows):
        self.header = header
        self.rows = rows  # list of data rows

    def get_all_values(self):
        return [self.header] + self.rows


class FakeSpreadsheet:
    def __init__(self, ws):
        self._ws = ws

    def worksheet(self, name):
        return self._ws


def cleanup():
    db = SessionLocal()
    try:
        db.query(Lead).filter(Lead.asin == TEST_ASIN).delete(synchronize_session=False)
        db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin == TEST_ASIN).delete(synchronize_session=False)
        # Also clear SheetLeadSubmission -- sync_submissions() (added
        # 2026-09-09, va_submission_sync.py) uses this table, not
        # SheetLeadSyncState, as identity's real source of truth. A run
        # that dies before reaching this cleanup (or an older version of
        # this test that predated this table) leaves a row here that
        # makes the NEXT run's "genuinely new ASIN" look like a pending
        # edit to a stale, already-deleted Lead instead.
        db.query(SheetLeadSubmission).filter(SheetLeadSubmission.asin == TEST_ASIN).delete(synchronize_session=False)
        db.query(WatchedProduct).filter(WatchedProduct.asin == TEST_ASIN).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


cleanup()
original_open_sheet = sync_module.open_sheet

try:
    header = ["ASIN", "Client Rating", "Product Name", "VA Notes"]
    # Client Rating = "OOS" -> normalize_client_rating returns "oos" ->
    # apply_lead_decision auto-adds this ASIN to the Watchlist via
    # ProductRepository.add_watch -- the exact trigger for the deadlock.
    rows = [[TEST_ASIN, "OOS", "Deadlock Test Product", "test note"]]
    ws = FakeWorksheet(header, rows)
    sync_module.open_sheet = lambda url: FakeSpreadsheet(ws)

    # No baseline pass needed here: sync_submissions() (va_submission_
    # sync.py, added 2026-09-09) keys "seen before" off SheetLeadSubmission
    # identity (ASIN+Date), not a bare per-content-hash table, and the
    # real production submission table is already initialized (not
    # empty) -- so a genuinely new ASIN's FIRST appearance is ingested
    # and decided immediately, in the same call. That's exactly the
    # scenario that used to self-deadlock (Pass 1 ingest -> commit ->
    # Pass 2's apply_lead_decision -> add_watch's own nested session),
    # so this single call is the real regression check.
    result = sync_module.pull_and_ingest_va_leads()
    assert result["ingested"] >= 1, f"expected the new row to be ingested, got {result}"
    assert result["decisions_applied"] >= 1, f"expected the OOS decision to be applied, got {result}"
    print(f"test 1: sync with a pending OOS decision completes WITHOUT a database-locked deadlock: ok ({result})")

    # Now edit the row (a genuinely new source detail, same ASIN). This
    # sheet has no "Date" column, so the row's identity (ASIN + blank
    # date) is unchanged from above -- match_rows() correctly treats
    # this as an EDIT to the lead just ingested, not a second new lead.
    # This exercises the code path that runs after Pass 1's commit on a
    # second call, confirming it still doesn't deadlock or error.
    ws.rows = [[TEST_ASIN, "OOS", "Deadlock Test Product", "test note -- updated"]]

    result = sync_module.pull_and_ingest_va_leads()
    assert result["updated"] >= 1, f"expected the edited row to be matched as an update, got {result}"
    print(f"test 2: re-sync after an edit to the same lead completes correctly: ok ({result})")

    # The lead really was decided (not left dangling from a half-failed
    # transaction), and the OOS auto-watch really did persist.
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.asin == TEST_ASIN).order_by(Lead.id.desc()).first()
        assert lead is not None
        assert lead.decision == "oos", f"expected decision='oos', got {lead.decision!r}"
        watched = db.get(WatchedProduct, TEST_ASIN)
        assert watched is not None, "add_watch's own write must have actually persisted, not been rolled back"
    finally:
        db.close()
    print("test 3: the decision AND the nested add_watch write both persisted correctly: ok")

finally:
    sync_module.open_sheet = original_open_sheet
    cleanup()
    db = SessionLocal()
    try:
        remaining = (
            db.query(Lead).filter(Lead.asin == TEST_ASIN).count()
            + db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin == TEST_ASIN).count()
            + db.query(WatchedProduct).filter(WatchedProduct.asin == TEST_ASIN).count()
        )
    finally:
        db.close()
    assert remaining == 0, "cleanup failed -- fake rows still present"
    print("cleanup verified: 0 fake rows remain.")

print("\nALL TESTS PASSED.")
