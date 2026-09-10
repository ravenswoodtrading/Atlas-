"""Remove exact duplicate campaigns, preserving the furthest cursor and backup."""
import sqlite3,json
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
db=sqlite3.connect('atlas.db',timeout=30)
db.row_factory=sqlite3.Row
db.execute('BEGIN IMMEDIATE')
rows=[dict(r) for r in db.execute('select * from scan_queue_items')]
backup=Path('report_checks')/('scan_queue_before_dedupe_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'.json')
backup.write_text(json.dumps(rows,indent=2))
groups=defaultdict(list)
for r in rows:
    key=(r['brand'].strip().lower(),tuple(sorted(set(filter(None,(r['category_ids'] or '').split(','))))))
    groups[key].append(r)
removed=[]
for key,items in groups.items():
    if len(items)<2: continue
    keep=max(items,key=lambda r:(r['next_page'] or 0,r['last_run_at'] or '',-r['id']))
    db.execute('update scan_queue_items set position=?,scanned_count=? where id=?',
        (min(r['position'] for r in items),sum(r['scanned_count'] or 0 for r in items),keep['id']))
    for r in items:
        if r['id']==keep['id']: continue
        db.execute('update automation_settings set last_scan_queue_item_id=? where last_scan_queue_item_id=?',(keep['id'],r['id']))
        db.execute('delete from scan_queue_items where id=?',(r['id'],))
        removed.append((key[0],r['id']))
db.commit()
print('REMOVED',removed)
print('REMAINING',db.execute('select count(*),count(distinct brand) from scan_queue_items').fetchone()[:])
print('BACKUP',backup)
