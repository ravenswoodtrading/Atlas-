"""VA purchase attribution: oldest eligible batch first, capped at purchased units."""
from collections import defaultdict
from datetime import date


def allocate_sales(purchases, sales, period_start, period_end):
    batches = [dict(p, allocations=[], sold=0, revenue=0.0, actual_profit=0.0, actual_cog=0.0, cost_complete=True) for p in purchases]
    grouped = defaultdict(list)
    adjustments = {s['asin'] for s in sales if s['units'] <= 0}
    for batch in batches:
        purchased = batch.get('purchased_on')
        batch['issue'] = batch.get('issue') or (
            'Purchase date unavailable' if not purchased else
            'Upload sales covering the purchase date' if not period_start or purchased < period_start else
            'Purchase is after the export period' if purchased > period_end else
            'Returns or adjustments require review' if batch['asin'] in adjustments else '')
        grouped[batch['asin']].append(batch)
    # Allocate every dated batch independently. A problem on one VA row (for
    # example a missing Buy Sheet match) must not erase otherwise valid FIFO
    # history for the same ASIN. Dated batches still sort oldest first, so the
    # first ten sold units go to the oldest ten purchased units.
    for group in grouped.values():
        group.sort(key=lambda b: (b.get('purchased_on') or date.max, b['id']))
    for sale in sorted(sales, key=lambda s: (s['date'], s['source_row'])):
        available = max(0, sale['units'])
        for batch in grouped.get(sale['asin'], []):
            if batch['issue'] or batch['purchased_on'] > sale['date'] or not available:
                continue
            quantity = min(available, batch['quantity'] - batch['sold'])
            if not quantity:
                continue
            fraction = quantity / sale['units']
            batch['allocations'].append(dict(sale, allocated=quantity,
                allocated_sales=sale['sales'] * fraction, allocated_profit=sale['profit'] * fraction,
                allocated_cog=sale['cog'] * fraction if sale.get('cog') is not None else None,
                unit_price=sale['sales'] / sale['units']))
            batch['sold'] += quantity
            batch['revenue'] += sale['sales'] * fraction
            batch['actual_profit'] += sale['profit'] * fraction
            if sale.get('cog') is None or sale['cog'] <= 0:
                batch['cost_complete'] = False
            else:
                batch['actual_cog'] += sale['cog'] * fraction
            available -= quantity
    for b in batches:
        b['remaining'] = b['quantity'] - b['sold']
        b['average_price'] = b['revenue'] / b['sold'] if b['sold'] else None
        b['price_gap'] = b['average_price'] - b['expected_price'] if b['average_price'] is not None and b.get('expected_price') is not None else None
        b['first_sale'] = b['allocations'][0]['date'] if b['allocations'] else None
        b['sell_through'] = b['allocations'][-1]['date'] if b['remaining'] == 0 else None
        b['elapsed_days'] = (period_end - b['purchased_on']).days if period_end and b.get('purchased_on') and period_end >= b['purchased_on'] else None
        b['sell_through_days'] = (b['sell_through'] - b['purchased_on']).days if b['sell_through'] else None
        b['expected_sold_profit'] = b['expected_profit'] * b['sold'] if b.get('expected_profit') is not None else None
        b['status'] = 'Needs review' if b['issue'] else 'Sold through' if not b['remaining'] else 'Part sold' if b['sold'] else 'No allocated sales'
        b['actual_roi'] = b['actual_profit'] / b['actual_cog'] * 100 if b['sold'] and b['cost_complete'] and b['actual_cog'] > 0 else None
        b['actual_margin'] = b['actual_profit'] / b['revenue'] * 100 if b['sold'] and b['revenue'] > 0 else None
        financial_pass = ((b['actual_roi'] is not None and b['actual_roi'] >= 25 - 1e-9)
            or (b['actual_margin'] is not None and b['actual_margin'] >= 14 - 1e-9))
        if b['issue']:
            b['target_result'] = 'Insufficient data'
        elif b['sell_through_days'] is not None and b['sell_through_days'] > 30:
            b['target_result'] = 'Missed sell-through target'
        elif b['sell_through_days'] is not None:
            b['target_result'] = ('Met target' if financial_pass else 'Insufficient data'
                if b['actual_roi'] is None or b['actual_margin'] is None else 'Below financial target')
        elif b['elapsed_days'] is not None and b['elapsed_days'] <= 30:
            b['target_result'] = 'Within first 30 days'
        else:
            b['target_result'] = 'Missed sell-through target'
    return batches
