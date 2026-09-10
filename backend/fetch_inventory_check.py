"""Read-only inventory report request; save Amazon's original response locally."""
from pathlib import Path
from datetime import datetime, timezone
from app.sp_api.client import get_sp_api_client
folder=Path('report_checks'); folder.mkdir(exist_ok=True)
client=get_sp_api_client()
if not client:
    raise SystemExit('Amazon connection is not configured')
print('Requesting UK FBA Manage Inventory report...',flush=True)
result=client._run_report('GET_FBA_MYI_ALL_INVENTORY_DATA','UK')
if result is None:
    raise SystemExit('Amazon inventory report failed; no stock assumptions made')
path=folder/('amazon_inventory_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'.txt')
path.write_text(result,encoding='utf-8')
print('Saved '+str(path),flush=True)
import csv,io,json
rows=list(csv.DictReader(io.StringIO(result),delimiter='\t'))
print(json.dumps({'rows':len(rows),'columns':list(rows[0]) if rows else []}),flush=True)
