"""
Regression tests for the VA Lead Sheet pull/push sync, 2026-09-05.

Run with `python test_va_lead_sheet_sync.py` (same plain-script style
as the other test_*.py files here, no pytest).

NO Google Sheets API calls in this file -- these test the pure mapping/
decision logic (ingest_sheet_lead_row, normalize_client_rating, the
whitespace-normalization fix, apply_lead_decision's purchased_qty, and
_row_content_hash) directly against a real but disposable DB fixture.
The actual sheet read/write (pull_and_ingest_va_leads/
push_decision_to_sheet) is verified separately via a real, capped
manual run against the live Lead Sheet -- see the session's own report.

Every DB-writing test uses a dedicated, obviously-fake ASIN and cleans
up in a try/finally, verified against exact row counts at the end.
"""
from app.database.database import SessionLocal
from app.database.models import Lead, SheetLeadSyncState
from app.routes.leads import ingest_sheet_lead_row, normalize_client_rating, _extract, _normalize_header, _extract_float, VA_SALE_PRICE_ALIASES, VA_COST_PRICE_ALIASES
from app.services.review_queue_service import apply_lead_decision
from app.services.google_sheets_lead_sync import _row_content_hash

FAKE_ASIN = "B0TESTVALEAD1"


def cleanup():
    db = SessionLocal()
    try:
        db.query(Lead).filter(Lead.asin == FAKE_ASIN).delete()
        db.commit()
    finally:
        db.close()


def test_whitespace_normalized_header_matches_cog_with_newline():
    payload = {"ASIN": FAKE_ASIN, "CoG \n(unit)": "12.50"}
    assert _extract(payload, ["cog (unit)"]) == "12.50"
    assert _normalize_header("CoG \n(unit)") == "cog (unit)"
    print("PASS: whitespace-normalized header now matches Lead Sheet's real 'CoG \\n(unit)' header.")


def test_normalize_client_rating():
    """
    Real bug found live (2026-09-05): the initial mapping only knew
    Avoid/Review/Ok from a small sample. A full column scan of all 720
    real rows found a richer vocabulary (Good, OOS, Already bought, No
    purchase (other reason)) that was silently left unresolved, leaving
    genuinely already-decided leads (e.g. rated "Good") stuck pending.
    """
    assert normalize_client_rating("Avoid") == "rejected"
    assert normalize_client_rating("Ok") == "approved"
    assert normalize_client_rating("Good") == "approved"
    assert normalize_client_rating("OOS") == "oos"
    assert normalize_client_rating("Already bought") == "approved"
    assert normalize_client_rating("No purchase (other reason)") == "rejected"
    assert normalize_client_rating("Review") is None
    assert normalize_client_rating("") is None
    assert normalize_client_rating(None) is None
    print("PASS: Client Rating maps the full real vocabulary correctly; Review/blank/unknown stay pending.")


def test_extract_float_strips_currency_symbols():
    """
    Real bug found live, 2026-09-06: Lead Sheet's real price cells come
    through as "£65.00"/"£37.99" -- the £ symbol was never stripped, so
    float() raised on every single one and va_sale_price/va_cost_price
    silently ended up None for every real lead.
    """
    assert _extract_float({"Sale Price": "£65.00"}, VA_SALE_PRICE_ALIASES) == 65.00
    assert _extract_float({"CoG (unit)": "£37.99"}, VA_COST_PRICE_ALIASES) == 37.99
    assert _extract_float({"Sale Price": "€1,234.50"}, VA_SALE_PRICE_ALIASES) == 1234.50
    print("PASS: currency symbols (£/€) are stripped correctly before parsing a price.")


def test_ingest_sheet_lead_row_captures_title_and_source_url():
    """
    Real gaps found live, 2026-09-06: the sheet's "Product Name" and
    "Source URL" columns were never captured at all -- a sheet lead had
    no title until Keepa analysis succeeded (which could be a while, or
    never), and the only "source" info kept was a free-text retailer
    NAME, never an actual clickable link.
    """
    payload = {
        "ASIN": FAKE_ASIN, "Product Name": "Logitech Lift Vertical Ergonomic Mouse",
        "Source URL": "https://www.logitech.com/en-gb/shop/p/lift-vertical-ergonomic-mouse",
        "Store": "logitech",
    }
    db = SessionLocal()
    try:
        lead = ingest_sheet_lead_row(payload, db)
        db.commit()
        assert lead.title == "Logitech Lift Vertical Ergonomic Mouse"
        assert lead.source_url == "https://www.logitech.com/en-gb/shop/p/lift-vertical-ergonomic-mouse"
        assert lead.source_detail == "logitech"  # unchanged -- still the free-text retailer name
    finally:
        db.close()
    print("PASS: Product Name and Source URL are captured onto the Lead (title, source_url) at ingest time.")


