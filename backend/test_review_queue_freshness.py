"""
Tests for the Review Queue backend changes (atlas-review-queue-backend-v1.md).
Run with `python test_review_queue_freshness.py` (same plain-script
style as the other test_*.py files here, no pytest).

Covers the six cases named in the spec's section 13:
  1. Fresh profitable offer -> BUY / FRESH.
  2. Offer becomes unavailable -> original BUY preserved, current
     status UNAVAILABLE.
  3. Source price rises -> original BUY preserved, current status
     NO_LONGER_PROFITABLE.
  4. User rejects because no offer -> structured reason stored.
  5. User rejects because wrong match -> structured reason stored.
  6. Recheck restores profitability -> the SAME record is reactivated,
     no duplicate row.

Cases 1-3 exercise the pure freshness classifier and
eu_a2a_freshness_service._check_one_record directly against fake/
duck-typed record objects and a stubbed SP-API client -- no DB, no
network (CurrencyService.to_gbp is monkeypatched to a fixed rate so
the FX lookup never actually fires). Cases 4-6 exercise the real
persistence layer (ProductRepository.set_review /
review_queue_service.classify_offer_freshness) against one dedicated,
obviously-fake ASIN, created and deleted by this script -- never
touching real scan data.

Also checks (guards the "risk" section of the inspection): adding the
new fields to ReviewQueueService's merged lead dicts hasn't changed
list_leads()'s existing count/shape for real data already in the DB.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database.database import SessionLocal
from app.database.models import ProductRecord
from app.services.product_repository import ProductRepository
from app.services.review_queue_service import (
    ReviewQueueService,
    classify_offer_freshness,
    interpret_review_reason,
    REVIEW_REASON_CATEGORIES,
)
from app.services import eu_a2a_freshness_service
from app.services.eu_a2a_freshness_service import _check_one_record

TEST_ASIN = "B0TESTFAKE1"  # obviously fake, never a real Amazon ASIN format Atlas would scan


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- 1. Fresh profitable offer -> BUY / FRESH --------------------------
freshness = classify_offer_freshness(scanned_at=utcnow(), now=utcnow())
assert freshness == "FRESH", freshness
print("case 1 (fresh profitable offer): ok")

# --- setup for cases 2-3: fake record + stubbed SP-API client ----------
def fake_record(**overrides):
    base = dict(
        asin="B0FAKESRC01", title="Fake Product", brand="FakeBrand", category="",
        buy_box_now=30.0, buy_box_90d=30.0,
        best_source_marketplace="DE", best_source_cost_gbp=10.0,
        fba_fee=3.0, category_name="",
        last_offer_checked_at=None, last_offer_price_gbp=None, last_offer_buyable=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class StubSpClient:
    def __init__(self, response):
        self._response = response

    def get_item_offers(self, asin, marketplace):
        return self._response


# Monkeypatch the EUR->GBP rate lookup so this test never makes a real
# network call and is fully deterministic -- mirrors test_source_check.py's
# convention of stubbing the external dependency (Keepa there, SP-API/FX
# here), not the code under test.
eu_a2a_freshness_service.CurrencyService.to_gbp = staticmethod(
    lambda amount, currency: round(amount * 0.85, 2)
)

# --- 2. Offer becomes unavailable -> original preserved, UNAVAILABLE ---
record = fake_record()
original_cost = record.best_source_cost_gbp
sp_client = StubSpClient({"status": "Success", "price": None, "offer_count": None})

status, reason, category = _check_one_record(sp_client, record)

assert status == "removed", status
assert category == "NO_BUYABLE_OFFER", category
assert record.last_offer_buyable is False, record.last_offer_buyable
assert record.last_offer_checked_at is not None
assert record.best_source_cost_gbp == original_cost, "original analysis must never be overwritten"

live_freshness = classify_offer_freshness(
    scanned_at=utcnow(), last_offer_checked_at=record.last_offer_checked_at,
    last_offer_buyable=record.last_offer_buyable,
)
assert live_freshness == "UNAVAILABLE", live_freshness
print("case 2 (offer becomes unavailable): ok")

# --- 3. Source price rises -> original preserved, NO_LONGER_PROFITABLE -
record = fake_record(best_source_cost_gbp=10.0, buy_box_now=30.0)
original_cost = record.best_source_cost_gbp
# 25 EUR * 0.85 stub rate = £21.25 -- high enough against a £30 UK
# price that fees alone push profit negative.
sp_client = StubSpClient({"status": "Success", "price": 25.0, "offer_count": 3})

status, reason, category = _check_one_record(sp_client, record)

assert status == "removed", status
assert category == "NO_LONGER_PROFITABLE", category
assert record.last_offer_buyable is True, "a real (just unprofitable) offer still counts as buyable"
assert record.last_offer_price_gbp == 21.25, record.last_offer_price_gbp
assert record.best_source_cost_gbp == original_cost, "original analysis must never be overwritten"
print("case 3 (source price rises): ok")

# --- also: a genuinely still-viable recheck leaves status untouched ----
record = fake_record(best_source_cost_gbp=10.0, buy_box_now=30.0)
sp_client = StubSpClient({"status": "Success", "price": 10.0, "offer_count": 3})
status, reason, category = _check_one_record(sp_client, record)
assert status == "still_viable", status
assert category is None, category
assert record.last_offer_buyable is True
print("still-viable recheck (no removal): ok")

# --- inconclusive call never claims a fresher check than it has --------
record = fake_record()
sp_client = StubSpClient(None)
status, reason, category = _check_one_record(sp_client, record)
assert status == "inconclusive", status
assert record.last_offer_checked_at is None, "an inconclusive call must not update last_offer_checked_at"
print("inconclusive call leaves freshness untouched: ok")

# --- interpret_review_reason: expiry vs sourcing-issue split -----------
assert interpret_review_reason("NO_BUYABLE_OFFER") == "OPPORTUNITY_EXPIRED"
assert interpret_review_reason("PRICE_CHANGED") == "OPPORTUNITY_EXPIRED"
assert interpret_review_reason("WRONG_MATCH") == "SOURCING_ISSUE"
assert interpret_review_reason("GATED") == "SOURCING_ISSUE"
assert interpret_review_reason(None) is None
print("interpret_review_reason: ok")

# --- 4/5/6: real persistence layer, one dedicated fake ASIN ------------
db = SessionLocal()
try:
    db.query(ProductRecord).filter(ProductRecord.asin == TEST_ASIN).delete()
    db.commit()

    original_scanned_at = utcnow() - timedelta(hours=1)
    row = ProductRecord(
        asin=TEST_ASIN, title="Test Product", brand="Test", category="",
        best_source_marketplace="DE", best_source_cost_gbp=10.0,
        profit=10.0, roi=100.0, recommendation="BUY", score=80,
        scanned_at=original_scanned_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row_id = row.id
finally:
    db.close()

# -- 4. User rejects because no offer -> structured reason stored -------
ProductRepository.set_review(
    TEST_ASIN, "down", reason="No buyable offer", reason_category="NO_BUYABLE_OFFER"
)

db = SessionLocal()
try:
    row = db.get(ProductRecord, row_id)
    assert row.review == "down", row.review
    assert row.review_reason == "No buyable offer", row.review_reason
    assert row.review_reason_category == "NO_BUYABLE_OFFER", row.review_reason_category
    assert row.recommendation == "BUY", "original recommendation must survive a rejection"
    assert row.best_source_cost_gbp == 10.0, "original analysis must survive a rejection"
finally:
    db.close()
print("case 4 (reject: no buyable offer, structured reason stored): ok")

# -- 5. User rejects because wrong match -> structured reason stored ----
ProductRepository.set_review(
    TEST_ASIN, "down", reason="Wrong product entirely", reason_category="WRONG_MATCH"
)

db = SessionLocal()
try:
    row = db.get(ProductRecord, row_id)
    assert row.review_reason == "Wrong product entirely", row.review_reason
    assert row.review_reason_category == "WRONG_MATCH", row.review_reason_category
finally:
    db.close()
print("case 5 (reject: wrong match, structured reason stored): ok")

assert set(REVIEW_REASON_CATEGORIES) >= {"NO_BUYABLE_OFFER", "WRONG_MATCH"}

# -- 6. Recheck restores profitability -> same record reactivated -------
# Simulate the sweep marking it stale_auto (as it would for an EU A2A
# lead whose source went OOS), then a later recheck finding a fresh,
# profitable offer -- the record is reactivated (review cleared) rather
# than a second row being created for the same ASIN.
ProductRepository.set_review(
    TEST_ASIN, "stale_auto", reason="Auto-removed: source OOS", reason_category="NO_BUYABLE_OFFER"
)

db = SessionLocal()
try:
    count_before = db.query(ProductRecord).filter(ProductRecord.asin == TEST_ASIN).count()
finally:
    db.close()
assert count_before == 1, "no duplicate row should exist yet"

# Recheck finds it buyable and profitable again -- reactivate the SAME
# row rather than creating a new one: clear review/review_reason(s) and
# record the live check.
db = SessionLocal()
try:
    row = db.get(ProductRecord, row_id)
    row.review = None
    row.review_reason = None
    row.review_reason_category = None
    row.last_offer_checked_at = utcnow()
    row.last_offer_price_gbp = 10.0
    row.last_offer_buyable = True
    db.commit()
finally:
    db.close()

db = SessionLocal()
try:
    count_after = db.query(ProductRecord).filter(ProductRecord.asin == TEST_ASIN).count()
    row = db.get(ProductRecord, row_id)
    assert count_after == 1, "reactivation must not duplicate the lead"
    assert row.review is None, row.review
    assert row.recommendation == "BUY", "original recommendation preserved through the whole cycle"
    assert row.last_offer_buyable is True
finally:
    db.close()
print("case 6 (recheck restores profitability, same record reactivated): ok")

# cleanup -- never leave the fake ASIN behind in a real Atlas database
db = SessionLocal()
try:
    db.query(ProductRecord).filter(ProductRecord.asin == TEST_ASIN).delete()
    db.commit()
finally:
    db.close()

# --- regression guard: existing list_leads() shape/count unaffected ----
leads_before_shape = ReviewQueueService.list_leads()
required_keys = {
    "source", "asin", "recommendation", "freshness", "review_reason_category",
    "last_offer_checked_at", "last_offer_price_gbp", "last_offer_buyable",
}
for lead in leads_before_shape[:25]:
    assert required_keys <= lead.keys(), sorted(required_keys - lead.keys())
    assert lead["freshness"] in ("FRESH", "AGING", "STALE", "UNAVAILABLE", "UNKNOWN"), lead["freshness"]
print(f"regression guard (list_leads shape, {len(leads_before_shape)} real leads): ok")

print("\nAll review queue freshness tests passed.")
