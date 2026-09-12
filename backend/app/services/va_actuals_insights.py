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


def sold_cohort_metrics(batches):
    """Totals for sold units from the selected purchase cohort, through export end.

    FIFO allocation runs across all purchases before selecting this cohort.
    Missing expectations and costs remain unavailable rather than becoming zero.
    """
    units = profit = revenue = cost = expected = 0.0
    missing_expected_units = missing_cost_units = 0.0
    leads = 0
    for batch in batches:
        if batch.get('issue'):
            continue
        sold = False
        for sale in batch.get('allocations', []):
            quantity = sale['allocated']
            if quantity <= 0:
                continue
            sold = True
            units += quantity
            profit += sale['allocated_profit']
            revenue += sale['allocated_sales']
            if batch.get('expected_profit') is None:
                missing_expected_units += quantity
            else:
                expected += batch['expected_profit'] * quantity
            if sale.get('allocated_cog') is None or sale['allocated_cog'] <= 0:
                missing_cost_units += quantity
            else:
                cost += sale['allocated_cog']
        leads += int(sold)
    expected_profit = None if missing_expected_units else expected
    return dict(units=units, leads=leads, actual_profit=profit, revenue=revenue,
        cost=None if missing_cost_units else cost, expected_profit=expected_profit,
        profit_difference=profit - expected_profit if expected_profit is not None else None,
        profit_difference_pct=(profit - expected_profit) / expected_profit * 100
            if expected_profit is not None and expected_profit > 0 else None,
        roi=profit / cost * 100 if cost > 0 and not missing_cost_units else None,
        margin=profit / revenue * 100 if revenue > 0 else None,
        missing_expected_units=missing_expected_units, missing_cost_units=missing_cost_units)


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
    stock_asins = len(unique_ledger)

    # Forecast profit still sitting in unsold ("remaining") stock, at the
    # VA sheet's own per-unit expected_profit rate, then discounted by the
    # SAME actual-vs-forecast shortfall already observed on units that HAVE
    # sold (Tamara, 2026-09-11) -- a raw sheet-rate forecast on remaining
    # stock would repeat the exact over-optimism Section 2 of the report
    # already documents (prices/profit consistently coming in below the
    # sheet's forecast), so it's scaled down to what similar stock has
    # actually realised rather than taken at face value.
    expected_sold_profit_total = sum(b['expected_sold_profit'] for b in eligible if b.get('expected_sold_profit') is not None)
    actual_profit_same_population = sum(b['actual_profit'] for b in eligible if b.get('expected_sold_profit') is not None)
    forecast_accuracy_pct = (actual_profit_same_population / expected_sold_profit_total - 1) * 100 if expected_sold_profit_total else None
    remaining_forecast_profit = sum((b.get('expected_profit') or 0) * b['remaining'] for b in eligible)
    remaining_expected_profit = (
        remaining_forecast_profit * (1 + forecast_accuracy_pct / 100) if forecast_accuracy_pct is not None else None
    )

    return dict(rows=selected, selection=selection, target_days=target_days,
        sold_cohort=sold_cohort_metrics(selected),
        target_counts={label: sum(b['target_result'] == label for b in selected) for label in
            ('Met target', 'Missed sell-through target', 'Below financial target', 'Insufficient data')},
        undated=sum(not b.get('purchased_on') for b in batches),
        insights=dict(top=top[:10], problems=problems, replens=replens),
        totals=dict(purchases=len(selected), units=sum(b['quantity'] for b in selected), covered=len(eligible),
            sold=sum(b['sold'] for b in eligible), remaining=sum(b['remaining'] for b in eligible),
            profit=sum(b['actual_profit'] for b in eligible), returns=returns, stock=stock, stock_asins=stock_asins,
            forecast_accuracy_pct=forecast_accuracy_pct, remaining_forecast_profit=remaining_forecast_profit,
            remaining_expected_profit=remaining_expected_profit,
            review=len(selected)-len(eligible)), timing=timing)
