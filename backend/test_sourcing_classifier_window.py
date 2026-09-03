"""
Tests for atlas-competitor-watch-classification-v1.md's original fixes:
1. RECENT_WINDOW_DAYS widened 10 -> 30.
2. Historical A2A evidence preserved across reclassification even when
   the CURRENT classification legitimately changes (e.g. reverts to
   OA/unclear once the evidence ages out of the window).

Run with `python test_sourcing_classifier_window.py` (same plain-script
style as the other test_*.py files here, no pytest).

See test_competitor_watch_current_vs_historical.py for the follow-up
fix's own tests (all-4-marketplace historical evidence, and the
currently_buyable/ProductRecord.recommendation decoupling).

SourcingClassifier.classify() is a pure function of a Product (per its
own docstring, "easy to unit-test against constructed Product
fixtures") -- every test here builds a Product directly with the
recent-window fields already set (as BrandScanService.scan would have
via compute_recent_evidence), no Keepa/DB access needed at all.
_persist_classification is tested against a lightweight duck-typed
stand-in object (only needs .sourcing_reasoning_json/.sourcing_tag/
.currently_buyable), also no DB.
"""
import json
from types import SimpleNamespace

from app.models.product import Product
from app.services.sourcing_classifier import (
    SourcingClassifier, RECENT_WINDOW_DAYS, RECENT_VIABLE_ROI_PCT,
)
from app.services.seller_watch_service import SellerWatchService

assert RECENT_WINDOW_DAYS == 30, f"expected the widened 30-day window, got {RECENT_WINDOW_DAYS}"
print("window widened to 30 days: ok")


def eu_a2a_product(marketplace="DE", viable_days=1, best_roi=None, **overrides):
    """
    A product with real EU A2A evidence for ONE marketplace somewhere
    in the recent window -- eu_source_evidence_by_marketplace is
    derived to stay consistent with the scalar fields, same as
    SourcingClassifier.compute_recent_evidence would produce.
    """
    best_roi = best_roi if best_roi is not None else RECENT_VIABLE_ROI_PCT + 10
    base = dict(
        asin="B0FAKE001", title="Fake Product", brand="FakeBrand", category="Fixture",
        best_source_marketplace=marketplace,
        eu_source_priced_days_recent=30,
        eu_source_viable_days_recent=viable_days,
        eu_source_best_roi_recent=best_roi,
        eu_source_best_roi_cost_gbp=10.0,
        eu_source_best_roi_date="2026-08-14",
        eu_source_best_roi_marketplace=marketplace,
        eu_source_evidence_by_marketplace=(
            {marketplace: {
                "viable_days": viable_days, "best_roi": best_roi,
                "best_buy_price": 10.0, "best_date": "2026-08-14",
            }} if viable_days else {}
        ),
        buy_box_now=15.0, buy_box_90d=25.0,
        uk_dip_days_recent=0, uk_price_min_recent=0.0, uk_price_min_recent_date="",
        offers_now=10,
        profit=-2.0,  # not profitable TODAY specifically -- currently_buyable is a separate concern
    )
    base.update(overrides)
    return Product(**base)


def no_evidence_product(**overrides):
    base = dict(
        asin="B0FAKE002", title="Fake Product", brand="FakeBrand", category="Fixture",
        best_source_marketplace="",
        eu_source_priced_days_recent=0, eu_source_viable_days_recent=0,
        eu_source_evidence_by_marketplace={},
        buy_box_now=20.0, buy_box_90d=20.0,
        uk_dip_days_recent=0, uk_price_min_recent=0.0, uk_price_min_recent_date="",
        offers_now=10,  # > WHOLESALE_MAX_SELLERS, no multipack title -- no wholesale signal either
        profit=0.0,
    )
    base.update(overrides)
    return Product(**base)


# --- TEST 1 -- current EU A2A ------------------------------------------
p = eu_a2a_product(viable_days=30, profit=10.0)
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "EU A2A", result.sourcing_tag
print("TEST 1 (current EU A2A): ok")

# --- TEST 2 -- historical EU A2A (20d ago), current source expensive ---
p = eu_a2a_product(viable_days=1, profit=-5.0)  # only 1 viable day in the window, today isn't
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "EU A2A", result.sourcing_tag
print("TEST 2 (historical EU A2A, current source expensive): ok")

# --- TEST 3 -- historical EU A2A (15d ago), UK price temporarily low ---
p = eu_a2a_product(viable_days=1, buy_box_now=15.0, buy_box_90d=25.0, profit=-1.0)
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "EU A2A", result.sourcing_tag
print("TEST 3 (historical EU A2A, UK price temporarily depressed): ok")

# --- TEST 4 -- no A2A evidence at all -----------------------------------
p = no_evidence_product()
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "OA / unclear", result.sourcing_tag
print("TEST 4 (no A2A evidence -> OA/unclear): ok")

# --- TEST 5 -- old A2A evidence (45d ago), none in the last 30 ---------
# compute_recent_evidence (not exercised directly here) would never even
# look at day -45 with a 30-day window -- at the classify() level that
# means zero viable/priced days, same fixture as "no evidence".
p = no_evidence_product(best_source_marketplace="DE")
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "OA / unclear", (
    f"a 45-day-old opportunity outside the 30-day window must NOT classify as EU A2A, got {result.sourcing_tag}"
)
print("TEST 5 (old evidence outside window -> not classified from it): ok")

