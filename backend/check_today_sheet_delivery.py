"""Read-only comparison of today's saved decisions and live VA sheet."""
import sqlite3
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from app.services.google_sheets_client import open_sheet

db = sqlite3.connect('file:atlas.db?mode=ro', uri=True)
db.row_factory = sqlite3.Row
today = datetime.now(ZoneInfo('Europe/London')).date().isoformat()
leads = db.execute("select id,asin,decision,atlas_notes,purchased_qty,reviewed_at from leads where date(reviewed_at)=? and decision is not null order by reviewed_at", (today,)).fetchall()
print('DATE', today, 'SAVED_DECISIONS', len(leads), flush=True)
ws = open_sheet('https://docs.google.com/spreadsheets/d/1myUd44MfrKDRv6iGgm0KnvS9oZ4jhIEtCy4vTkrFlA4/edit').worksheet('Lead Sheet')
rows = ws.get_all_values()
header = [h.strip().lower() for h in rows[0]]
def cell(row, name):
    i = header.index(name) if name in header else len(row)
    return row[i] if i < len(row) else ''
mapping = {'approved':'Ok','rejected':'Avoid','oos':'Review','watch':'Review','need_more_info':'Review'}
counts = {}
for lead in leads:
    matches = [(i+1,r) for i,r in enumerate(rows) if cell(r,'asin').strip().upper()==lead['asin']]
    if not matches:
        status='no_sheet_row'
    else:
        number,row=matches[-1]
        issues=[]
        if cell(row,'client rating').strip().lower()!=mapping.get(lead['decision'],'Review').lower(): issues.append('rating')
        if lead['atlas_notes'] and lead['atlas_notes'].strip() not in cell(row,'client notes'): issues.append('notes')
        if lead['purchased_qty'] is not None:
            try: qty=float(cell(row,'purchased qty'))
            except ValueError: qty=None
            if qty!=lead['purchased_qty']: issues.append('quantity')
        status=','.join(issues) or 'matched'
    counts[status]=counts.get(status,0)+1
    print(json.dumps(dict(id=lead['id'],asin=lead['asin'],decision=lead['decision'],status=status, sheet_ratings=[(n,cell(r,'client rating')) for n,r in matches])))
errors=[{'row':i+1,'column':rows[0][j], 'text':v[:350]} for i,row in enumerate(rows[1:],1) for j,v in enumerate(row) if 'dns' in v.lower() or 'resolve host' in v.lower()]
print('DNS_CELLS',len(errors),'EXAMPLE',json.dumps(errors[:1]))
print('SUMMARY',json.dumps(counts))
