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
from app.database.models import Lead, SheetLeadSyncState, WatchedProduct
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

    # Baseline pass first (matches pull_and_ingest_va_leads' own "first
    # sync never treats existing rows as new" safety rule) so the
    # SECOND call below is the one that actually ingests + decides --
    # otherwise this row would just get baselined, never reaching Pass 2.
    baseline_result = sync_module.pull_and_ingest_va_leads()
    print(f"test 1: baseline pass completes without error: ok ({baseline_result})")

    # Change the row's content (a genuinely new source detail) so Pass 1
    # sees a new content hash and actually ingests + Pass 2 actually
    # runs apply_lead_decision for this ASIN this time.
    ws.rows = [[TEST_ASIN, "OOS", "Deadlock Test Product", "test note -- updated"]]

    result = sync_module.pull_and_ingest_va_leads()
    assert result["ingested"] >= 1, f"expected the changed row to be ingested, got {result}"
    assert result["decisions_applied"] >= 1, f"expected the OOS decision to be applied, got {result}"
    print(f"test 2: sync with a pending OOS decision completes WITHOUT a database-locked deadlock: ok ({result})")

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
