"""One-off VA Buy Box audit. Fetch is resumable; publish changes only two columns."""
import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime, time as daytime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from app.keepa.client import get_keepa_client
from app.services.google_sheets_client import open_sheet

SHEET = '1myUd44MfrKDRv6iGgm0KnvS9oZ4jhIEtCy4vTkrFlA4'
HEADERS = ['Buy Box on lead date', 'Price drop (>5%)']
FOLDER = Path('report_checks/price_drop_2026_09_09')
START, END = date(2026, 7, 9), date(2026, 9, 9)
EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)


def price_on_day(series, day):
    if not series or len(series) % 3:
        return None
    cutoff = datetime.combine(day + timedelta(days=1), daytime.min, ZoneInfo('Europe/London'))
    minute = int((min(cutoff, datetime.now(timezone.utc)) - EPOCH).total_seconds() / 60)
    last = None
    for i in range(0, len(series), 3):
        if series[i] >= minute:
            break
        last = series[i+1:i+3]
    if last is None or last[0] <= 0 or last[1] < 0:
        return None
    return Decimal(last[0] + last[1]) / 100


def classification(target, price):
    try:
        target = Decimal(str(target).replace('£', '').replace(',', '').strip())
        if not target.is_finite() or target <= 0 or price is None:
            return 'Unknown'
        return 'Yes' if target > price * Decimal('1.05') else 'No'
    except InvalidOperation:
        return 'Unknown'


def selected(rows):
    h = rows[0]
    result = []
    for number, row in enumerate(rows[1:], 2):
        try:
            d = datetime.strptime(row[h.index('Date')], '%d %b %y').date()
        except (ValueError, IndexError):
            continue
        if START <= d <= END:
            result.append(dict(row=number, date=d.isoformat(),
                asin=row[h.index('ASIN')].strip().upper(), target=row[h.index('Sale Price')]))
    return result


