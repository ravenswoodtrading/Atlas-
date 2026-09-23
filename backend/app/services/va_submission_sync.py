"""Match sheet submissions before applying edits; never identify a lead by price."""
import json
from collections import defaultdict

from app.database.models import Lead, SheetLeadSubmission
from app.routes.leads import ingest_sheet_lead_row

AUDIT_COLUMNS = {'buy box on lead date', 'price drop (>5%)'}
# See the guard in sync_submissions: more than this many known submissions missing from one read of the sheet
# (and more than this share of them) means the read was bad, not that the rows were deleted.
MAX_MISSING_ROWS = 50
MAX_MISSING_FRACTION = 0.25


def normalized(payload):
    return {' '.join(k.lower().split()): str(v).strip() for k, v in payload.items()
            if k.strip() and not k.startswith('_atlas_') and ' '.join(k.lower().split()) not in AUDIT_COLUMNS}


def identity(payload):
    p = normalized(payload)
    return p.get('asin', '').upper(), p.get('date', '')


def match_rows(previous, current):
    """Exact rows first, then unique ASIN/date, then unique ASIN (date correction).

    Remaining rows sharing an unmatched ASIN are ambiguous and must not be
    ingested. An additional row is new when the original rows still exist.
    """
    old, new = set(range(len(previous))), set(range(len(current)))
    matches = {}
    keys = (lambda p: json.dumps(normalized(p), sort_keys=True), identity,
            lambda p: identity(p)[0])
    for level, key in enumerate(keys):
        left, right = defaultdict(list), defaultdict(list)
        for i in sorted(old): left[key(previous[i])].append(i)
        for i in sorted(new): right[key(current[i])].append(i)
        for k in left.keys() & right.keys():
            a, b = left[k], right[k]
            if level and (len(a) != 1 or len(b) != 1):
                continue
            for oi, ni in zip(a, b):
                matches[ni] = oi
                old.remove(oi)
                new.remove(ni)
    uncertain_asins = {identity(previous[i])[0] for i in old}
    ambiguous = {i for i in new if identity(current[i])[0] in uncertain_asins}
    return matches, new - ambiguous, ambiguous, old


def sync_submissions(db, payloads):
    states = db.query(SheetLeadSubmission).filter(SheetLeadSubmission.active.is_(True)).all()
    initialized = db.query(SheetLeadSubmission).first() is not None
    counts = dict(baselined=0, ingested=0, updated=0, skipped_unchanged=0,
                  ambiguous=0, is_first_run=not initialized)
    if not initialized:
        # Establish identity without resurrecting historical reviewed submissions.
        candidates = defaultdict(list)
        for lead in db.query(Lead).filter(Lead.source == 'sheet').all():
            try:
                candidates[identity(json.loads(lead.raw_sheet_data or '{}'))].append(lead)
            except (ValueError, TypeError):
                continue
        occurrences = defaultdict(int)
        for p in payloads: occurrences[identity(p)] += 1
        for p in payloads:
            found = candidates[identity(p)]
            linked = found[0].id if len(found) == 1 and occurrences[identity(p)] == 1 else None
            db.add(SheetLeadSubmission(asin=identity(p)[0], payload=json.dumps(p), lead_id=linked))
            counts['baselined'] += 1
        return counts
    previous = [json.loads(s.payload) for s in states]
    matches, new, ambiguous, missing = match_rows(previous, payloads)
    # Guard, 2026-09-21: every stored submission had been flagged inactive (the only thing that ever does so is
    # the loop at the bottom of this function), so the next sync saw all 843 sheet rows as new and the
    # ingestion circuit breaker refused, leaving VA leads unsynced. The likeliest way in is one bad read of the
    # sheet (empty or partial) whose rows match almost nothing: every stored submission looks "gone" and is
    # deactivated, and that deactivation is committed. A real sheet never loses a large share of its rows in one
    # hour, so refuse instead -- raising here happens before anything is applied, and the caller's session rolls
    # back. Same shape as the ingestion breaker in google_sheets_lead_sync (an absolute AND a relative bar).
    if len(missing) > MAX_MISSING_ROWS and len(missing) > MAX_MISSING_FRACTION * len(states):
        raise RuntimeError(
            f"Refusing to sync: {len(missing)} of {len(states)} known lead submissions are missing from this "
            "read of the sheet -- almost certainly a bad or partial read, not the VA deleting that many rows. "
            "No changes written; nothing was deactivated."
        )
    for ni, oi in matches.items():
        state, p = states[oi], payloads[ni]
        if normalized(previous[oi]) == normalized(p):
            counts['skipped_unchanged'] += 1
        else:
            lead = db.get(Lead, state.lead_id) if state.lead_id else None
            if lead is not None:
                ingest_sheet_lead_row(p, db, existing_lead=lead)
            counts['updated'] += 1
        state.payload = json.dumps(p)
    # A webhook can create a Lead before the poller sees the new submission.
    # Link only an unclaimed ASIN/date identity; ASIN alone would merge repeats.
    linked_ids = {r[0] for r in db.query(SheetLeadSubmission.lead_id).all() if r[0] is not None}
    unclaimed = defaultdict(list)
    for existing in db.query(Lead).filter(Lead.source == 'sheet').all():
        if existing.id not in linked_ids:
            try:
                unclaimed[identity(json.loads(existing.raw_sheet_data or '{}'))].append(existing)
            except (ValueError, TypeError):
                continue
    for ni in sorted(new):
        p = payloads[ni]
        matches = unclaimed[identity(p)]
        if len(matches) > 1:
            counts['ambiguous'] += 1
            continue
        existing = matches.pop() if matches else None
        lead = ingest_sheet_lead_row(p, db, existing_lead=existing, new_submission=existing is None)
        db.add(SheetLeadSubmission(asin=identity(p)[0], payload=json.dumps(p), lead_id=lead.id))
        counts['ingested'] += 1
    uncertain_asins = {identity(payloads[i])[0] for i in ambiguous}
    for oi in missing:
        if states[oi].asin not in uncertain_asins:
            states[oi].active = False
    counts['ambiguous'] += len(ambiguous)
    return counts
