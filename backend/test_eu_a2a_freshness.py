"""
Ad-hoc verification script for app/services/eu_a2a_freshness_service.py
(NOT a permanent regression test -- deletes its own fixture rows after
running, unlike test_*.py files elsewhere in this repo). Run with
`python test_eu_a2a_freshness.py`.

Creates 6 synthetic ProductRecord rows (ZZFRESH001-006) covering the
main branches, monkeypatches get_sp_api_client() to return a FakeSpClient
keyed only on those 6 ASINs, and runs the REAL recheck_pending_eu_a2a()
end-to-end against the sandbox's own (staged, already-migrated) copy of
atlas.db.

IMPORTANT: this db copy also contains Tamara's real pre-existing
ProductRecord rows, some of which independently satisfy the same
"pending + EU-sourced" query this service uses. FakeSpClient.
get_item_offers therefore returns None (a safe, realistic "no answer
available") for any ASIN it doesn't recognise, exactly like a real
failed/unreachable SP-API call would -- recheck_pending_eu_a2a treats
that as "inconclusive" and leaves the record untouched. We then assert
that none of those real rows got modified, on top of asserting each of
the 6 synthetic rows landed in its expected bucket.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord
from app.services import eu_a2a_freshness_service as svc

db = SessionLocal()

FIXTURE_PREFIX = "ZZFRESH"

# --- clean up any leftover fixture rows from a previous run -----------
db.query(ProductRecord).filter(ProductRecord.asin.like(f"{FIXTURE_PREFIX}%")).delete(synchronize_session=False)
db.commit()

# --- snapshot every OTHER pending EU A2A record's review value, before running --
# Keyed by row id (primary key), NOT asin -- product_records holds one row
# PER SCAN, so the same ASIN can have several rows (an old reviewed one
# and a newer pending one); keying by asin alone would let an "after"
# lookup silently land on a different physical row for that same ASIN
# and produce a false positive here.
before = {
    r.id: r.review
    for r in db.query(ProductRecord)
    .filter(ProductRecord.review.is_(None))
    .filter(ProductRecord.best_source_marketplace.in_(("DE", "FR", "ES", "IT")))
    .all()
}
print(f"{len(before)} real pre-existing pending EU A2A record row(s) in this db copy (will assert these are untouched).")

now = datetime.now(timezone.utc)


def make_record(asin, review, marketplace, cost_gbp, buy_box_now, fba_fee=3.0, category_name=""):
    return ProductRecord(
        asin=asin, title=f"Fixture {asin}", brand="FixtureBrand", category="Fixture",
        category_name=category_name,
        review=review,
        best_source_marketplace=marketplace, best_source_cost_gbp=cost_gbp,
        buy_box_now=buy_box_now, buy_box_90d=buy_box_now,
        fba_fee=fba_fee,
        scanned_at=now,
    )


fixtures = [
    # 1: confirmed OOS at source (SP-API Success, price/offer_count empty) -> removed
    make_record("ZZFRESH001", None, "DE", 20.0, 60.0),
    # 2: price rose, no longer profitable -> removed
    make_record("ZZFRESH002", None, "DE", 20.0, 30.0),
    # 3: price basically unchanged / still cheap -> still_viable, untouched
    make_record("ZZFRESH003", None, "FR", 20.0, 60.0),
    # 4: SP-API call fails outright (returns None) -> inconclusive, untouched
    make_record("ZZFRESH004", None, "ES", 20.0, 60.0),
    # 5: already reviewed -> not even picked up by the query
    make_record("ZZFRESH005", "up", "DE", 20.0, 60.0),
    # 6: UK-sourced, not EU A2A -> not picked up by the query
    make_record("ZZFRESH006", None, "UK", 20.0, 60.0),
]
for f in fixtures:
    db.add(f)
db.commit()

FAKE_OFFERS = {
    "ZZFRESH001": {"status": "Success", "price": None, "offer_count": 0},
    "ZZFRESH002": {"status": "Success", "price": 45.0, "offer_count": 2},  # EUR -> pushes cost well above viable
    "ZZFRESH003": {"status": "Success", "price": 20.5, "offer_count": 3},  # EUR, ~unchanged
    # ZZFRESH004 deliberately absent -> simulates a failed call (None)
}


class FakeSpClient:
    def get_item_offers(self, asin, marketplace):
        if asin not in FAKE_OFFERS:
            return None
        return FAKE_OFFERS[asin]


svc.get_sp_api_client = lambda: FakeSpClient()

result = svc.recheck_pending_eu_a2a()
print("recheck_pending_eu_a2a() ->", result)

db.expire_all()


def get(asin):
    return db.query(ProductRecord).filter(ProductRecord.asin == asin).first()


r1, r2, r3, r4, r5, r6 = (get(f"ZZFRESH00{i}") for i in range(1, 7))

assert r1.review == "stale_auto", f"expected ZZFRESH001 removed, got review={r1.review!r}"
assert "out of stock" in r1.review_reason, r1.review_reason

assert r2.review == "stale_auto", f"expected ZZFRESH002 removed, got review={r2.review!r}"
assert "no longer profitable" in r2.review_reason, r2.review_reason

assert r3.review is None, f"expected ZZFRESH003 left untouched (still viable), got review={r3.review!r}"
assert r4.review is None, f"expected ZZFRESH004 left untouched (inconclusive/failed call), got review={r4.review!r}"
assert r5.review == "up", "ZZFRESH005 (already reviewed) must never be touched by the query at all"
assert r6.review is None, "ZZFRESH006 (UK-sourced) must never be picked up by the query at all"

# --- confirm every real pre-existing pending record was left exactly as it was --
after = {
    r.id: r.review
    for r in db.query(ProductRecord)
    .filter(ProductRecord.id.in_(list(before.keys())))
    .all()
}
changed = {rid: (before[rid], after.get(rid)) for rid in before if after.get(rid) != before[rid]}
assert not changed, f"real pre-existing record ROWS were modified by the sweep (fixture bug or real bug): {changed}"
print(f"all {len(before)} real pre-existing pending record row(s) confirmed untouched.")

assert result["checked"] >= 4  # at least the 4 synthetic eligible rows (real rows are "inconclusive" too)
assert result["removed"] == 2
assert result["still_viable"] >= 1
assert result["inconclusive"] >= 1

# --- clean up fixture rows ---------------------------------------------
db.query(ProductRecord).filter(ProductRecord.asin.like(f"{FIXTURE_PREFIX}%")).delete(synchronize_session=False)
db.commit()
db.close()

print("\nALL PASS")