def fetch():
    FOLDER.mkdir(parents=True, exist_ok=True)
    ws = open_sheet(SHEET).worksheet('Lead Sheet')
    rows = ws.get_all_values()
    (FOLDER/'source.json').write_text(json.dumps(rows), encoding='utf-8')
    leads = selected(rows)
    cache_file = FOLDER/'history.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    for lead in leads:
        if not re.fullmatch(r'[A-Z0-9]{10}', lead['asin']):
            cache[lead['asin']] = dict(series=None, reason='Invalid ASIN in sheet',
                fetched_at=datetime.now(timezone.utc).isoformat(), product_type=None)
    cache_file.write_text(json.dumps(cache), encoding='utf-8')
    pending = list(dict.fromkeys(r['asin'] for r in leads
                                 if r['asin'] not in cache and re.fullmatch(r'[A-Z0-9]{10}', r['asin'])))
    get_keepa_client()  # Load Atlas's credential without printing it.
    session = requests.Session()
    failures = 0
    while pending:
        status = session.get('https://api.keepa.com/token', params={'key': os.environ['KEEPA_API_KEY']}, timeout=30)
        if status.status_code != 200:
            raise RuntimeError(f'Keepa status failed: HTTP {status.status_code}')
        available = status.json().get('tokensLeft', 0)
        batch_size = min(20, max(0, (available - 50)//3), len(pending))
        if not batch_size:
            print(f'Waiting for Keepa refill; {len(cache)} histories saved, {len(pending)} left', flush=True)
            time.sleep(20)
            continue
        batch = pending[:batch_size]
        response = session.get('https://api.keepa.com/product', params={
            'key': os.environ['KEEPA_API_KEY'], 'domain': 2, 'asin': ','.join(batch),
            'buybox': 1, 'history': 1, 'update': -1}, timeout=60)
        if response.status_code == 429:
            time.sleep(20)
            continue
        if response.status_code != 200:
            raise RuntimeError(f'Keepa product lookup failed: HTTP {response.status_code}')
        data = response.json()
        if 'error' in data or 'products' not in data:
            failures += 1
            reason = str(data.get('error') or list(data)).replace(os.environ['KEEPA_API_KEY'], '[redacted]')
            print('Keepa lookup returned no products:', reason, flush=True)
            if failures >= 3:
                raise RuntimeError('Keepa lookup failed three times; saved histories are intact')
            time.sleep(20)
            continue
        failures = 0
        returned = {p['asin']: p for p in data['products']}
        for asin in batch:
            p = returned.get(asin, {})
            csv = p.get('csv') or []
            cache[asin] = dict(series=csv[18] if len(csv) > 18 else None,
                fetched_at=datetime.now(timezone.utc).isoformat(), product_type=p.get('productType'))
        cache_file.write_text(json.dumps(cache), encoding='utf-8')
        pending = pending[len(batch):]
        print(f'Fetched {len(cache)} / {len(set(r["asin"] for r in leads))} ASINs; tokens {data.get("tokensLeft")}', flush=True)
    results = []
    for lead in leads:
        price = price_on_day(cache[lead['asin']]['series'], date.fromisoformat(lead['date']))
        results.append(dict(lead, buy_box=float(price) if price is not None else None,
                            price_drop=classification(lead['target'], price)))
    (FOLDER/'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('RESULTS', dict(Counter(r['price_drop'] for r in results)), flush=True)


def publish():
    from app.database.database import SessionLocal, engine
    from app.database.models import SheetLeadSubmission, SheetLeadSyncState, Lead
    from app.services.va_submission_sync import sync_submissions
    from app.services.google_sheets_lead_sync import _row_content_hash, _row_to_payload
    rows = json.loads((FOLDER/'source.json').read_text())
    results = json.loads((FOLDER/'results.json').read_text())
    ws = open_sheet(SHEET).worksheet('Lead Sheet')
    current = ws.get_all_values()
    if current != rows:
        raise RuntimeError('Sheet changed during lookup. Refresh the source and reuse the history cache before publishing.')
    header = rows[0]
    if any(h in header for h in HEADERS):
        raise RuntimeError('Audit columns already exist; inspect before updating.')
    start = max(len(r) for r in rows)
    after = [r + [''] * (start+2-len(r)) for r in rows]
    after[0][start:start+2] = HEADERS
    cells = [dict(values=[dict(userEnteredValue={'stringValue': h}) for h in HEADERS])]
    by_row = {r['row']: r for r in results}
    for number in range(2, len(rows)+1):
        result = by_row.get(number)
        values = []
        if result:
            value = result['buy_box']
            values = [dict(userEnteredValue={'numberValue': value} if value is not None else {'stringValue': 'Unknown'}),
                      dict(userEnteredValue={'stringValue': result['price_drop']})]
            after[number-1][start:start+2] = [f'{value:.2f}' if value is not None else 'Unknown', result['price_drop']]
        cells.append(dict(values=values))
    # Establish the new submission baseline before any sheet modification.
    SheetLeadSubmission.__table__.create(engine, checkfirst=True)
    with SessionLocal() as db:
        before_ids = {lead.id for lead in db.query(Lead).all()}
        payloads = [_row_to_payload(header, r) for r in rows[1:] if len(r)>3 and r[3].strip()]
        outcome = sync_submissions(db, payloads)
        if outcome['ingested']:
            db.rollback()
            raise RuntimeError('Unexpected new submissions during audit baseline; inspect before writing.')
        # An already-running pre-fix poller must also recognise the audit-only
        # changes. Register the exact formatted post-write rows before publishing.
        known = {s.content_hash for s in db.query(SheetLeadSyncState).all()}
        for r in after[1:]:
            if len(r)<4 or not r[3].strip(): continue
            for variant in (r, list(r)):
                if variant is not r:
                    while variant and variant[-1] == '': variant.pop()
                digest = _row_content_hash(variant)
                if digest not in known:
                    db.add(SheetLeadSyncState(asin=r[3].strip().upper(), content_hash=digest,
                                             date_last_added=r[16] if len(r)>16 else ''))
                    known.add(digest)
        db.commit()
    requests_list = []
    if ws.col_count < start+2:
        requests_list.append({'updateSheetProperties': {'properties': {'sheetId': ws.id,
            'gridProperties': {'columnCount': start+2}}, 'fields': 'gridProperties.columnCount'}})
    requests_list.append({'updateCells': {'start': {'sheetId': ws.id, 'rowIndex': 0, 'columnIndex': start},
        'rows': cells, 'fields': 'userEnteredValue'}})
    for offset, note in enumerate([
        'Keepa Amazon UK Buy Box including shipping, in GBP, at end of lead Date (Europe/London). Today uses latest available. Unknown means no usable Buy Box. One-off audit 9 Jul–9 Sep 2026. Source: https://keepa.com/api-docs/product-object.html',
        'Yes when VA Sale Price is strictly more than 5% above Buy Box on lead date. No means within threshold; Unknown means missing Buy Box or target price. Snapshot of current VA target; later edits require rerunning the audit.'
    ]):
        requests_list.append({'updateCells': {'start': {'sheetId': ws.id, 'rowIndex': 0, 'columnIndex': start+offset},
            'rows': [{'values': [{'note': note, 'userEnteredFormat': {'textFormat': {'bold': True},
                'backgroundColor': {'red': 0.85, 'green': 0.94, 'blue': 0.94}, 'wrapStrategy': 'WRAP'}}]}],
            'fields': 'note,userEnteredFormat'}})
    requests_list.append({'repeatCell': {'range': {'sheetId': ws.id, 'startRowIndex': 1,
        'endRowIndex': len(rows), 'startColumnIndex': start, 'endColumnIndex': start+1},
        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0.00'}}},
        'fields': 'userEnteredFormat.numberFormat'}})
    requests_list.append({'updateDimensionProperties': {'range': {'sheetId': ws.id, 'dimension': 'COLUMNS',
        'startIndex': start, 'endIndex': start+2}, 'properties': {'pixelSize': 170}, 'fields': 'pixelSize'}})
    ws.spreadsheet.batch_update({'requests': requests_list})
    verified = ws.get_all_values()
    for r in results:
        if verified[r['row']-1][start:start+2] != after[r['row']-1][start:start+2]:
            raise RuntimeError(f'Write verification failed at row {r["row"]}')
    for i, row in enumerate(rows):
        if verified[i][:start] != row + ['']*(start-len(row)):
            raise RuntimeError(f'Original data verification failed at row {i+1}')
    with SessionLocal() as db:
        created = [lead.id for lead in db.query(Lead).all() if lead.id not in before_ids]
    print('PUBLISHED AND VERIFIED', len(results), 'rows;', dict(Counter(r['price_drop'] for r in results)),
          '; new Atlas lead IDs during write:', created, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['fetch', 'publish'])
    args = parser.parse_args()
    (fetch if args.action == 'fetch' else publish)()
