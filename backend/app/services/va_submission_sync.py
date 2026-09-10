"""Match sheet submissions before applying edits; never identify a lead by price."""
import json
from collections import defaultdict

from app.database.models import Lead, SheetLeadSubmission
from app.routes.leads import ingest_sheet_lead_row

AUDIT_COLUMNS = {'buy box on lead date', 'price drop (>5%)'}


def normalized(payload):
    return {' '.join(k.lower().split()): str(v).strip() for k, v in payload.items()
            if k.strip() and ' '.join(k.lower().split()) not in AUDIT_COLUMNS}


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
    for ni in sorted(new):
        p = payloads[ni]
        lead = ingest_sheet_lead_row(p, db, new_submission=True)
        db.add(SheetLeadSubmission(asin=identity(p)[0], payload=json.dumps(p), lead_id=lead.id))
        counts['ingested'] += 1
    uncertain_asins = {identity(payloads[i])[0] for i in ambiguous}
    for oi in missing:
        if states[oi].asin not in uncertain_asins:
            states[oi].active = False
    counts['ambiguous'] = len(ambiguous)
    return counts
