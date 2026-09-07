"""
Regression test: VA-sheet leads must stay visible in the queue even
when Atlas can't (or hasn't yet) rated them, 2026-09-06.

Per Tamara's explicit instruction: "the VA queue is all leads
regardless of whether they are good or not -- if we can't rate them we
should just try [to show them]." Before this fix, a sheet lead with
verdict=None (still queued, or LeadAnalysisService gave up after
MAX_ANALYSIS_ATTEMPTS -- e.g. Keepa had no data, or every Claude call
failed because the account ran out of credits) or verdict="AVOID" was
invisible everywhere in Review Queue/Command Centre, since _pending_
leads() only ever queried BUY/WATCH verdicts.

Run with `python test_va_queue_unrated_visibility.py` (same plain-
script convention as the other test_*.py files, no pytest). Uses
dedicated fake ASINs, cleans up in a try/finally, verified against
exact row counts at the end.
"""
from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.review_queue_service import ReviewQueueService

FAKE_ASINS = ["B0TESTVAQ001", "B0TESTVAQ002", "B0TESTVAQ003", "B0TESTVAQ004"]


def cleanup():
    db = SessionLocal()
    try:
        db.query(Lead).filter(Lead.asin.in_(FAKE_ASINS)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_unrated_and_avoid_sheet_leads_are_visible():
    db = SessionLocal()
    try:
        # Still queued -- analysis never ran (or hasn't reached it yet).
        db.add(Lead(asin=FAKE_ASINS[0], source="sheet", status="queued"))
        # Analyzed, but Atlas gave up -- verdict None (e.g. Keepa had no
        # data, or every Claude call failed -- out of credits).
        db.add(Lead(asin=FAKE_ASINS[1], source="sheet", status="analyzed", verdict=None,
                     rationale="Could not analyze after 3 attempts -- Analysis error: insufficient credits"))
        # Analyzed, Atlas's own opinion is AVOID.
        db.add(Lead(asin=FAKE_ASINS[2], source="sheet", status="analyzed", verdict="AVOID"))
        # Control: a manual (non-sheet) lead with the SAME AVOID verdict
        # must stay hidden -- this fix is scoped to source="sheet" only.
        db.add(Lead(asin=FAKE_ASINS[3], source="manual", status="analyzed", verdict="AVOID"))
        db.commit()
    finally:
        db.close()

    unrated = ReviewQueueService._pending_sheet_leads_unrated()
    unrated_asins = {lead["asin"] for lead in unrated}
    assert FAKE_ASINS[0] in unrated_asins, "queued (never-analyzed) sheet lead must be visible"
    assert FAKE_ASINS[1] in unrated_asins, "analyzed-but-gave-up (verdict None) sheet lead must be visible"
    assert FAKE_ASINS[2] in unrated_asins, "AVOID-verdict sheet lead must be visible"
    assert FAKE_ASINS[3] not in unrated_asins, "a manual (non-sheet) lead must NOT be pulled in by this method"

    # Confirm _item_views puts these in VA_TO_REVIEW without also
    # wrongly flagging them NEEDS_ATTENTION just for lacking a verdict.
    for lead in unrated:
        if lead["asin"] in (FAKE_ASINS[0], FAKE_ASINS[1], FAKE_ASINS[2]):
            views = ReviewQueueService._item_views(lead)
            assert "VA_TO_REVIEW" in views, f"{lead['asin']} should be in VA_TO_REVIEW"
            assert "NEEDS_ATTENTION" not in views, f"{lead['asin']} should NOT be flagged NEEDS_ATTENTION just for having no/AVOID verdict"

    print("PASS: unrated/AVOID sheet leads are visible in VA_TO_REVIEW without a spurious NEEDS_ATTENTION flag; manual leads unaffected.")


def test_unrated_sheet_lead_is_openable_and_actionable():
    """
    Real bugs found live, 2026-09-06: get_queue_item (the single-ASIN
    detail-panel lookup) and resolve_item (the actual Buy/Reject action)
    both had their OWN separate `Lead.status == "analyzed"` queries,
    never updated when _pending_sheet_leads_unrated() fixed the same gap
    for the full list view. Without this fix, an unrated sheet lead was
    VISIBLE in the queue list but opening it showed nothing, and
    clicking Buy/Reject silently did nothing (matched zero lead_ids).
    """
    db = SessionLocal()
    try:
        lead = Lead(asin=FAKE_ASINS[0], source="sheet", status="queued")
        db.add(lead)
        db.commit()
        lead_id = lead.id
    finally:
        db.close()

    item = ReviewQueueService.get_queue_item(FAKE_ASINS[0])
    assert item is not None, "an unrated (still-queued) sheet lead must be openable via get_queue_item"
    assert item["va_info"] is not None

    resolved = ReviewQueueService.resolve_item(FAKE_ASINS[0], "rejected", reason="test")
    assert lead_id in resolved["lead"], "resolve_item must be able to act on an unrated (still-queued) sheet lead"

    db = SessionLocal()
    try:
        assert db.get(Lead, lead_id).decision == "rejected"
    finally:
        db.close()
    print("PASS: an unrated sheet lead is both openable (get_queue_item) and actionable (resolve_item), not just visible in the list.")


if __name__ == "__main__":
    cleanup()
    try:
        test_unrated_and_avoid_sheet_leads_are_visible()
        cleanup()
        test_unrated_sheet_lead_is_openable_and_actionable()
        print("\nALL TESTS PASSED.")
    finally:
        cleanup()
        db = SessionLocal()
        remaining = db.query(Lead).filter(Lead.asin.in_(FAKE_ASINS)).count()
        db.close()
        assert remaining == 0, "Cleanup failed -- fake rows still present."
        print("Cleanup verified: 0 fake rows remain.")
