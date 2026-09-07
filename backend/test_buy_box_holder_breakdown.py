"""
Regression test for KeepaParser.buy_box_holder_breakdown, 2026-09-07
(Tamara, re: B0CFV7Z7SJ: "this only had amazon on the buy box in the
past 30 days which is a red flag I should see" -- the existing
amazon_buy_box_percentage() only reads the CURRENT-moment buy-box
holder, so it silently reports 0 whenever Amazon happens to be between
stock right when the page is viewed, hiding a genuine recent pattern of
Amazon totally dominating the buy box).

Builds a synthetic Keepa product with a controlled buyBoxSellerIdHistory
series rather than depending on a real, live ASIN -- stable and
portable, no Keepa API call.

Run with `python test_buy_box_holder_breakdown.py` (plain script, no
pytest).
"""
from datetime import datetime, timedelta, timezone

from app.keepa.parser import KeepaParser

KEEPA_EPOCH = KeepaParser.KEEPA_EPOCH
AMAZON_ID = KeepaParser.AMAZON_UK_SELLER_ID
COMPETITOR_ID = "A1COMPETITOR123"


def minutes_since_epoch(dt: datetime) -> int:
    return int((dt - KEEPA_EPOCH).total_seconds() / 60)


def make_product(changes: list) -> dict:
    """changes: [(datetime, seller_id_or_None), ...] in chronological order.
    None encodes Keepa's own "no holder" sentinel ("-1")."""
    history = []
    for dt, seller_id in changes:
        history.append(str(minutes_since_epoch(dt)))
        history.append("-1" if seller_id is None else seller_id)
    return {"buyBoxSellerIdHistory": history}


now = datetime.now(timezone.utc)


def days_ago(n, hour=12):
    return (now - timedelta(days=n)).replace(hour=hour, minute=0, second=0, microsecond=0)


# Scenario 1: Amazon-exclusive -- Amazon holds the buy box the whole
# window, alternating only with "no holder" (out of stock gaps), never
# a third party. This is the exact real pattern confirmed for B0CFV7Z7SJ.
changes = [
    (days_ago(40), AMAZON_ID),   # before the window -- establishes "current holder" at window start
    (days_ago(20), None),        # brief out-of-stock gap
    (days_ago(19), AMAZON_ID),   # Amazon retakes it
]
parser = KeepaParser(make_product(changes))
result = parser.buy_box_holder_breakdown(30)
assert result["third_party_ever_won"] is False, f"expected no third-party win, got {result}"
assert result["amazon_minutes"] > 0, f"expected some amazon_minutes, got {result}"
assert result["amazon_minutes"] + result["competitor_minutes"] + result["no_holder_minutes"] == 30 * 24 * 60, (
    f"minutes must sum to exactly the window, got {result}"
)
print("test 1: Amazon-exclusive scenario (no third-party win, real B0CFV7Z7SJ pattern) is detected correctly: ok")

# Scenario 2: mixed -- a competitor wins the buy box at some point in
# the window, so the red flag must NOT fire. This is the real pattern
# confirmed for B0FQCB7YS9.
changes = [
    (days_ago(40), AMAZON_ID),
    (days_ago(15), COMPETITOR_ID),
    (days_ago(10), AMAZON_ID),
]
parser = KeepaParser(make_product(changes))
result = parser.buy_box_holder_breakdown(30)
assert result["third_party_ever_won"] is True, f"expected a third-party win, got {result}"
assert result["competitor_minutes"] > 0 and result["amazon_minutes"] > 0, f"expected both sellers represented, got {result}"
assert result["amazon_minutes"] + result["competitor_minutes"] + result["no_holder_minutes"] == 30 * 24 * 60
print("test 2: mixed Amazon/competitor scenario correctly flags a third-party win: ok")

# Scenario 3: no history at all -- must return the safe all-zero
# default, never an error or a guess.
parser = KeepaParser({})
result = parser.buy_box_holder_breakdown(30)
assert result == {"amazon_minutes": 0, "competitor_minutes": 0, "no_holder_minutes": 0, "third_party_ever_won": False}
print("test 3: no buyBoxSellerIdHistory at all returns the safe all-zero default: ok")

# Scenario 4: history that never enters the window at all (last change
# is long before window_start) -- the pre-window holder should still be
# time-weighted across the entire window, not dropped.
changes = [(days_ago(90), AMAZON_ID)]
parser = KeepaParser(make_product(changes))
result = parser.buy_box_holder_breakdown(30)
assert result["amazon_minutes"] == 30 * 24 * 60, f"expected the full window attributed to Amazon, got {result}"
assert result["third_party_ever_won"] is False
print("test 4: a holder set before the window carries forward for the whole window: ok")

print("\nALL TESTS PASSED.")
