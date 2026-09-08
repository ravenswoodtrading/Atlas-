"""
VA Lead Sheet <-> Atlas Lead queue sync, 2026-09-05 -- replaces the
webhook design in app/routes/leads.py that got stuck needing an ngrok
tunnel (Apps Script pushing to Atlas requires Atlas to be publicly
reachable). Flips the direction: Atlas already has a working OAuth
connection to Tamara's Google account (google_sheets_client.py, built
for the OA Source Intelligence sheet), so Atlas pulls from Lead Sheet
and pushes decisions back to it directly -- no public exposure, no
tunnel, no Apps Script involvement.

Source: the real "Lead Sheet" tab, same spreadsheet as the OA Source
Intelligence "Atlas OA Leads" tab. Its real columns (ASIN, CoG (unit),
Actual CoG, Sale Price, Expected Profit, ROI, Store, Source URL,
Sourcing Method, VA Notes, Client Rating, Purchased Qty, ...) already
line up with leads.py's alias lists, which were written blind before
this sheet was ever seen.

Reuses, unchanged: leads.py's ingest_sheet_lead_row (the exact same
mapping the webhook already used), review_queue_service.apply_lead_
decision (the one shared decision path every entry point uses).

THIS MODULE'S ONLY ATLAS WRITES are Lead rows (via ingest_sheet_lead_row)
and Lead.decision (via apply_lead_decision) -- both already-existing,
already-reviewed write paths. No new queue/opportunity concept, no
Keepa/SP-API call, no scheduler change beyond the new poll task itself.
"""
import hashlib
import json
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead, SheetLeadSyncState
from app.services.google_sheets_client import open_sheet
from app.services.review_queue_service import apply_lead_decision
from app.routes.leads import (
    ingest_sheet_lead_row, normalize_client_rating, _extract,
    ASIN_ALIASES, CLIENT_RATING_ALIASES, VA_NOTES_ALIASES, DATE_LAST_ADDED_ALIASES,
)

LEAD_SHEET_URL = "https://docs.google.com/spreadsheets/d/1myUd44MfrKDRv6iGgm0KnvS9oZ4jhIEtCy4vTkrFlA4/edit"
LEAD_SHEET_TAB = "Lead Sheet"

# Atlas's decision -> the value written into Lead Sheet's own "Client
# Rating" column. "oos"/"watch"/"need_more_info" all map to "Review"
# (not a final call) rather than inventing new sheet vocabulary.
DECISION_TO_CLIENT_RATING = {
    "approved": "Ok", "rejected": "Avoid",
    "oos": "Review", "watch": "Review", "need_more_info": "Review",
}


def _row_content_hash(row: list) -> str:
    # Hash the WHOLE row, not just the fields we map -- see
    # SheetLeadSyncState's own docstring: any edit to any column is a
    # genuine change worth re-syncing, not just ones this code parses.
    return hashlib.sha256(json.dumps(row, default=str).encode("utf-8")).hexdigest()


def _row_to_payload(header: list, row: list) -> dict:
    return {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}


