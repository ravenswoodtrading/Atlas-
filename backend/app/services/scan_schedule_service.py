"""Two approved scan cadences, recorded coverage and weekly review decisions."""
from datetime import datetime, timedelta
from threading import Lock

from app.database.database import SessionLocal
from app.database.models import (
    ScanQueueItem, ScanBrandSchedule, ScanCampaignProgress, ScanQueueRun,
    ScanTierReview, ScanReviewWeek,
)

TIERS = {"regular": "Scanning regularly", "less_regular": "Scanning less regularly"}
LESS_REGULAR_MULTIPLIER = 4
_review_lock = Lock()


def schedule_maps(db):
    return ({r.brand: r.tier for r in db.query(ScanBrandSchedule).all()},
            {r.item_id: r for r in db.query(ScanCampaignProgress).all()})


def due_at(item, tier, progress, count):
    # Regular brands retain a turn each rotation. Less regular brands wait
    # four nominal rotations between attempts, including token failures.
    last = progress.last_attempt_at if progress else None
    if last is None:
        return datetime.min
    return last + timedelta(minutes=max(1, count) * (LESS_REGULAR_MULTIPLIER if tier == "less_regular" else 1))


def set_brand_tier(brand, tier):
    if tier not in TIERS:
        raise ValueError("Unknown scan tier")
    with SessionLocal() as db:
        if not db.query(ScanQueueItem).filter_by(brand=brand).first():
            raise LookupError("Brand is no longer queued")
        row = db.get(ScanBrandSchedule, brand)
        if row is None:
            db.add(ScanBrandSchedule(brand=brand, tier=tier))
        else:
            row.tier = tier
        # A direct manual decision supersedes any pending recommendation.
        db.query(ScanTierReview).filter_by(brand=brand, status="pending").update({"status": "superseded"})
        db.commit()


def queue_rows(items, paused=False):
    with SessionLocal() as db:
        tiers, progress = schedule_maps(db)
    grouped = {}
    now = datetime.utcnow()
    for item in items:
        grouped.setdefault(item.brand, []).append(item)
    rows = []
    for brand, campaigns in grouped.items():
        tier = tiers.get(brand, "regular")
        states = [progress.get(i.id) for i in campaigns]
        totals = [p.filtered_count if p else None for p in states]
        # Scoped catalogues may overlap; don't invent a sum across campaigns.
        total = totals[0] if len(campaigns) == 1 else None
        passes = [p.last_full_pass_at if p else None for p in states]
        full = min(passes) if all(passes) else None
        last = max((i.last_run_at for i in campaigns if i.last_run_at), default=None)
        eligible = [(i, p) for i, p in zip(campaigns, states) if i.status not in ("gated", "excluded")]
        next_due = min((due_at(i, tier, p, len(items)) for i, p in eligible), default=None)
        next_label = "Paused" if paused else "Blocked" if not eligible else (
            "Next rotation" if next_due <= now else f"~{max(1, int((next_due-now).total_seconds()/60))} min")
        rows.append(dict(id=campaigns[0].id, brand=brand, tier=tier,
                         tier_label=TIERS[tier], filtered_count=total,
                         last_full_pass_at=full, last_run_at=last, next_scan=next_label,
                         campaigns=campaigns, progress=states))
    return rows


def refresh_weekly_reviews():
    """Persist one evidence snapshot per ISO week. Never applies a change."""
    week = datetime.utcnow().strftime("%G-W%V")
    with _review_lock, SessionLocal() as db:
        if db.get(ScanReviewWeek, week):
            return
        from app.services.attention_engine_service import get_attention_candidates
        candidates = get_attention_candidates(use_cache=False)
        queued = {r.brand for r in db.query(ScanQueueItem).all()}
        tiers, _ = schedule_maps(db)
        db.query(ScanTierReview).filter_by(status="pending").update({"status": "superseded"})
        for candidate in candidates:
            brand = candidate["brand"]
            if brand not in queued or candidate.get("ignored"):
                continue
            current = tiers.get(brand, "regular")
            proposed = {"INCREASE_ATTENTION": "regular", "REDUCE_QUIET": "less_regular"}.get(candidate["lane"])
            if proposed and proposed != current:
                db.add(ScanTierReview(brand=brand, week=week, current_tier=current,
                                     proposed_tier=proposed, reason=candidate["reason"]))
        db.add(ScanReviewWeek(week=week))
        db.commit()


def pending_reviews():
    with SessionLocal() as db:
        return db.query(ScanTierReview).filter_by(status="pending").order_by(ScanTierReview.brand).all()


def decide_review(review_id, approve):
    with _review_lock, SessionLocal() as db:
        review = db.get(ScanTierReview, review_id)
        if review is None or review.status != "pending":
            raise LookupError("This recommendation has already been resolved or replaced")
        row = db.get(ScanBrandSchedule, review.brand)
        current = row.tier if row else "regular"
        queued = db.query(ScanQueueItem).filter_by(brand=review.brand).first()
        if not queued or current != review.current_tier:
            review.status = "superseded"
            db.commit()
            raise LookupError("The queue has changed; this recommendation is no longer current")
        if approve:
            if row is None:
                db.add(ScanBrandSchedule(brand=review.brand, tier=review.proposed_tier))
            else:
                row.tier = review.proposed_tier
        review.status = "approved" if approve else "dismissed"
        db.commit()


def recent_runs(brand):
    with SessionLocal() as db:
        return db.query(ScanQueueRun).filter_by(brand=brand).order_by(ScanQueueRun.id.desc()).limit(12).all()
