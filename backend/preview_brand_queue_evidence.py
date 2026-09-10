"""Read-only EU brand evidence comparison; no scans or queue changes."""
import sqlite3, json
from collections import defaultdict
from pathlib import Path
from app.services.google_sheets_client import open_sheet
db=sqlite3.connect('file:atlas.db?mode=ro',uri=True)
db.row_factory=sqlite3.Row
rows=open_sheet('1WcO8SQ6cQEmoVG-GUmAJwBde1AIgp4aBg9pUBJ62UA0').worksheet('Buy Sheet').get_all_values()
purchases=defaultdict(list)
for r in rows[1:]:
    if len(r)>14 and r[2].strip(): purchases[r[2].strip().upper()].append(r)
queue={r['brand'].strip().lower() for r in db.execute('select brand from scan_queue_items')}
brands=defaultdict(lambda:dict(units=0,profit=0,cog=0,asins=set(),competitors=set(),finds=set()))
mixed=0
for sale in db.execute('select asin,sum(units) units,sum(profit) profit,sum(cog) cog from va_sales_lines group by asin'):
    ps=purchases.get(sale['asin'],[])
    eu=[r for r in ps if r[14].strip().lower() in {'amazon.de','amazon.fr','amazon.es','amazon.it'}]
    if not eu: continue
    if len(eu)!=len(ps): mixed+=1; continue
    brand=eu[-1][13].strip().lower()
    if not brand: continue
    b=brands[brand]; b['units']+=sale['units']; b['profit']+=sale['profit']; b['cog']+=sale['cog'] or 0; b['asins'].add(sale['asin'])
for r in db.execute("select p.brand,l.asin,l.tracked_seller_id from seller_new_listings l join product_records p on p.id=l.product_record_id where l.sourcing_tag='EU A2A' and l.dismissed=0"):
    brand=(r['brand'] or '').strip().lower()
    if brand:
        brands[brand]['finds'].add(r['asin']); brands[brand]['competitors'].add(r['tracked_seller_id'])
out=[]
for brand in set(brands)|queue:
    b=brands[brand]
    out.append(dict(brand=brand,queued=brand in queue,units=b['units'],profit=round(b['profit'],2),roi=round(100*b['profit']/b['cog'],1) if b['cog']>0 else None,sold_asins=len(b['asins']),competitor_asins=len(b['finds']),competitors=len(b['competitors'])))
out.sort(key=lambda x:(-(int(x['profit']>0)+int(x['competitor_asins']>0)),-x['profit'],-x['competitor_asins']))
Path('report_checks/brand_queue_preview.json').write_text(json.dumps(out,indent=2))
print('MIXED_SOURCE_ASINS_EXCLUDED',mixed)
print('OWN WINNERS',json.dumps(sorted([x for x in out if x['profit']>0],key=lambda x:-x['profit'])[:12]))
print('COMPETITOR',json.dumps(sorted([x for x in out if x['competitor_asins']],key=lambda x:-x['competitor_asins'])[:12]))
print('CURRENT QUEUE',json.dumps([x for x in out if x['queued']]))