def pull_and_ingest_va_leads() -> dict:
    """
    THE pull entry point, called by the scheduler (or manually). Two
    independent passes over the same sheet read:

    PASS 1 -- new/changed row ingestion (content-hash-gated, unchanged
    from the original design). For each row with a real ASIN:
      - First time this ASIN has ever been seen at all: baseline it
        (record its content hash) WITHOUT calling ingest_sheet_lead_row.
        This is the hard safety requirement -- Lead Sheet already has
        720 historical rows, most already fully decided by the team's
        own manual process; treating all of them as brand-new Atlas
        leads on the very first sync would flood the Review Queue and
        burn Keepa tokens re-analyzing already-settled decisions.
      - Seen before, content unchanged: skip entirely (no re-analysis,
        no wasted Keepa spend).
      - Seen before, content changed (or genuinely new after the
        baseline): ingest via ingest_sheet_lead_row (creates/updates a
        Lead(source="sheet"), same mapping the old webhook used).

    PASS 2 -- decision + VA-notes reconciliation, added 2026-09-05 after
    a real gap was found live: Tamara found real pending leads in her
    Review Queue whose ASIN ALREADY had a Client Rating set on Lead
    Sheet, stuck with no Atlas decision. Root cause -- those Leads were
    entered via Verdict Checker (source="manual") BEFORE this sync
    existed, so Pass 1's ingestion (which only ever touches
    source="sheet" rows) never looked at them, and even a sheet-sourced
    lead's decision was only ever checked at the moment it was first
    ingested, never on a later sync where the row's hash no longer
    changes. Pass 2 fixes both: it runs over EVERY row on EVERY sync,
    completely independent of Pass 1's content-hash gate, and reconciles
    against ANY pending (decision IS NULL) Lead for that ASIN regardless
    of source -- Lead Sheet is the team's single master sheet, so its
    Client Rating is authoritative for any Atlas lead with that ASIN,
    however it originally got into Atlas. Also backfills va_notes onto
    a pre-existing lead that never had it (e.g. a manual lead whose
    ASIN also has real VA Notes on the sheet).

    Where an ASIN's ROW repeats (re-sourced product), the LAST
    occurrence's Client Rating/VA Notes are authoritative -- same
    "most recent wins" convention as ingest_sheet_lead_row.

    Returns a summary dict for the scheduler tick / manual run to log.
    """
    sh = open_sheet(LEAD_SHEET_URL)
    ws = sh.worksheet(LEAD_SHEET_TAB)
    rows = ws.get_all_values()
    if not rows:
        return {"baselined": 0, "ingested": 0, "decisions_applied": 0, "skipped_unchanged": 0, "notes_backfilled": 0}

    header = rows[0]
    db = SessionLocal()
    baselined = ingested = decisions_applied = skipped_unchanged = notes_backfilled = 0
    try:
        # ---- PASS 1: new/changed row ingestion ----
        # Keyed by content_hash, NOT asin -- see SheetLeadSyncState's own
        # docstring for the two real incidents that happened keying this
        # on ASIN instead (the same ASIN legitimately appears more than
        # once in Lead Sheet with different row content over time).
        known_hashes = {s.content_hash for s in db.query(SheetLeadSyncState).all()}
        is_first_run = len(known_hashes) == 0
        latest_by_asin = {}  # asin -> payload, last occurrence wins, for Pass 2

        for row in rows[1:]:
            payload = _row_to_payload(header, row)
            asin_raw = _extract(payload, ASIN_ALIASES)
            if not asin_raw:
                continue
            asin = str(asin_raw).strip().upper()
            latest_by_asin[asin] = payload
            content_hash = _row_content_hash(row)

            if content_hash in known_hashes:
                skipped_unchanged += 1
                continue

            known_hashes.add(content_hash)
            date_last_added = str(_extract(payload, DATE_LAST_ADDED_ALIASES) or "").strip()
            db.add(SheetLeadSyncState(asin=asin, content_hash=content_hash, date_last_added=date_last_added))

            if is_first_run:
                # The entire first-ever run baselines every row (however
                # many times an ASIN repeats) without ever ingesting --
                # Lead Sheet's 720 historical rows are the team's own
                # already-settled decisions, not new Atlas leads.
                baselined += 1
                continue

            ingest_sheet_lead_row(payload, db)
            ingested += 1

        # Real bug found live, 2026-09-08 (Tamara: "I have VA leads
        # added today on the sheet that aren't showing") -- this used
        # to be a bare db.flush(), which sends Pass 1's writes to the
        # SQLite connection WITHOUT committing, leaving that write
        # transaction open for the entire duration of Pass 2 below.
        # apply_lead_decision (in Pass 2) can trigger ProductRepository.
        # add_watch for an "oos"/"watch" decision, which opens its OWN
        # separate SessionLocal() and commits immediately -- a second
        # writer, in the SAME process, colliding with the still-open
        # first one on the same SQLite file. That's a self-deadlock,
        # not ordinary contention: it reproduced on EVERY single run
        # (confirmed live -- 100% failure since 2026-09-07 12:36, the
        # sheet always has at least one pending oos/watch lead),
        # because the outer transaction can never release the lock
        # while it's itself blocked waiting on the inner commit.
        # Committing here instead makes Pass 1's writes durable and
        # releases the lock before Pass 2 (and anything it calls) ever
        # needs to write again.
        db.commit()

        # ---- PASS 2: decision + VA-notes reconciliation, ANY source ----
        for asin, payload in latest_by_asin.items():
            pending = db.query(Lead).filter(Lead.asin == asin, Lead.decision.is_(None)).all()
            if not pending:
                continue

            va_notes = _extract(payload, VA_NOTES_ALIASES)
            decision = normalize_client_rating(_extract(payload, CLIENT_RATING_ALIASES))

            for lead in pending:
                # Real bug found live, 2026-09-06 (Tamara): "only fill
                # if currently blank" meant a VA editing/adding to a
                # note AFTER the first sync never propagated -- the
                # sheet's VA Notes column is the one source of truth
                # here (Atlas never writes to it), so it should always
                # be mirrored, not just backfilled once.
                if va_notes and va_notes != lead.va_notes:
                    lead.va_notes = va_notes
                    notes_backfilled += 1
                if decision:
                    apply_lead_decision(lead, decision)
                    if lead.source == "sheet":
                        lead.synced_to_sheet_at = datetime.now(timezone.utc)  # sheet already has this, nothing to push back
                    decisions_applied += 1

        db.commit()
    finally:
        db.close()

    return {
        "baselined": baselined, "ingested": ingested,
        "decisions_applied": decisions_applied, "skipped_unchanged": skipped_unchanged,
        "notes_backfilled": notes_backfilled, "is_first_run": is_first_run,
    }


