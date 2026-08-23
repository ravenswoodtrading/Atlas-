from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import TokenUsageEvent


class TokenUsageService:
    """
    Records and summarizes Keepa token spend (and SP-API-avoided
    spend) for the Settings > Token Usage page -- see TokenUsageEvent
    in app/database/models.py for the full field reasoning.

    record_keepa_spend/record_sp_api_saved are the two entry points
    real call sites use; record() itself is the shared low-level
    insert both funnel through.
    """

    @staticmethod
    def record(category: str, call_type: str, tokens: float,
               marketplace: str = "", asins_count: int = 0):
        if tokens <= 0:
            return

        db = SessionLocal()

        try:
            db.add(TokenUsageEvent(
                category=category or "other",
                call_type=call_type,
                marketplace=marketplace,
                asins_count=asins_count,
                tokens=tokens,
            ))
            db.commit()
        except Exception as exc:
            # A logging failure must never break the real Keepa/SP-API
            # call it's describing -- same convention as ActivityLog.record.
            print(f"TokenUsageService.record failed ({category}/{call_type}): {exc}")
        finally:
            db.close()

    @staticmethod
    def record_keepa_spend(category: str, call_type: str, tokens_before, tokens_after,
                            marketplace: str = "", asins_count: int = 0):
        """
        tokens_before/tokens_after: api.tokens_left read immediately
        before and after the real Keepa call. Either can be None (the
        keepa library hasn't populated tokens_left yet, e.g. before
        the client's first-ever call) -- silently skipped rather than
        logged as 0, since "we couldn't measure this" and "this
        genuinely cost nothing" are different things and only one of
        them is worth a row.
        """
        if tokens_before is None or tokens_after is None:
            return

        spent = max(tokens_before - tokens_after, 0)
        TokenUsageService.record(category, call_type, spent, marketplace, asins_count)

    @staticmethod
    def record_sp_api_saved(category: str, marketplace: str, asins_count: int,
                             estimated_tokens: float):
        TokenUsageService.record(category, "sp_api_saved", estimated_tokens, marketplace, asins_count)

    @staticmethod
    def daily_summary(days: int = 14) -> list:
        """
        [{date: "2026-08-21", category, call_type, tokens}] for the
        last `days` days, oldest first -- one row per (date, category,
        call_type) combination that had any activity. The Token Usage
        page pivots this client-side into a day-by-day stacked view;
        summing here in Python (not SQL date_trunc, which isn't
        portable across SQLite/Postgres) keeps this DB-agnostic like
        the rest of the app's queries.
        """
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

            rows = (
                db.query(TokenUsageEvent)
                .filter(TokenUsageEvent.occurred_at >= cutoff)
                .all()
            )

            buckets = {}

            for row in rows:
                date_key = row.occurred_at.date().isoformat()
                key = (date_key, row.category, row.call_type)
                buckets[key] = buckets.get(key, 0.0) + row.tokens

            return [
                {"date": date_key, "category": category, "call_type": call_type, "tokens": round(tokens, 1)}
                for (date_key, category, call_type), tokens in sorted(buckets.items())
            ]
        finally:
            db.close()

    @staticmethod
    def category_totals(days: int = 30) -> list:
        """
        [{category, tokens}] real Keepa spend only (excludes
        sp_api_saved -- see savings_totals for that), sorted highest
        first -- "what's actually spending my tokens right now" for
        the Token Usage page's breakdown table.
        """
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

            rows = (
                db.query(TokenUsageEvent.category, func.sum(TokenUsageEvent.tokens))
                .filter(TokenUsageEvent.occurred_at >= cutoff)
                .filter(TokenUsageEvent.call_type != "sp_api_saved")
                .group_by(TokenUsageEvent.category)
                .all()
            )

            totals = [{"category": category, "tokens": round(tokens, 1)} for category, tokens in rows]
            totals.sort(key=lambda r: r["tokens"], reverse=True)
            return totals
        finally:
            db.close()

    @staticmethod
    def savings_totals(days: int = 30) -> dict:
        """
        {keepa_spent, sp_api_saved_estimate} over the last `days` days
        -- the Token Usage page's headline numbers. sp_api_saved_estimate
        is exactly that, an estimate (see TokenUsageEvent's docstring),
        never subtracted from keepa_spent -- shown side by side instead
        so it's clear which number is measured and which is inferred.
        """
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

            spent = (
                db.query(func.sum(TokenUsageEvent.tokens))
                .filter(TokenUsageEvent.occurred_at >= cutoff)
                .filter(TokenUsageEvent.call_type != "sp_api_saved")
                .scalar()
            ) or 0.0

            saved = (
                db.query(func.sum(TokenUsageEvent.tokens))
                .filter(TokenUsageEvent.occurred_at >= cutoff)
                .filter(TokenUsageEvent.call_type == "sp_api_saved")
                .scalar()
            ) or 0.0

            return {"keepa_spent": round(spent, 1), "sp_api_saved_estimate": round(saved, 1)}
        finally:
            db.close()
