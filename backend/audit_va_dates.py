"""Inspect real VA dates and current imports without changing data."""
import json,sqlite3
from pathlib import Path
from app.services.google_sheets_client import open_sheet
from app.services.google_sheets_lead_sync import LEAD_SHEET_URL,LEAD_SHEET_TAB
from app.services.va_performance_service import PURCHASING_SHEET_URL,purchase_rows,_value
folder=Path('report_checks'); folder.mkdir(exist_ok=True)
all_rows={}
for name,url,tab in [('va',LEAD_SHEET_URL,LEAD_SHEET_TAB),('buy',PURCHASING_SHEET_URL,'Buy Sheet')]:
    values=open_sheet(url).worksheet(tab).get_all_values()
    headers={}
    for i,h in enumerate(values[0]): headers.setdefault(' '.join(h.lower().split()),i)
    rows=[{h:r[i] if i<len(r) else '' for h,i in headers.items()} for r in values[1:]]
    all_rows[name]=rows
    print(name+' headers: '+json.dumps(headers),flush=True)
    print(name+' date samples: '+json.dumps([{k:v for k,v in r.items() if 'date' in k or k in ('asin','purchased qty','quantity')} for r in rows if any(r.values())][:8]),flush=True)
(folder/'sheet_audit.json').write_text(json.dumps(all_rows),encoding='utf-8')
purchases=purchase_rows(all_rows['va'],all_rows['buy'])
from collections import Counter
print('Purchase issues: '+str(Counter(b['issue'] or 'matched' for b in purchases)),flush=True)
print('Unmatched examples: '+json.dumps([{k:str(b[k]) for k in ('id','asin','submitted','purchased_on','quantity','issue')} for b in purchases if b['issue']][:12]),flush=True)
c=sqlite3.connect('file:atlas.db?mode=ro',uri=True)
for table in ('va_sales_imports','va_sales_lines','report_uploads','sku_performance_snapshots'):
    try: print(table, c.execute('select count(*) from '+table).fetchone()[0],flush=True)
    except sqlite3.OperationalError: print(table,'not present',flush=True)
try: print('Sales coverage:',c.execute('select period_start,period_end,imported_at from va_sales_imports').fetchall(),flush=True)
except sqlite3.OperationalError: pass
c.close()