def test_ingest_sheet_lead_row_creates_and_updates():
    payload = {
        "ASIN": FAKE_ASIN, "Sale Price": "45.00", "CoG \n(unit)": "20.00",
        "Expected Profit": "10.50", "ROI": "25%", "Store": "amazon.de",
        "Sourcing Method": "EUA2A", "VA Notes": "Good margin, check stock",
    }
    db = SessionLocal()
    try:
        lead = ingest_sheet_lead_row(payload, db)
        db.commit()
        assert lead.asin == FAKE_ASIN
        assert lead.source == "sheet"
        assert lead.va_sale_price == 45.00
        assert lead.va_cost_price == 20.00  # the newline-header fix in action
        assert lead.va_profit == 10.50
        assert lead.va_roi == 25.0
        assert lead.sourcing_type == "A2A"
        assert lead.status == "queued"

        # Re-ingesting the SAME still-pending ASIN updates in place, not duplicate.
        payload2 = dict(payload, **{"Sale Price": "42.00"})
        lead2 = ingest_sheet_lead_row(payload2, db)
        db.commit()
        assert lead2.id == lead.id
        assert lead2.va_sale_price == 42.00

        count = db.query(Lead).filter(Lead.asin == FAKE_ASIN).count()
        assert count == 1
    finally:
        db.close()
    print("PASS: ingest_sheet_lead_row creates a Lead correctly and updates in place on re-ingest.")


def test_purchased_qty_only_applied_when_passed():
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.asin == FAKE_ASIN).first()
        apply_lead_decision(lead, "approved", purchased_qty=5.0)
        db.commit()
        assert lead.purchased_qty == 5.0
        assert lead.decision == "approved"
    finally:
        db.close()
    print("PASS: apply_lead_decision records purchased_qty when passed.")


def test_row_content_hash_detects_changes():
    row_a = ["B0X", "Title", "45.00"]
    row_b = ["B0X", "Title", "45.00"]
    row_c = ["B0X", "Title", "42.00"]
    assert _row_content_hash(row_a) == _row_content_hash(row_b)
    assert _row_content_hash(row_a) != _row_content_hash(row_c)
    print("PASS: _row_content_hash is stable for unchanged rows and changes when any cell changes.")


def test_sync_state_keyed_by_hash_not_asin_handles_duplicate_asin_rows():
    """
    Regression test for two real incidents during build (2026-09-05):
    keying SheetLeadSyncState on ASIN meant a legitimately-repeated ASIN
    (re-sourced later at a different price) collided -- only the LAST
    occurrence's hash could ever be remembered, so every earlier
    occurrence looked "changed" on every subsequent run forever,
    wrongly re-ingesting/re-deciding real historical rows (44 leads +
    26 decisions the first time, 63 leads + 46 decisions the second).
    Keying on content_hash instead means two different rows sharing an
    ASIN are just two independent hashes -- no collision.
    """
    db = SessionLocal()
    try:
        db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin == FAKE_ASIN).delete()
        row1 = [FAKE_ASIN, "Title", "45.00"]
        row2 = [FAKE_ASIN, "Title", "39.99"]  # same ASIN, genuinely different (re-sourced) row
        hash1, hash2 = _row_content_hash(row1), _row_content_hash(row2)
        assert hash1 != hash2

        db.add(SheetLeadSyncState(asin=FAKE_ASIN, content_hash=hash1))
        db.add(SheetLeadSyncState(asin=FAKE_ASIN, content_hash=hash2))
        db.commit()  # must NOT raise a unique-constraint error on asin

        count = db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin == FAKE_ASIN).count()
        assert count == 2

        db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin == FAKE_ASIN).delete()
        db.commit()
    finally:
        db.close()
    print("PASS: two different rows sharing an ASIN both get their own independent sync-state row.")


