"""Purchase-period selection and explainable VA product insights."""
from calendar import monthrange
from datetime import date
from collections import defaultdict


def date_selection(period='all', month='', quarter='', start='', end=''):
    if period == 'all':
        return dict(period=period, month=month, quarter=quarter, start=start, end=end, lower=None, upper=None, label='All purchase dates')
    try:
        if period == 'month':
            lower = date.fromisoformat(month + '-01')
            upper = lower.replace(day=monthrange(lower.year, lower.month)[1])
            label = lower.strftime('%B %Y')
        elif period == 'quarter':
            year, q = quarter.split('-Q')
            if q not in ('1', '2', '3', '4'):
                raise ValueError()
            lower = date(int(year), (int(q)-1)*3+1, 1)
            last_month = lower.month+2
            upper = date(lower.year, last_month, monthrange(lower.year, last_month)[1])
            label = quarter
        elif period == 'custom':
            lower, upper = date.fromisoformat(start), date.fromisoformat(end)
            if lower > upper:
                raise ValueError()
            label = f'{lower} to {upper}'
        else:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Choose a valid month, quarter (for example 2026-Q2), or an inclusive start/end date range.') from None
    return dict(period=period, month=month, quarter=quarter, start=start, end=end, lower=lower, upper=upper, label=label)


def report_view(batches, buys, selection, as_of, target_days=30):
    # Allocation must run on ALL batches before filtering; earlier batches retain sales.
    selected = [b for b in batches if selection['lower'] is None or
        (b.get('purchased_on') and selection['lower'] <= b['purchased_on'] <= selection['upper'])]
    eligible = [b for b in selected if not b['issue']]
    grouped = defaultdict(list)
    for b in selected:
        grouped[b['asin']].append(b)
    top, problems, replens = [], [], []
    # Cohort metrics deliberately use the selected VA purchase batches while
    # considering every allocated sale through the latest uploaded export.
    timing = {
        '0-30 days': {'leads': 0, 'units': 0, 'profit': 0.0},
        '31-60 days': {'leads': 0, 'units': 0, 'profit': 0.0},
        '61-90 days': {'leads': 0, 'units': 0, 'profit': 0.0},
        '90+ days sold through': {'leads': 0, 'units': 0, 'profit': 0.0},
        'Still unsold after 90 days': {'leads': 0, 'units': 0, 'profit': 0.0},
    }
    for b in eligible:
        days = b.get('sell_through_days')
        if days is not None:
            bucket = '0-30 days' if days <= 30 else '31-60 days' if days <= 60 else '61-90 days' if days <= 90 else '90+ days sold through'
            timing[bucket]['leads'] += 1
        elif b.get('elapsed_days') is not None and b['elapsed_days'] > 90:
            timing['Still unsold after 90 days']['leads'] += 1
        for sale in b.get('allocations', []):
            sale_days = (sale['date'] - b['purchased_on']).days if b.get('purchased_on') and sale.get('date') else None
            if sale_days is None:
                continue
            bucket = '0-30 days' if sale_days <= 30 else '31-60 days' if sale_days <= 60 else '61-90 days' if sale_days <= 90 else '90+ days sold through'
            timing[bucket]['units'] += sale.get('allocated', 0)
            timing[bucket]['profit'] += sale.get('allocated_profit', 0) or 0
    for asin, group in grouped.items():
        valid = [b for b in group if not b['issue']]
        fully_sold = len(valid) == len(group) and all(b['sell_through_days'] is not None for b in valid)
        comparable = valid and all(b.get('expected_price') is not None and b['expected_price'] > 0 for b in valid)
        sold = sum(b['sold'] for b in valid)
        revenue = sum(b['revenue'] for b in valid)
        expected = sum(b['expected_price'] * b['sold'] for b in valid) if comparable else None
        gap = (revenue-expected)/sold if comparable and sold else None
        duration = max(b['sell_through_days'] for b in valid) if fully_sold else None
        item = dict(asin=asin, title=group[0]['title'], batches=group, sold=sold,
            profit=sum(b['actual_profit'] for b in valid), gap=gap, days=duration,
            uplift=(revenue/expected-1)*100 if expected else None)
        if fully_sold and all(b['target_result'] == 'Met target' for b in valid):
            top.append(item)
        flags = []
        for b in valid:
            reasons = []
            if b['remaining'] and b['elapsed_days'] is not None and b['elapsed_days'] > target_days:
                reasons.append(f"{int(b['remaining'])} units not yet matched to sales after {b['elapsed_days']} days")
            elif b['sell_through_days'] is not None and b['sell_through_days'] > target_days:
                reasons.append(f"Took {b['sell_through_days']} days to sell through")
            if b['price_gap'] is not None and b['price_gap'] < 0:
                reasons.append(f"Selling £{-b['price_gap']:.2f} per unit below plan")
            if b['sold'] and b['actual_profit'] < 0:
                reasons.append(f"Allocated loss £{-b['actual_profit']:.2f}")
            if b['target_result'] == 'Below financial target':
                reasons.append('ROI below 25% and margin below 14%')
            if reasons:
                flags.append(dict(batch=b, reasons=reasons))
        if flags:
            problems.append(dict(item, flags=flags))
        # Replenishments are product-level evidence after the first-ever VA purchase,
        # never counted afresh for each repeat VA batch or inferred from sales alone.
        all_for_asin = [b for b in batches if b['asin'] == asin]
        if not as_of or any(not b.get('purchased_on') for b in all_for_asin):
            continue
        first = min(b['purchased_on'] for b in all_for_asin)
        initial = sum(b['quantity'] for b in all_for_asin if b['purchased_on'] == first)
        original = [r for r in buys if r['asin'] == asin and r['date'] == first]
        later = [r for r in buys if r['asin'] == asin and first < r['date'] <= as_of]
        if original and sum(r['quantity'] for r in original) >= initial and later:
            replens.append(dict(item, initial_date=first, initial_units=initial,
                extra_units=sum(r['quantity'] for r in later), orders=sorted(later, key=lambda r: (r['date'], r['row']))))
    top.sort(key=lambda r: (r['days'], -(r['uplift'] if r['uplift'] is not None else float('-inf')), -r['profit'], r['asin']))
    problems.sort(key=lambda r: (-len(r['flags']), r['profit'], r['asin']))
    replens.sort(key=lambda r: (-r['extra_units'], r['asin']))
    unique_ledger = {b['asin']: b.get('ledger') for b in eligible if b.get('ledger')}
    returns = sum((v.get('returns') or 0) for v in unique_ledger.values())
    stock = sum((v.get('ending_balance') or 0) for v in unique_ledger.values())
    return dict(rows=selected, selection=selection, target_days=target_days,
        target_counts={label: sum(b['target_result'] == label for b in selected) for label in
            ('Met target', 'Missed sell-through target', 'Below financial target', 'Insufficient data')},
        undated=sum(not b.get('purchased_on') for b in batches),
        insights=dict(top=top[:10], problems=problems, replens=replens),
        totals=dict(purchases=len(selected), units=sum(b['quantity'] for b in selected), covered=len(eligible),
            sold=sum(b['sold'] for b in eligible), remaining=sum(b['remaining'] for b in eligible),
            profit=sum(b['actual_profit'] for b in eligible), returns=returns, stock=stock,
            review=len(selected)-len(eligible)), timing=timing)
