import json
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.verdict_service import VerdictService
from app.services.anthropic_client import generate_verdict
from app.services.keepa_priority import KeepaPriority
from app.services.activity_log import ActivityLog
from app.services.product_service import KeepaTokensExhaustedError

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
                # source_marketplace is only set on EU A2A sheet rows
                # (see leads.py's _extract_source_marketplace) and costs
                # one extra Keepa call when it is -- the price of
                # catching an FBM-only or Amazon-out-of-stock source
                # here rather than having a human reject it by hand in
                # the review queue. NULL on OA rows, which skips it.
                metrics = VerdictService.compute_metrics(
                    lead.asin,
                    cost_price=lead.va_cost_price,
                    source_marketplace=lead.source_marketplace,
                )

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

            # Gated brands apply to a VA sheet row exactly as they do
            # to a manual check (2026-08-27) -- the stock can't be
            # listed either way, so the verdict shouldn't come back BUY
            # without saying so. Stamped onto metrics as well so the
            # Review Queue can show it. This path still doesn't pass
            # same-ASIN rejection history, which the Verdict Checker
            # does -- a separate gap, left alone here.
            brand_gating = VerdictService.get_brand_gating(
                metrics.get("brand"), metrics.get("category_name")
            )
            metrics["brand_gating"] = brand_gating

            verdict, rationale = generate_verdict(
                metrics, va_financials, brand_gating=brand_gating,
            )

            lead.keepa_metrics = json.dumps(metrics)
            lead.verdict = verdict
            lead.rationale = rationale
            lead.status = "analyzed"
            lead.analyzed_at = datetime.now(timezone.utc)
            db.commit()

        except KeepaTokensExhaustedError as exc:
            # Transient, not this ASIN's fault -- leave the lead
            # "queued" (don't burn one of its MAX_ANALYSIS_ATTEMPTS)
            # so the next batch tick just picks it up again once
            # tokens refill, instead of it eventually going permanently
            # "analyzed"/failed for a reason that had nothing to do
            # with the ASIN itself.
            db.rollback()
            print(f"Lead analysis deferred for lead {lead.id} ({lead.asin}): {exc}")

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
