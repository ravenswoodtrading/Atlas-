"""
Tests for atlas-competitor-watch-classification-v1.md's follow-up fix:
historical sourcing evidence examined across ALL FOUR EU marketplaces
(DE/FR/ES/IT) independently, decoupled from "can we buy it today"
(which now comes straight from the linked ProductRecord's own
OpportunityEngine recommendation, never recomputed here).

Run with `python test_competitor_watch_current_vs_historical.py` (same
plain-script style as the other test_*.py files here, no pytest).

No Keepa calls, no database access -- SourcingClassifier.classify() is
a pure function of a Product built directly with the evidence fields
already set (as compute_recent_evidence would have produced from a
real Keepa fetch), and _persist_classification is exercised against a
lightweight duck-typed stand-in object, not a real ORM row.

Covers the 8 named scenarios from the spec's section 14, plus the two
worked "Behaviour" demonstrations requested in section 16.
"""
import json
from types import SimpleNamespace

from app.models.product import Product
from app.services.sourcing_classifier import SourcingClassifier
from app.services.seller_watch_service import SellerWatchService


def de_historical_product(**overrides):
    """
    Historical DE evidence (a real, viable EU A2A opportunity ~10 days
    ago) -- current-day fields (best_source_marketplace, buy_box_now,
    etc) are set separately per test to represent whatever's true
    "today", since the whole point of this fix is that these two no
    longer have to agree.
    """
    base = dict(
        asin="B0FAKEDEES", title="Fake Product", brand="FakeBrand", category="Fixture",
        best_source_marketplace="",  # "today's" pick -- set per-test, classify() no longer reads this
        eu_source_priced_days_recent=30,
        eu_source_viable_days_recent=3,
        eu_source_best_roi_recent=47.5,
        eu_source_best_roi_cost_gbp=10.0,
        eu_source_best_roi_date="2026-08-24",  # ~10 days before "today" in this fixture set
        eu_source_best_roi_marketplace="DE",
        eu_source_evidence_by_marketplace={
            "DE": {"viable_days": 3, "best_roi": 47.5, "best_buy_price": 10.0, "best_date": "2026-08-24"},
        },
        buy_box_now=15.0, buy_box_90d=25.0,
        uk_dip_days_recent=0, uk_price_min_recent=0.0, uk_price_min_recent_date="",
        offers_now=10,
        profit=0.0,
    )
    base.update(overrides)
    return Product(**base)


def fake_record(recommendation, marketplace="", cost=0.0, uk_price=0.0, profit=0.0, roi=0.0):
    """Duck-typed stand-in for the linked ProductRecord -- only the fields _persist_classification/the template read."""
    return SimpleNamespace(
        recommendation=recommendation, best_source_marketplace=marketplace,
        best_source_cost_gbp=cost, buy_box_now=uk_price, profit=profit, roi=roi,
    )


# =========================================================================
# TEST 1 -- historical DE, current ES, ES clears BUY
# =========================================================================
p = de_historical_product(best_source_marketplace="ES")  # today's live pick is ES, not DE
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", classification.sourcing_tag
assert classification.reasoning["marketplace"] == "DE", (
    "the HISTORICAL marketplace must be named, not today's current pick", classification.reasoning["marketplace"]
)

record = fake_record("BUY", marketplace="ES", cost=11.0, uk_price=29.99, profit=14.20, roi=89.4)
listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
SellerWatchService._persist_classification(listing, classification, record.recommendation)
assert listing.sourcing_tag == "EU A2A", listing.sourcing_tag
assert listing.currently_buyable is True, listing.currently_buyable
print("TEST 1 (historical DE, current ES with BUY) -> EU A2A, currently_buyable=True: ok")

# =========================================================================
# TEST 2 -- historical DE, current ES, but ES is only CONSIDER (no BUY)
# =========================================================================
p = de_historical_product(best_source_marketplace="ES")
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", classification.sourcing_tag

record = fake_record("CONSIDER", marketplace="ES", cost=13.0, uk_price=25.0, profit=3.0, roi=18.0)
listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
SellerWatchService._persist_classification(listing, classification, record.recommendation)
assert listing.sourcing_tag == "EU A2A", listing.sourcing_tag
assert listing.currently_buyable is False, (
    "CONSIDER must NOT count as currently buyable -- only BUY does", listing.currently_buyable
)
print("TEST 2 (historical DE, current ES with CONSIDER only) -> EU A2A, currently_buyable=False: ok")

# =========================================================================
# TEST 3 -- historical DE, no current EU marketplace at all
# =========================================================================
p = de_historical_product(best_source_marketplace="")  # nothing qualifies today on any of DE/FR/ES/IT
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", classification.sourcing_tag

listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
SellerWatchService._persist_classification(listing, classification, None)  # no linked record / no recommendation
assert listing.sourcing_tag == "EU A2A", listing.sourcing_tag
assert listing.currently_buyable is False, listing.currently_buyable
print("TEST 3 (historical DE, no current EU marketplace) -> EU A2A, currently_buyable=False: ok")

# =========================================================================
# TEST 4 -- historical ES, current DE (the reverse pairing)
# =========================================================================
p = de_historical_product(
    best_source_marketplace="DE",
    eu_source_best_roi_marketplace="ES",
    eu_source_evidence_by_marketplace={
        "ES": {"viable_days": 2, "best_roi": 31.2, "best_buy_price": 15.0, "best_date": "2026-08-20"},
    },
)
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", classification.sourcing_tag
assert classification.reasoning["marketplace"] == "ES", classification.reasoning["marketplace"]
print("TEST 4 (historical ES, current DE) -> EU A2A: ok")

