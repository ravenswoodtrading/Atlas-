"""
Regression test for the daily OA-to-Investigate archive job, 2026-09-07
(Tamara: "maybe we should clear out/archive some leads older than say
14 days" -- confirmed scope: OA/unclear only, recurring daily policy).

Every assertion below checks ONLY the specific fake listing IDs this
test itself created -- never a table-wide count -- precisely because an
earlier test in this same session (test_reclassify_oa_queue.py, since
deleted) accidentally ran its logic against the FULL live table and
briefly touched 613 real production rows. This test only ever reads
back the exact rows it made, and cleans up in a try/finally.

Run with `python test_oa_archive.py` (plain script, no pytest).
"""
from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import SellerNewListing, TrackedSeller
from app.services.seller_watch_service import SellerWatchService

TEST_SELLER_ID = "OAARCHIVETESTSELLER"
ASINS = {
    "old_untouched": "B0OAARCH01",       # 20 days old, OA/unclear, never reviewed -> ARCHIVED
    "recent": "B0OAARCH02",               # 5 days old, OA/unclear -> NOT archived (too recent)
    "manually_classified": "B0OAARCH03",  # 20 days old, but manually_classified=True -> NOT archived
    "already_reviewed": "B0OAARCH04",     # 20 days old, but review="up" -> NOT archived
    "wrong_tag": "B0OAARCH05",            # 20 days old, but tagged EU A2A -> NOT archived (out of scope)
}


def cleanup():
    db = SessionLocal()
    try:
        db.query(SellerNewListing).filter(SellerNewListing.asin.in_(ASINS.values())).delete(synchronize_session=False)
        db.query(TrackedSeller).filter(TrackedSeller.seller_id == TEST_SELLER_ID).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


cleanup()

try:
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=TEST_SELLER_ID, nickname="OA Archive Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        seller_id = seller.id

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=5)

        listing_ids = {}
        specs = [
            ("old_untouched", "OA / unclear", old, False, None),
            ("recent", "OA / unclear", recent, False, None),
            ("manually_classified", "OA / unclear", old, True, None),
            ("already_reviewed", "OA / unclear", old, False, "up"),
            ("wrong_tag", "EU A2A", old, False, None),
        ]
        for key, tag, detected_at, manual, review in specs:
            listing = SellerNewListing(
                tracked_seller_id=seller_id, asin=ASINS[key], sourcing_tag=tag,
                currently_buyable=False, detected_at=detected_at,
                manually_classified=manual, review=review, dismissed=False,
            )
            db.add(listing)
            db.commit()
            db.refresh(listing)
            listing_ids[key] = listing.id
    finally:
        db.close()

    archived_count = SellerWatchService.archive_stale_oa_investigate(max_age_days=14)
    assert archived_count >= 1, "must archive at least our one qualifying fixture"
    print(f"test 1: archive_stale_oa_investigate ran, reported {archived_count} archived (>=1 expected): ok")

    db = SessionLocal()
    try:
        rows = {key: db.get(SellerNewListing, lid) for key, lid in listing_ids.items()}

        assert rows["old_untouched"].dismissed is True, "old, never-actioned OA/unclear listing must be archived"
        print("test 2: old untouched OA/unclear listing (20d) archived: ok")

        assert rows["recent"].dismissed is False, "a listing only 5 days old must NOT be archived"
        print("test 3: recent OA/unclear listing (5d) left alone: ok")

        assert rows["manually_classified"].dismissed is False, "a manually-classified listing must NOT be auto-archived"
        print("test 4: manually-classified listing left alone regardless of age: ok")

        assert rows["already_reviewed"].dismissed is False, "an already-reviewed listing must NOT be re-archived"
        print("test 5: already-reviewed listing left alone: ok")

        assert rows["wrong_tag"].dismissed is False, "a non-OA/unclear listing must never be touched by this job"
        print("test 6: EU A2A-tagged listing (out of scope) left alone: ok")
    finally:
        db.close()

    print("\nALL TESTS PASSED.")
finally:
    cleanup()
    db = SessionLocal()
    try:
        remaining = db.query(SellerNewListing).filter(SellerNewListing.asin.in_(ASINS.values())).count()
    finally:
        db.close()
    assert remaining == 0, "cleanup failed -- fake rows still present"
    print("cleanup verified: 0 fake rows remain.")