def push_decision_to_sheet(lead_id: int) -> bool:
    """
    Atlas -> sheet push, called IMMEDIATELY at decision time (not on the
    poll cycle) -- see resolve_item/review_decide's own calls -- AND
    from the standalone "save a note without deciding yet" action (see
    app/routes/review_queue.py's save-note route), since Tamara asked
    for a way to write a comment back to the VA independent of making a
    Buy/Reject call. Writes whatever's actually set -- Client Rating
    only if a decision exists, Atlas Notes if present, Purchased Qty if
    present -- into the matching Lead Sheet row for this lead's ASIN.

    Works for ANY lead source, not just source="sheet" (2026-09-05,
    same reasoning as Pass 2's reconciliation in pull_and_ingest_va_
    leads: Lead Sheet is the team's single master sheet, so it's the
    right place to push a decision/note back to regardless of how this
    particular ASIN first entered Atlas) -- but only if a matching row
    for this ASIN actually exists on the sheet; if not, this is a
    no-op (nothing to push to).

    Best-effort: returns False rather than raising on any failure,
    since the Atlas-side decision/note this follows has ALREADY been
    committed and must never be rolled back by a Sheets API hiccup.

    Matches the MOST RECENT row for this ASIN on the sheet (mirroring
    ingest_sheet_lead_row's own "most recent pending" convention) --
    if the VA re-sourced the same ASIN more than once, the newest
    listing is the one this decision/note almost certainly concerns.
    """
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if lead is None:
            return False
        asin, decision, atlas_notes, purchased_qty = lead.asin, lead.decision, lead.atlas_notes, lead.purchased_qty
    finally:
        db.close()

    if decision is None and not atlas_notes and purchased_qty is None:
        return False  # nothing to push

    try:
        sh = open_sheet(LEAD_SHEET_URL)
        ws = sh.worksheet(LEAD_SHEET_TAB)
        rows = ws.get_all_values()
        header = rows[0]

        asin_col = next((i for i, h in enumerate(header) if h.strip().lower() == "asin"), None)
        if asin_col is None:
            return False

        target_row_num = None
        for i in range(len(rows) - 1, 0, -1):  # search from the bottom -- most recent
            if len(rows[i]) > asin_col and rows[i][asin_col].strip().upper() == asin:
                target_row_num = i + 1  # 1-indexed sheet row
                break
        if target_row_num is None:
            return False

        def col_letter(name: str, create_at_end: bool = False):
            for i, h in enumerate(header):
                if h.strip().lower() == name.lower():
                    return i + 1
            if create_at_end:
                new_col = len(header) + 1
                ws.update_cell(1, new_col, name)
                header.append(name)
                return new_col
            return None

        # Only ever write Client Rating when a real decision exists --
        # a note-only save (decision is None) must NEVER overwrite
        # whatever rating is already on the sheet.
        if decision is not None:
            rating_col = col_letter("Client Rating")
            if rating_col:
                ws.update_cell(target_row_num, rating_col, DECISION_TO_CLIENT_RATING.get(decision, "Review"))

        if atlas_notes:
            # Real bug found live, 2026-09-07 (Tamara: "the notes I write
            # ... are not feeding back into the sheet"): this used to
            # write to a SEPARATE "Atlas Notes" column it silently
            # auto-created at the far right of the sheet (create_at_end=
            # True) -- correctly written every time, but into a column
            # nobody was looking at (confirmed live: only 3 rows had
            # anything in it). Lead Sheet already has a real, actively-
            # used "Client Notes" column right next to Client Rating
            # (675 of 726 rows had genuine team history in it) -- that's
            # what Tamara and her team actually read. Now appends into
            # THAT column instead, never replacing what's already
            # there -- a full overwrite would have destroyed real,
            # pre-existing manual notes the instant Atlas commented on a
            # lead someone had already annotated by hand. Re-pushing an
            # unchanged note (e.g. resolving a lead a second time with
            # the same comment still in the box) is a no-op rather than
            # duplicating the same line forever.
            #
            # No "[Atlas YYYY-MM-DD]" prefix (2026-09-08, Tamara: "I
            # don't want the Atlas date stamp on there") -- appends the
            # plain note text as its own line, nothing prepended.
            notes_col = col_letter("Client Notes")
            if notes_col:
                existing = rows[target_row_num - 1][notes_col - 1] if len(rows[target_row_num - 1]) >= notes_col else ""
                new_line = atlas_notes
                if not existing.rstrip().endswith(new_line):
                    combined = f"{existing}\n{new_line}" if existing.strip() else new_line
                    ws.update_cell(target_row_num, notes_col, combined)

        if purchased_qty is not None:
            qty_col = col_letter("Purchased Qty")
            if qty_col:
                ws.update_cell(target_row_num, qty_col, purchased_qty)

        return True
    except Exception as exc:
        print(f"push_decision_to_sheet failed for lead {lead_id} ({asin}): {exc}")
        return False