# =========================================================================
# TEST 5 -- multiple historical marketplaces (DE and ES both qualify)
# =========================================================================
p = de_historical_product(
    eu_source_evidence_by_marketplace={
        "DE": {"viable_days": 3, "best_roi": 47.5, "best_buy_price": 10.0, "best_date": "2026-08-20"},
        "ES": {"viable_days": 2, "best_roi": 31.2, "best_buy_price": 11.0, "best_date": "2026-08-25"},
    },
)
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", classification.sourcing_tag
assert classification.eu_evidence.keys() == {"DE", "ES"}, classification.eu_evidence.keys()

merged = SourcingClassifier.merge_evidence(None, classification)
assert set(merged["historical_a2a_evidence"]["eu_a2a"].keys()) == {"DE", "ES"}, (
    "eu_source_evidence_by_marketplace must retain BOTH qualifying marketplaces"
)
print("TEST 5 (DE and ES both qualify -> both retained in eu_source_evidence_by_marketplace): ok")

# =========================================================================
# TEST 6 -- historical evidence survives current deterioration
# =========================================================================
# Day 1: DE has strong evidence, ES has none.
p_day1 = de_historical_product(
    eu_source_evidence_by_marketplace={
        "DE": {"viable_days": 3, "best_roi": 47.5, "best_buy_price": 10.0, "best_date": "2026-08-04"},
    },
)
classification_day1 = SourcingClassifier.classify(p_day1)
merged_day1 = SourcingClassifier.merge_evidence(None, classification_day1)
assert merged_day1["historical_a2a_evidence"]["eu_a2a"]["DE"] is not None

# Later: DE no longer buyable/viable, ES now has strong opportunity.
p_later = de_historical_product(
    eu_source_best_roi_marketplace="ES",
    eu_source_evidence_by_marketplace={
        "ES": {"viable_days": 2, "best_roi": 31.2, "best_buy_price": 11.0, "best_date": "2026-09-01"},
    },
)
classification_later = SourcingClassifier.classify(p_later)
merged_later = SourcingClassifier.merge_evidence(merged_day1, classification_later)

assert merged_later["historical_a2a_evidence"]["eu_a2a"]["DE"] == merged_day1["historical_a2a_evidence"]["eu_a2a"]["DE"], (
    "DE evidence must survive even though DE is no longer the current best source"
)
assert merged_later["historical_a2a_evidence"]["eu_a2a"]["ES"] is not None, (
    "ES's new evidence must also be recorded, alongside DE, not instead of it"
)
print("TEST 6 (DE evidence survives; ES evidence added alongside it, not instead of it): ok")

# =========================================================================
# TEST 7 -- currently_buyable comes only from ProductRecord.recommendation
# =========================================================================
base_classification = SourcingClassifier.classify(de_historical_product())
for recommendation, expected in [("BUY", True), ("CONSIDER", False), ("WATCH", False), ("IGNORE", False), (None, False)]:
    listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
    SellerWatchService._persist_classification(listing, base_classification, recommendation)
    assert listing.currently_buyable is expected, (recommendation, listing.currently_buyable)
print("TEST 7 (currently_buyable = recommendation == 'BUY', nothing else counts): ok")

# =========================================================================
# TEST 8 -- no current best_source_marketplace (the real false-negative cause)
# =========================================================================
p = de_historical_product(best_source_marketplace=None or "")  # blank, exactly as observed live
classification = SourcingClassifier.classify(p)
assert classification.sourcing_tag == "EU A2A", (
    "a blank current best_source_marketplace must NOT block a genuine historical finding -- "
    f"this was the actual cause of the real-world false negatives, got {classification.sourcing_tag}"
)
print("TEST 8 (blank current best_source_marketplace does not block EU A2A): ok")


# =========================================================================
# "Behaviour" demonstrations (section 16 of the spec)
# =========================================================================
print("\n--- Behaviour demo: historical DE, current ES, BUY ---")
p = de_historical_product(best_source_marketplace="ES")
classification = SourcingClassifier.classify(p)
record = fake_record("BUY", marketplace="ES", cost=11.0, uk_price=29.99, profit=14.20, roi=89.4)
listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
SellerWatchService._persist_classification(listing, classification, record.recommendation)
print(f"Historical source: {classification.reasoning['marketplace']}")
print(f"Current source: {record.best_source_marketplace}")
print(f"Historical tag: {listing.sourcing_tag}")
print(f"Current recommendation: {record.recommendation}")
print(f"currently_buyable: {listing.currently_buyable}")
assert listing.sourcing_tag == "EU A2A" and listing.currently_buyable is True

print("\n--- Behaviour demo: historical DE, current has no BUY ---")
p = de_historical_product(best_source_marketplace="")
classification = SourcingClassifier.classify(p)
listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)
SellerWatchService._persist_classification(listing, classification, None)
print(f"Historical source: {classification.reasoning['marketplace']}")
print(f"Current source: (none currently qualifies)")
print(f"Historical tag: {listing.sourcing_tag}")
print(f"Current recommendation: None")
print(f"currently_buyable: {listing.currently_buyable}")
assert listing.sourcing_tag == "EU A2A" and listing.currently_buyable is False

print("\nALL PASS")
