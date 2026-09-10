"""Extend the existing audit to all dated leads; update only AB/AC."""
import json
from datetime import date
import va_price_drop_audit as audit

audit.START = date(2000, 1, 1)
audit.fetch()
ws = audit.open_sheet(audit.SHEET).worksheet('Lead Sheet')
source = json.loads((audit.FOLDER/'source.json').read_text())
results = json.loads((audit.FOLDER/'results.json').read_text())
assert ws.get_all_values() == source, 'Sheet changed; rerun using cached histories'
assert source[0][27:29] == audit.HEADERS
updates = []
for row in results:
    updates.append({'range':f'AB{row["row"]}:AC{row["row"]}', 'values':[[row['buy_box'] if row['buy_box'] is not None else 'Unknown', row['price_drop']]]})
ws.batch_update(updates, value_input_option='RAW')
actual = ws.get_all_values()
for row in results:
    cells = actual[row['row']-1][27:29]
    assert cells[1] == row['price_drop']
    assert (cells[0] == 'Unknown' if row['buy_box'] is None else abs(float(cells[0])-row['buy_box']) < .001)
print('VERIFIED',len(results),'dated leads',flush=True)
