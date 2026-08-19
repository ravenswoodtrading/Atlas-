from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import ActivityLogEntry, SchedulerStatus


class ActivityLog:
    """
    Thin, generic recorder for "Atlas did a real unit of work" events
    (2026-08-19, for the Dashboard's "Today's activity" section and
    "What's automated next" panel). See ActivityLogEntry/SchedulerStatus
    in app/database/models.py for why this exists instead of inferring
    activity from incidental per-item timestamps scattered across other
    tables -- those can't distinguish "ran and found nothing" from
    "never ran", and can't be summed into a same-day count without
    double-counting or under-counting depending on the table.

    record() is called at the natural completion point of each real
    pipeline (BrandScanService callers, SellerWatchService.run_check,
    ReplenService's check methods, WatchlistService.check_stale,
    SignalService.run_check, OaSourceDiscoveryService.run_batch,
    LeadAnalysisService.process_queued_batch) -- fires for BOTH manual
    and automated triggers, since the Dashboard's "today" counts are
    meant to answer "how much has Atlas done", not just "how much did
    the scheduler do".

    mark_tick() is called ONLY from app/main.py's scheduler loops, at
    the end of a tick that actually ran (or genuinely attempted to) --
    this is what powers "last ran X ago / next due ~Y" per automated
    task, independent of record()'s per-action counts.
    """

    @staticmethod
    def record(activity_type: str, detail: str = ""):
        db = SessionLocal()

        try:
            db.add(ActivityLogEntry(activity_type=activity_type, detail=detail))
            db.commit()
        except Exception as exc:
            # A logging failure must never break the real pipeline work
            # it's describing -- swallow and move on.
            print(f"ActivityLog.record failed ({activity_type}): {exc}")
        finally:
            db.close()

    @staticmethod
    def counts_today() -> dict:
        """
        {activity_type: count} since UTC midnight -- the same "today"
        boundary Products' /products/today page already uses, for
        consistency across the app.
        """
        db = SessionLocal()

        try:
            cutoff = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            )

            rows = (
                db.query(ActivityLogEntry.activity_type, func.count(ActivityLogEntry.id))
                .filter(ActivityLogEntry.occurred_at >= cutoff)
                .group_by(ActivityLogEntry.activity_type)
                .all()
            )

            return {activity_type: count for activity_type, count in rows}
        finally:
            db.close()

    @staticmethod
    def mark_tick(name: str, interval_seconds: int, summary: str = ""):
        db = SessionLocal()

        try:
            status = db.get(SchedulerStatus, name)

            if status is None:
                status = SchedulerStatus(name=name)
                db.add(status)

            status.interval_seconds = interval_seconds
            status.last_tick_at = datetime.now(timezone.utc)
            status.last_summary = summary
            db.commit()
        except Exception as exc:
            print(f"ActivityLog.mark_tick failed ({name}): {exc}")
        finally:
            db.close()

    @staticmethod
    def scheduler_overview() -> list:
        """
        One row per known scheduler for the Dashboard's "What's
        automated next" panel: when it last ran, roughly how often,
        and a rough "next due" estimate (last_tick_at + interval,
        clamped to "due now" if that's already passed by the time
        this is read -- e.g. the app was asleep, or the tick is
        mid-flight). Empty list before the app has ticked even once
        since this feature shipped.
        """
        db = SessionLocal()

        try:
            rows = db.query(SchedulerStatus).all()
            now = datetime.now(timezone.utc)
            overview = []

            for row in rows:
                next_due_at = None
                overdue = False

                if row.last_tick_at:
                    last = row.last_tick_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    next_due_at = last + timedelta(seconds=row.interval_seconds)
                    overdue = next_due_at <= now

                overview.append({
                    "name": row.name,
                    "last_tick_at": row.last_tick_at,
                    "last_summary": row.last_summary,
                    "interval_seconds": row.interval_seconds,
                    "next_due_at": next_due_at,
                    "overdue": overdue,
                })

            return overview
        finally:
            db.close()
