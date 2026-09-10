import json
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.verdict_service import VerdictService
from app.services.anthropic_client import generate_verdict
from app.services.keepa_priority import KeepaPriority
from app.services.activity_log import ActivityLog
from app.services.product_service import KeepaTokensExhaustedError
from app.sp_api.client import get_sp_api_client

# Small batch per tick -- keeps each background pass short, matching the
# "high priority, near-real-time" spirit of the queue (spec section 5)
# without hogging the Keepa token budget itself.
LEAD_ANALYSIS_BATCH_SIZE = 3

# Caps retries on a permanently-bad ASIN (no Keepa data, or a repeatedly
# failing Claude call) -- see Lead.analysis_attempts in
# app/database/models.py for why this exists instead of retrying forever.
MAX_ANALYSIS_ATTEMPTS = 3

# "Substantial stock" floor for the already-in-inventory flag (Review
# Queue backend build, 2026-09-03) -- ANY positive fulfillable quantity
# counts. This is a fact check (do we hold real stock right now?), not
# a judgment threshold like a profitability bar -- so it's a plain
# "> 0", not a number invented to tune sensitivity.
INVENTORY_FLAG_MIN_UNITS = 1


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

            if not leads:
                return

            # Fetched ONCE per batch tick, not once per lead -- SP-API,
            # not Keepa, so this doesn't touch the Keepa token budget at
            # all, but there's no reason to make 3 separate full
            # inventory pulls for one 30s tick's worth of leads either.
            inventory_by_asin = LeadAnalysisService._fetch_inventory_snapshot()

            for lead in leads:
                LeadAnalysisService._analyze_one(db, lead, inventory_by_asin)

            ActivityLog.record("lead_analysis", f"{len(leads)} lead(s) analyzed")
        finally:
            db.close()

    @staticmethod
    def _fetch_inventory_snapshot() -> dict:
        """
        Current FBA inventory keyed by ASIN (collapsing multiple SKUs
        for the same ASIN to whichever has the higher fulfillable
        count) -- see SPAPIClient.get_inventory_summaries for the raw
        per-SKU shape this is built from. SP-API only, zero Keepa cost.

        Returns {} (never None/raises) on anything short of a full
        success -- unconfigured SP-API, a failed call, or an empty
        catalog -- so a lead is simply not flagged rather than the
        whole batch failing because inventory couldn't be checked.
        """
        sp_client = get_sp_api_client()
        if not sp_client:
            return {}

        try:
            summaries = sp_client.get_inventory_summaries()
        except Exception as exc:
            print(f"Inventory snapshot fetch failed: {exc}")
            return {}

        if not summaries:
            return {}

        by_asin: dict[str, dict] = {}
        for info in summaries.values():
            asin = info.get("asin")
            if not asin:
                continue

            existing = by_asin.get(asin)
            fulfillable = info.get("fulfillable") or 0
            if existing is None or fulfillable > (existing.get("fulfillable") or 0):
                by_asin[asin] = info

        return by_asin

    @staticmethod
    def _analyze_one(db, lead: Lead, inventory_by_asin: dict | None = None):
        inventory_by_asin = inventory_by_asin or {}
        lead._analysis_source_payload = lead.raw_sheet_data

        try:
            with KeepaPriority.high_priority():
                # source_marketplace is only set on EU A2A sheet rows
                # (see leads.py's _extract_source_marketplace) and costs
                # one extra Keepa call when it is -- the price of
                # catching an FBM-only or Amazon-out-of-stock source
                # here rather than having a human reject it by hand in
                # the review queue. NULL on OA rows, which skips it.
                # (compute_metrics also derives a sourcing_classification
                # -- EU A2A/UK A2A/Wholesale/OA -- from data this same
                # call already fetches; see its own docstring for why
                # that costs nothing extra.)
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
            # Review Queue can show it.
            brand_gating = VerdictService.get_brand_gating(
                metrics.get("brand"), metrics.get("category_name")
            )
            metrics["brand_gating"] = brand_gating

            # Rejection history (2026-09-03, Review Queue backend build)
            # -- this exact ASIN's own past Lead rejections, same DB-only
            # lookup the manual Verdict Checker already uses (see
            # routes/verdict.py). Previously NOT passed on the automated
            # sheet path -- confirmed as a real gap in the audit. Zero
            # Keepa cost; a pure query against already-stored Lead rows.
            similar_rejections = VerdictService.get_similar_rejections(lead.asin)
            metrics["similar_rejections"] = similar_rejections

            # Already-in-inventory (2026-09-03, Review Queue backend
            # build) -- reuses the SP-API inventory snapshot fetched
            # once for the whole batch (see process_queued_batch).
            # Recorded as a FACT on the lead, not an auto-reject: the
            # Review Queue's priority layer decides what to do with it
            # (see ReviewQueueService), matching the explicit instruction
            # not to reject solely because stock exists.
            inventory_match = inventory_by_asin.get(lead.asin)
            metrics["already_in_inventory"] = bool(
                inventory_match and (inventory_match.get("fulfillable") or 0) >= INVENTORY_FLAG_MIN_UNITS
            )
            metrics["inventory_detail"] = inventory_match

            # Real bug found live, 2026-09-07 (Tamara: "when would Atlas
            # check the price here as it says unchecked"): keepa_metrics
            # used to only get saved AFTER generate_verdict succeeded, so
            # a Claude+Gemini double failure (both were down/quota-
            # exhausted at once) threw away a perfectly good, already-
            # paid-for Keepa fetch -- the lead showed "not yet analyzed"
            # with no photo/profit forever, even though Atlas HAD
            # genuinely checked the price, three times, and just
            # couldn't get an AI verdict on top of it. Committing the
            # Keepa data here, before the AI call, means a verdict
            # failure below only costs the verdict -- not the real
            # price/photo/ROI data this lead already earned.
            if not LeadAnalysisService._save_if_current(db, lead, {
                'keepa_metrics': json.dumps(metrics), 'analyzed_at': datetime.now(timezone.utc)
            }):
                return

            try:
                verdict, rationale = generate_verdict(
                    metrics, va_financials, similar_rejections=similar_rejections, brand_gating=brand_gating,
                )
            except Exception as exc:
                print(f"Lead verdict generation failed for lead {lead.id} ({lead.asin}): {exc}")
                LeadAnalysisService._record_failure(db, lead, f"Analysis error: {exc}")
                return

            LeadAnalysisService._save_if_current(db, lead, {
                'verdict': verdict, 'rationale': rationale, 'status': 'analyzed'
            })

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
    def _save_if_current(db, lead, values):
        # An edit or decision arriving during a network call wins over this
        # worker. Compare-and-update atomically so old analysis cannot return.
        source = getattr(lead, '_analysis_source_payload', lead.raw_sheet_data)
        updated = db.query(Lead).filter(Lead.id == lead.id,
            Lead.raw_sheet_data == source, Lead.decision.is_(None)).update(values, synchronize_session=False)
        db.commit()
        db.expire(lead)
        return bool(updated)

    @staticmethod
    def _record_failure(db, lead: Lead, reason: str):
        attempts = lead.analysis_attempts + 1
        values = {'analysis_attempts': attempts}
        if attempts >= MAX_ANALYSIS_ATTEMPTS:
            values.update(status='analyzed', verdict=None,
                rationale=f'Could not analyze after {MAX_ANALYSIS_ATTEMPTS} attempts -- {reason}',
                analyzed_at=datetime.now(timezone.utc))
        LeadAnalysisService._save_if_current(db, lead, values)
