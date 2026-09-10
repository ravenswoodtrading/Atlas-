"""Read-only progress and live submission-migration check."""
import json
import sqlite3
from pathlib import Path
from collections import Counter
from datetime import date
from va_price_drop_audit import selected, price_on_day, classification, FOLDER

cache = json.loads((FOLDER/'history.json').read_text())
rows = json.loads((FOLDER/'source.json').read_text())
leads = selected(rows)
outcomes = Counter(classification(r['target'], price_on_day(cache[r['asin']]['series'], date.fromisoformat(r['date'])))
                   for r in leads if r['asin'] in cache)
print('Saved ASIN histories:', len(cache), 'With Buy Box history:', sum(bool(v['series']) for v in cache.values()))
print('Leads checked so far:', dict(outcomes))
with sqlite3.connect('file:atlas.db?mode=ro', uri=True) as db:
    exists = db.execute("SELECT count(*) FROM sqlite_master WHERE name='sheet_lead_submissions'").fetchone()[0]
    print('Live submission table exists:', bool(exists))
    if exists:
        print('Tracked submissions:', db.execute('SELECT count(*) FROM sheet_lead_submissions').fetchone()[0])
    print('Latest VA sync:', db.execute("SELECT last_summary FROM scheduler_status WHERE name='va_lead_sheet_sync'").fetchone())
    print('Recent token usage:', db.execute('SELECT category, tokens, occurred_at FROM token_usage_events ORDER BY id DESC LIMIT 5').fetchall())
