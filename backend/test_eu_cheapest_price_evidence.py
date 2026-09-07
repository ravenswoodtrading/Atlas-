"""
Regression test for the EU "cheapest price regardless of viability" +
"which markets were actually checked" fix, 2026-09-07 (Tamara, re:
B076H61X15: "I can see that this is for sale in other EU countries...
even if it has never been profitable this is things I want to see and
I want the cheapest price in the last 30 days and which country").

Real bug found live: Italy was selling this ASIN at a genuinely huge
margin (38-47% ROI) for 24 of the last 30 days, but Atlas's stored
classification showed eu_a2a_viable_days_recent: 0 -- because Italy was
never actually queried that scan (Keepa token budget ran out partway
through EU_MARKETPLACES = ["DE","FR","ES","IT"], and Italy is last).
The old eu_source_evidence_by_marketplace only ever records a market
once it clears a VIABLE margin, so a real-but-unprofitable price (or a
genuinely un-checked market) were both invisible and indistinguishable
from each other.

Builds a synthetic Product + raw Keepa buy-box series rather than
depending on a real, live ASIN -- stable and portable, no Keepa API call.

Run with `python test_eu_cheapest_price_evidence.py` (plain script, no
pytest).
"""
from datetime import datetime, timedelta, timezone

from app.keepa.parser import KeepaParser
from app.models.product import Product
from app.services.sourcing_classifier import SourcingClassifier

KEEPA_EPOCH = KeepaParser.KEEPA_EPOCH


def minutes_since_epoch(dt: datetime) -> int:
    return int((dt - KEEPA_EPOCH).total_seconds() / 60)


def make_raw(price_gbp_or_eur: float) -> dict:
    """A flat, constant buy-box price for the whole recent window."""
    now = datetime.now(timezone.utc)
    dt = now - timedelta(days=29, hours=12)
    series = [minutes_since_epoch(dt), round(price_gbp_or_eur * 100), 0]
    csv = [None] * 19
    csv[18] = series
    return {"csv": csv}


product = Product(
    asin="B0EUCHEAP1", title="Test Product", brand="TestBrand", category="test",
    fba_fee=3.0, eu_vat_rate_used=0.20,
)

uk_raw = make_raw(35.0)  # UK sells at £35 the whole window
eu_products = {
    "DE": make_raw(30.0),   # real EU price, but ROI here is well below the 17% viable bar
    "FR": None,             # never checked this scan (e.g. token budget ran out)
    "ES": None,             # never checked this scan
    "IT": make_raw(16.77),  # the genuinely cheap, highly-viable price (matches the real find)
}

evidence = SourcingClassifier.compute_recent_evidence(uk_raw, eu_products, product, "test")
for field_name, value in evidence.items():
    setattr(product, field_name, value)
product.eu_markets_checked = ["DE", "IT"]  # FR/ES were never queried this scan

# 1 -- the cheapest EU price is correctly identified as IT's, not DE's,
# even though IT also happens to be the viable one here (the two are
# tracked independently -- see test 2 for a case where they diverge).
assert product.eu_cheapest_price_recent_marketplace == "IT", product.eu_cheapest_price_recent_marketplace
assert 0 < product.eu_cheapest_price_recent_gbp < 20.0, product.eu_cheapest_price_recent_gbp  # ~€16.77 in GBP, any real FX rate
print("test 1: cheapest EU price across markets is correctly identified (IT, the €16.77 listing): ok")

# 2 -- a real, but NEVER viable, price is still captured as "cheapest"
# when it's the lowest one seen -- this is the exact gap that made
# B076H61X15's real Italian price invisible.
product2 = Product(asin="B0EUCHEAP2", title="Test", brand="TestBrand", category="test",
                    fba_fee=3.0, eu_vat_rate_used=0.20)
uk_raw2 = make_raw(20.0)  # UK price low enough that no EU source is viable
eu_products2 = {"DE": make_raw(15.0), "FR": None, "ES": None, "IT": None}
evidence2 = SourcingClassifier.compute_recent_evidence(uk_raw2, eu_products2, product2, "test")
for field_name, value in evidence2.items():
    setattr(product2, field_name, value)
product2.eu_markets_checked = ["DE"]

assert product2.eu_source_viable_days_recent == 0, "sanity check: this DE price must not be viable"
assert product2.eu_cheapest_price_recent_gbp > 0, "a real, non-viable EU price must still be captured"
assert product2.eu_cheapest_price_recent_marketplace == "DE"
print("test 2: a real EU price that never clears the viable bar is still captured as 'cheapest', not dropped: ok")

# 3 -- classify() on product2 (nothing viable) still falls through to
# "OA / unclear", and its reasoning now carries the cheapest-price and
# eu_markets_checked fields so this is never mistaken for "no EU data
# at all".
classification = SourcingClassifier.classify(product2, brand_repeat_count=0)
assert classification.sourcing_tag == "OA / unclear", classification.sourcing_tag
assert classification.reasoning["eu_cheapest_price_recent_gbp"] > 0
assert classification.reasoning["eu_cheapest_price_recent_marketplace"] == "DE"
assert classification.reasoning["eu_markets_checked"] == ["DE"]
print("test 3: OA/unclear reasoning carries the real cheapest EU price + which markets were actually checked: ok")

# 4 -- a product with NO EU data checked at all (all four markets None)
# still returns a safe, all-zero/empty result -- never an error, never
# a fabricated price.
product3 = Product(asin="B0EUCHEAP3", title="Test", brand="TestBrand", category="test",
                    fba_fee=3.0, eu_vat_rate_used=0.20)
evidence3 = SourcingClassifier.compute_recent_evidence(uk_raw2, {"DE": None, "FR": None, "ES": None, "IT": None}, product3, "test")
assert evidence3["eu_cheapest_price_recent_gbp"] == 0.0
assert evidence3["eu_cheapest_price_recent_marketplace"] == ""
print("test 4: zero EU data anywhere returns a safe empty result, not an error: ok")

print("\nALL TESTS PASSED.")
