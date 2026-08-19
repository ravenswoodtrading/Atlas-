import json
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.verdict_service import VerdictService
from app.services.anthropic_client import generate_verdict
from app.services.keepa_priority import KeepaPriority
from app.services.activity_log import ActivityLog

# Small batch per tick -- keeps each background pass short, matching the
# "high priority, near-real-time" spirit of the queue (spec section 5)
# without hogging the Keepa token budget itself.
LEAD_ANALYSIS_BATCH_SIZE = 3

# Caps retries on a permanently-bad ASIN (no Keepa data, or a repeatedly
# failing Claude call) -- see Lead.analysis_attempts in
# app/database/models.py for why this exists instead of retrying forever.
MAX_ANALYSIS_ATTEMPTS = 3


class LeadAnalysisService:

    @staticmethod
    def process_queued_batch(batch_size: int = LEAD_ANALYSIS_BATCH_SIZE):
        db = SessionLocal()

        try:
            leads = (
                db.query(Lead)
                .filter(Lead.status == "queued")
                .order_by(Lead.added_at)
                .limit(batch_size)
                .all()
            )

            for lead in leads:
                LeadAnalysisService._analyze_one(db, lead)

            if leads:
                ActivityLog.record("lead_analysis", f"{len(leads)} lead(s) analyzed")
        finally:
            db.close()

    @staticmethod
    def _analyze_one(db, lead: Lead):
        try:
            with KeepaPriority.high_priority():
                metrics = VerdictService.compute_metrics(lead.asin, cost_price=lead.va_cost_price)

            if metrics is None:
                LeadAnalysisService._record_failure(db, lead, "Keepa has no data for this ASIN.")
                return

            va_financials = None
            if lead.va_roi is not None or lead.va_profit is not None:
                va_financials = {
                    "roi": lead.va_roi,
                    "profit": lead.va_profit,
                    "cost_price": lead.va_cost_price,
                    "sale_price": lead.va_sale_price,
                }

            verdict, rationale = generate_verdict(metrics, va_financials)

            lead.keepa_metrics = json.dumps(metrics)
            lead.verdict = verdict
            lead.rationale = rationale
            lead.status = "analyzed"
            lead.analyzed_at = datetime.now(timezone.utc)
            db.commit()

        except Exception as exc:
            db.rollback()
            print(f"Lead analysis failed for lead {lead.id} ({lead.asin}): {exc}")
            LeadAnalysisService._record_failure(db, lead, f"Analysis error: {exc}")

    @staticmethod
    def _record_failure(db, lead: Lead, reason: str):
        lead.analysis_attempts += 1

        if lead.analysis_attempts >= MAX_ANALYSIS_ATTEMPTS:
            lead.status = "analyzed"
            lead.verdict = None
            lead.rationale = f"Could not analyze after {MAX_ANALYSIS_ATTEMPTS} attempts -- {reason}"
            lead.analyzed_at = datetime.now(timezone.utc)

        db.commit()
