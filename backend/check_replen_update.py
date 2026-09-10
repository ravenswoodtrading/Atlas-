from app.services.replen_service import ReplenService
import sqlite3
print(ReplenService.import_uploaded_actuals())
db = sqlite3.connect('file:atlas.db?mode=ro', uri=True)
print('RECENT SCANS', db.execute("select occurred_at,detail from activity_log where activity_type='replen_check' order by occurred_at desc limit 3").fetchall())
assert db.execute("select count(*) from replen_items r where r.notes like 'Added from uploaded STK actuals:%' and exists (select 1 from amazon_inventory_ledger_lines l where l.asin=r.asin and l.customer_returns>0)").fetchone()[0] == 0
print('PASS: no automatic STK replen entries have recorded returns')