# --- TEST 6 -- multiple EU countries (evidence identifies the marketplace) --
p = eu_a2a_product(marketplace="DE", viable_days=1)
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "EU A2A", result.sourcing_tag
assert result.reasoning["marketplace"] == "DE", result.reasoning["marketplace"]
print("TEST 6 (multiple EU countries, evidence identifies the marketplace): ok")

# --- TEST 7 -- A2A evidence exists, current source unavailable today ---
p = eu_a2a_product(viable_days=5, profit=0.0)
result = SourcingClassifier.classify(p)
assert result.sourcing_tag == "EU A2A", "historical evidence within the window must keep the classification"
print("TEST 7 (A2A evidence disappears today, classification remains EU A2A): ok")

# --- TEST 8 (new) -- evidence ages out but must survive in the archive -
# Pass 1: real evidence found -> EU A2A, historical_a2a_evidence recorded.
p1 = eu_a2a_product(marketplace="DE", viable_days=1)
classification1 = SourcingClassifier.classify(p1)
assert classification1.sourcing_tag == "EU A2A"
merged1 = SourcingClassifier.merge_evidence(None, classification1)
assert merged1["historical_a2a_evidence"]["eu_a2a"]["DE"] is not None
first_found_at = merged1["historical_a2a_evidence"]["eu_a2a"]["DE"]["first_found_at"]
assert first_found_at

# Pass 2: 30+ days later, nothing left in the (now-moved) window -- current
# classification legitimately reverts, but the archived evidence from
# pass 1 must be preserved byte-for-byte (not even last_confirmed_at
# should move, since nothing new was found this round).
p2 = no_evidence_product(best_source_marketplace="DE")
classification2 = SourcingClassifier.classify(p2)
assert classification2.sourcing_tag != "EU A2A", (
    "current classification MUST be free to change once evidence ages out -- this is correct, not a bug"
)
merged2 = SourcingClassifier.merge_evidence(merged1, classification2)
assert merged2["historical_a2a_evidence"]["eu_a2a"] == merged1["historical_a2a_evidence"]["eu_a2a"], (
    "historical evidence must survive a reclassification that no longer finds it"
)
assert merged2["historical_a2a_evidence"]["eu_a2a"]["DE"]["first_found_at"] == first_found_at
print("TEST 8 (evidence ages out of the window but survives in the archive): ok")

# --- merge_evidence unit tests ------------------------------------------

# fresh evidence of the same marketplace refreshes last_confirmed_at but
# keeps the original first_found_at
p3 = eu_a2a_product(marketplace="DE", viable_days=2, best_roi=40.0)
classification3 = SourcingClassifier.classify(p3)
merged3 = SourcingClassifier.merge_evidence(merged1, classification3)
de_evidence = merged3["historical_a2a_evidence"]["eu_a2a"]["DE"]
assert de_evidence["first_found_at"] == first_found_at, "first_found_at must never move once set"
assert de_evidence["best_roi"] == 40.0, "a fresh confirmation should refresh the evidence numbers"
print("merge_evidence (refresh keeps first_found_at, updates the numbers): ok")

# EU and UK evidence are tracked independently
p_uk = no_evidence_product(uk_dip_days_recent=3, uk_price_min_recent=12.0, uk_price_min_recent_date="2026-08-10")
classification_uk = SourcingClassifier.classify(p_uk)
assert classification_uk.sourcing_tag == "UK A2A", classification_uk.sourcing_tag
merged_uk = SourcingClassifier.merge_evidence(merged1, classification_uk)
assert merged_uk["historical_a2a_evidence"]["eu_a2a"] == merged1["historical_a2a_evidence"]["eu_a2a"], (
    "a UK A2A finding must not disturb previously-recorded EU A2A evidence"
)
assert merged_uk["historical_a2a_evidence"]["uk_a2a"] is not None
print("merge_evidence (EU and UK evidence tracked independently): ok")

# --- _persist_classification writes tag freely but preserves evidence --
listing = SimpleNamespace(sourcing_reasoning_json=None, sourcing_tag=None, currently_buyable=False)

SellerWatchService._persist_classification(listing, classification1, "BUY")  # EU A2A found, current BUY
assert listing.sourcing_tag == "EU A2A"
assert listing.currently_buyable is True
stored = json.loads(listing.sourcing_reasoning_json)
assert stored["historical_a2a_evidence"]["eu_a2a"]["DE"] is not None

SellerWatchService._persist_classification(listing, classification2, None)  # evidence ages out, no current source
assert listing.sourcing_tag != "EU A2A", "sourcing_tag must flip freely, no pinning"
assert listing.currently_buyable is False
stored_after = json.loads(listing.sourcing_reasoning_json)
assert stored_after["historical_a2a_evidence"]["eu_a2a"] == stored["historical_a2a_evidence"]["eu_a2a"], (
    "reclassifying an existing listing must not erase its previously-archived evidence"
)
print("_persist_classification (tag flips freely, evidence archive preserved): ok")

print("\nALL PASS")
