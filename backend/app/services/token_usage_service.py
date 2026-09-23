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
    def spent_since(category: str, hours: float = 24) -> float:
        """
        Real Keepa tokens `category` has spent in the last `hours` (rolling), excluding the
        estimated "sp_api_saved" credits. Used to hold the Scan Queue to a daily ceiling. The
        figure is net of refill during each call (tokens_before - tokens_after), so it can read
        slightly low -- fine for a ceiling. Returns 0 on any error rather than blocking a scan.
        """
        db = None
        try:
            db = SessionLocal()
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
            total = (
                db.query(func.coalesce(func.sum(TokenUsageEvent.tokens), 0.0))
                .filter(TokenUsageEvent.category == category,
                        TokenUsageEvent.call_type != "sp_api_saved",
                        TokenUsageEvent.occurred_at > cutoff)
                .scalar()
            )
            return float(total or 0.0)
        except Exception as exc:
            print(f"TokenUsageService.spent_since failed ({category}): {exc}")
            return 0.0
        finally:
            if db is not None:
                db.close()

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
        page pivots this client-side into a day-by-day stacked view.
        Grouped in SQL by func.date (plain date(), valid on SQLite and
        Postgres alike), not date_trunc which isn't portable.
        """
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

            # Summed in SQL (2026-09-21): this used to load every event row in the window as an ORM object and add
            # them up in Python -- 2s on the Token Usage page and growing with every Keepa call.
            day = func.date(TokenUsageEvent.occurred_at)
            rows = (
                db.query(day, TokenUsageEvent.category, TokenUsageEvent.call_type, func.sum(TokenUsageEvent.tokens))
                .filter(TokenUsageEvent.occurred_at >= cutoff)
                .group_by(day, TokenUsageEvent.category, TokenUsageEvent.call_type)
                .order_by(day, TokenUsageEvent.category, TokenUsageEvent.call_type)
                .all()
            )

            return [
                {"date": date_key, "category": category, "call_type": call_type, "tokens": round(tokens or 0.0, 1)}
                for date_key, category, call_type, tokens in rows
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