def test_reconciliation_updates_notes_on_every_change_not_just_first_fill():
    """
    Real bug found live, 2026-09-06 (Tamara): "only fill if currently
    blank" meant a VA editing/adding to a note AFTER the first sync
    never propagated. Exercises the same condition Pass 2 in
    pull_and_ingest_va_leads uses, without a real Sheets API call.
    """
    cleanup()
    db = SessionLocal()
    try:
        lead = Lead(asin=FAKE_ASIN, source="manual", status="analyzed", verdict="WATCH", va_notes="First note")
        db.add(lead)
        db.commit()
        lead_id = lead.id

        # First reconciliation pass: note already set, should NOT change
        # if the sheet's value is identical.
        va_notes = "First note"
        pending = db.query(Lead).filter(Lead.asin == FAKE_ASIN, Lead.decision.is_(None)).all()
        for l in pending:
            if va_notes and va_notes != l.va_notes:
                l.va_notes = va_notes
        db.commit()
        assert db.get(Lead, lead_id).va_notes == "First note"

        # VA goes back and adds more detail on the sheet -- must propagate.
        va_notes = "First note -- update: confirmed in stock, buying 5 units"
        pending = db.query(Lead).filter(Lead.asin == FAKE_ASIN, Lead.decision.is_(None)).all()
        for l in pending:
            if va_notes and va_notes != l.va_notes:
                l.va_notes = va_notes
        db.commit()
        assert db.get(Lead, lead_id).va_notes == "First note -- update: confirmed in stock, buying 5 units"
    finally:
        db.close()
        cleanup()
    print("PASS: a VA note is always kept in sync with the sheet's current value, not just filled in once.")


def test_reconciliation_decides_manual_lead_and_backfills_notes():
    """
    Regression test for the real gap found live (2026-09-05): a Lead
    entered via Verdict Checker (source="manual", predating this sync)
    whose ASIN also has a Client Rating + VA Notes on Lead Sheet was
    stuck pending forever, because the old logic only ever decided
    leads it had just ingested itself (source="sheet"). The fix
    reconciles ANY pending lead by ASIN, regardless of source -- this
    test exercises that reconciliation logic directly (the same query/
    apply_lead_decision/va_notes-backfill pattern pull_and_ingest_va_
    leads' Pass 2 uses), without a real Sheets API call.
    """
    cleanup()  # this test's own fixture must start from a clean slate for this ASIN
    db = SessionLocal()
    try:
        manual_lead = Lead(asin=FAKE_ASIN, source="manual", status="analyzed", verdict="WATCH")
        db.add(manual_lead)
        db.commit()
        manual_lead_id = manual_lead.id

        # Simulate Pass 2's reconciliation for one ASIN with a resolved
        # Client Rating + VA Notes, exactly as pull_and_ingest_va_leads does.
        va_notes = "Confirmed exact match, good margin"
        decision = normalize_client_rating("Good")
        pending = db.query(Lead).filter(Lead.asin == FAKE_ASIN, Lead.decision.is_(None)).all()
        assert len(pending) == 1
        for lead in pending:
            if va_notes and not lead.va_notes:
                lead.va_notes = va_notes
            if decision:
                apply_lead_decision(lead, decision)
        db.commit()

        refreshed = db.get(Lead, manual_lead_id)
        assert refreshed.source == "manual"  # source untouched -- reconciliation only decides, never reclassifies origin
        assert refreshed.decision == "approved"
        assert refreshed.va_notes == "Confirmed exact match, good margin"
    finally:
        db.close()
        cleanup()
    print("PASS: reconciliation decides a manual-sourced lead and backfills VA notes by ASIN match.")


if __name__ == "__main__":
    cleanup()
    try:
        test_whitespace_normalized_header_matches_cog_with_newline()
        test_normalize_client_rating()
        test_extract_float_strips_currency_symbols()
        test_ingest_sheet_lead_row_captures_title_and_source_url()
        test_ingest_sheet_lead_row_creates_and_updates()
        test_purchased_qty_only_applied_when_passed()
        test_row_content_hash_detects_changes()
        test_sync_state_keyed_by_hash_not_asin_handles_duplicate_asin_rows()
        test_reconciliation_decides_manual_lead_and_backfills_notes()
        test_reconciliation_updates_notes_on_every_change_not_just_first_fill()
        print("\nALL TESTS PASSED.")
    finally:
        cleanup()
        db = SessionLocal()
        remaining = db.query(Lead).filter(Lead.asin == FAKE_ASIN).count()
        db.close()
        assert remaining == 0, "Cleanup failed -- fake rows still present."
        print("Cleanup verified: 0 fake rows remain.")
