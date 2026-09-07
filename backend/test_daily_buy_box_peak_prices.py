"""
Regression test for KeepaParser.daily_buy_box_peak_prices, 2026-09-07
(Tamara, re: B0FQCB7YS9: "we are not looking at the average here we are
looking at individual days that it passed the criteria not an average" --
daily_buy_box_prices' single midnight-per-day snapshot was missing a
real, confirmed intraday price spike: 2026-08-27, buy box jumped to
£181.54 for ~2 hours then reverted, invisible to the old snapshot
method which only saw £154.69 for that whole day).

Builds a synthetic Keepa product with a controlled BUY_BOX_SHIPPING
(index 18) triple series rather than depending on a real, live ASIN --
stable and portable, no Keepa API call.

Run with `python test_daily_buy_box_peak_prices.py` (plain script, no
pytest).
"""
from datetime import datetime, timedelta, timezone

from app.keepa.parser import KeepaParser

KEEPA_EPOCH = KeepaParser.KEEPA_EPOCH


def minutes_since_epoch(dt: datetime) -> int:
    return int((dt - KEEPA_EPOCH).total_seconds() / 60)


def make_product(price_points: list) -> dict:
    """price_points: [(datetime, price_gbp), ...] in chronological order."""
    series = []
    for dt, price_gbp in price_points:
        series.extend([minutes_since_epoch(dt), round(price_gbp * 100), 0])
    csv = [None] * 19
    csv[18] = series
    return {"csv": csv}


now = datetime.now(timezone.utc)


def days_ago(n, hour=12):
    return (now - timedelta(days=n)).replace(hour=hour, minute=0, second=0, microsecond=0)


# Scenario: price is £100 for most of the window, then on day "5 days
# ago" it spikes to £200 for a couple of hours before reverting to £100
# the same day -- the exact "brief intraday spike" shape confirmed live.
price_points = [
    (days_ago(10), 100.0),
    (days_ago(5, hour=8), 200.0),   # spike starts
    (days_ago(5, hour=10), 100.0),  # reverts same day
]
product = make_product(price_points)
parser = KeepaParser(product)

# 1 -- the OLD snapshot method (daily_buy_box_prices) misses the spike
# entirely, since its one snapshot per day is taken at midnight, before
# the spike started and after it had already reverted.
snapshot_prices = parser.daily_buy_box_prices(14)
spike_day_index = 14 - 5 - 1  # oldest-first list, "5 days ago" position
assert snapshot_prices[spike_day_index] == 100.0, (
    f"sanity check: the old snapshot method should show £100 (missing the spike), got {snapshot_prices[spike_day_index]}"
)
print("test 1: daily_buy_box_prices (old snapshot method) confirms it misses the intraday spike -- shows £100: ok")

# 2 -- the NEW peak method correctly catches the spike on that exact day.
peak_prices = parser.daily_buy_box_peak_prices(14)
assert peak_prices[spike_day_index] == 200.0, (
    f"daily_buy_box_peak_prices must catch the intraday spike -- expected £200, got {peak_prices[spike_day_index]}"
)
print("test 2: daily_buy_box_peak_prices correctly catches the £200 intraday spike on the right day: ok")

# 3 -- days before/after the spike are unaffected (still £100, carried
# forward correctly).
assert peak_prices[spike_day_index - 1] == 100.0
assert peak_prices[spike_day_index + 1] == 100.0
print("test 3: days before/after the spike are unaffected, price correctly carried forward: ok")

# 4 -- a day with no price data at all before the first tracked price
# is 0.0, same convention as daily_buy_box_prices.
assert peak_prices[0] == 0.0, "before the first tracked price, a day must be 0.0, not a guess"
print("test 4: a day before any tracked price is 0.0, matching daily_buy_box_prices' own convention: ok")

# 5 -- a product with no buy-box series at all returns all zeros, not an error.
empty_parser = KeepaParser({"csv": []})
assert empty_parser.daily_buy_box_peak_prices(7) == [0.0] * 7
print("test 5: a product with no CSV data returns all-zero days, no error: ok")

print("\nALL TESTS PASSED.")
