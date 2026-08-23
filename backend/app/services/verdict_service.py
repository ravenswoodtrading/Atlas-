import json
from dataclasses import replace

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.category_survey_service import get_category_names
from app.services.fee_engine import FeeEngine
from app.keepa.parser import KeepaParser
from app.sp_api.client import get_sp_api_client

# Cap on VerdictService.get_similar_rejections' output -- a verdict
# prompt with 30 past rejections crammed in is worse than one with the
# 5 most relevant, and it's real prompt-token cost either way.
MAX_SIMILAR_REJECTIONS = 5

# Label passed as best_source_marketplace when computing a rough
# Keepa-estimate profit for a verdict check -- not a real EU
# marketplace, so FeeEngine.calculate falls back to the UK VAT rate
# for the cost side, same convention OaLookupService uses for a
# domestic UK cost (see oa_lookup_service.search_candidates). A
# verdict check's supplied cost_price could be OA (domestic) or A2A
# (cross-border EU) -- this is a deliberate, documented approximation
# rather than a precise per-marketplace VAT treatment.
MANUAL_COST_LABEL = "MANUAL"

# Keepa stats windows this app can ask for (see
# ProductService.get_products' stats_days) -- 180 covers all three
# spec-required trend points (30/90/180) in a single request.
STATS_WINDOW_DAYS = 180


class VerdictService:

    @staticmethod
    def get_similar_rejections(asin: str, brand: str | None, category_name: str | None) -> list[dict]:
        """
        Sourcing agent brief step 7 -- past Lead rejections with a
        captured "why not" reason (Lead.decision_reason, added
        2026-08-23 in the reject-flow change) surfaced as context for
        scoring a new, possibly-related candidate.

        Deliberately scoped to Lead only, not ProductRecord/
        SellerNewListing's own review_reason columns -- those belong
        to Discovery/Competitor Watch's separate deterministic pipeline
        (see the brief's "repo split that matters" note), and blending
        their rejection reasons into the Verdict Checker's LLM prompt
        would mix two different review contexts together.

        Matching, highest-signal first, capped at
        MAX_SIMILAR_REJECTIONS total: (1) this exact ASIN rejected
        before -- the strongest possible signal, it's literally this
        product resurfacing; (2) same brand; (3) same category. Brand/
        category aren't their own Lead columns (only inside the JSON
        keepa_metrics blob), so this loads candidate rejected leads
        and filters/ranks in Python rather than querying the JSON blob
        via SQL LIKE -- a manual review queue's rejected-lead volume is
        small enough that this is simpler and more correct than a
        fragile text match.
        """
        db = SessionLocal()
        try:
            rejected = (
                db.query(Lead)
                .filter(
                    Lead.decision == "rejected",
                    Lead.decision_reason.isnot(None),
                    Lead.decision_reason != "",
                )
                .order_by(Lead.reviewed_at.desc())
                .all()
            )
        finally:
            db.close()

        def _metrics(lead: Lead) -> dict:
            if not lead.keepa_metrics:
                return {}
            try:
                return json.loads(lead.keepa_metrics)
            except Exception:
                return {}

        brand_norm = brand.strip().lower() if brand else None
        category_norm = category_name.strip().lower() if category_name else None

        same_asin, same_brand, same_category = [], [], []

        for lead in rejected:
            if lead.asin == asin:
                same_asin.append((lead, _metrics(lead), "same_asin"))
                continue

            m = _metrics(lead)
            lead_brand = (m.get("brand") or "").strip().lower()
            lead_category = (m.get("category_name") or "").strip().lower()

            if brand_norm and lead_brand == brand_norm:
                same_brand.append((lead, m, "same_brand"))
            elif category_norm and lead_category == category_norm:
                same_category.append((lead, m, "same_category"))

        results = []
        for lead, m, match_reason in (same_asin + same_brand + same_category)[:MAX_SIMILAR_REJECTIONS]:
            results.append({
                "asin": lead.asin,
                "brand": m.get("brand"),
                "category_name": m.get("category_name"),
                "decision_reason": lead.decision_reason,
                "reviewed_at": lead.reviewed_at.isoformat() if lead.reviewed_at else None,
                "match_reason": match_reason,
                "same_asin": lead.asin == asin,
            })

        return results

    @staticmethod
    def compute_metrics(asin: str, cost_price: float | None = None, deep_dive: bool = False) -> dict | None:
        """
        Full Keepa-derived metric set for one ASIN (spec section 4:
        profitability, demand/competition, price history/stability).
        Returns None if Keepa has no data for this ASIN.

        cost_price, if given, produces a Keepa-ESTIMATE profit/ROI
        only -- clearly not VA/SAS-verified. Callers that already have
        a VA-supplied profit/ROI on the Lead should not pass a
        cost_price here; that ground truth is applied at the
        route/worker layer and must never be recomputed from Keepa
        (see Lead's docstring in app/database/models.py).

        deep_dive (2026-08-23, sourcing-agent brief section 5): pulls
        the extra evidence a promising lead deserves that a routine
        check doesn't -- Keepa's per-seller stock levels (competitor_
        stock_levels, requesting `stock` on top of the always-on
        `offers`, ~2.4x the already-priciest include_offers cost,
        confirmed live) and a free SP-API getItemOffers live price/
        offer-count cross-check (sp_api_live_check) against Keepa's
        potentially-stale snapshot. Callers should only pass this for a
        lead that already looked promising on a cheap deep_dive=False
        pass, not for every ASIN in a bulk batch -- see
        app/routes/verdict.py's two-pass orchestration.
        """
        service = ProductService()
        products = service.get_products(
            [asin], "UK", full=True, stats_days=STATS_WINDOW_DAYS,
            include_rating=True, include_offers=True, include_stock=deep_dive,
            usage_category="verdict",
        )

        if not products:
            return None

        raw = products[0]
        product = ProductMapper.from_keepa(raw)

        if not product.title:
            return None

        parser = KeepaParser(raw)
        category_names = get_category_names(service.api)
        category_name = category_names.get(product.category, "")

        keepa_estimate_profit = None
        keepa_estimate_roi = None
        keepa_estimate_margin = None
        keepa_estimate_profit_90d = None
        keepa_estimate_roi_90d = None
        keepa_estimate_profit_peak = None
        keepa_estimate_roi_peak = None
        fee_breakdown = None
        viable_days_90d = None

        if cost_price:
            priced_product = replace(
                product,
                best_source_marketplace=MANUAL_COST_LABEL,
                best_source_cost_gbp=cost_price,
            )
            fees = FeeEngine.calculate(priced_product, category_name=category_name)
            keepa_estimate_profit = fees.profit
            keepa_estimate_roi = fees.roi
            keepa_estimate_margin = fees.margin
            keepa_estimate_profit_90d = fees.profit_90d
            keepa_estimate_roi_90d = fees.roi_90d
            # What profit/ROI would look like at the 90-day PEAK UK
            # price instead of today's/average -- for volatile
            # "sawtooth" leads where the average alone would always
            # look unprofitable. Riskier (only real if you can catch
            # stock while the price is actually up there), which is
            # why this is shown as a separate figure, never blended
            # into the primary estimate above. See OpportunityEngine's
            # PEAK_WINDOW recommendation for the same idea applied to
            # scan results.
            keepa_estimate_profit_peak = fees.profit_peak
            keepa_estimate_roi_peak = fees.roi_peak

            # Full breakdown of what went INTO the estimate above --
            # added so a mismatch against SAS or another tool (wrong
            # category, wrong referral rate, a missing fee) can be
            # spotted at a glance instead of guessed at.
            fee_breakdown = {
                "referral_fee": fees.referral_fee,
                "referral_rate_used": fees.referral_rate_used,
                "fba_fee": fees.fba_fee,
                "prep_fee": fees.prep_fee,
                "uk_vat_rate_used": fees.uk_vat_rate_used,
            }

            # Day-by-day, not average-based: reconstructs the actual
            # buy-box price for each of the last 90 days and re-runs
            # the same ROI formula against every one of them, then
            # counts how many days would have cleared a viable ROI.
            # Answers "how many of the last 90 days was this actually
            # a good buy", which buy_box_90d's single average can't --
            # a product that's genuinely profitable on most days but
            # dragged under by a handful of low outliers looks
            # identical to one that's marginal every day if all you
            # have is the average (and vice versa for a mostly-bad
            # product with one high spike).
            daily_prices = parser.daily_buy_box_prices(90)
            priced_days = 0
            days_at_min_roi = 0
            days_at_target_roi = 0

            # 17% mirrors OpportunityEngine.MIN_VIABLE_ROI (the app's
            # absolute floor before a lead is even CONSIDER-worthy,
            # raised from 10% 2026-08-23); FeeEngine.OA_TARGET_ROI_PCT
            # (25%) mirrors is_notable's own ROI bar (what Atlas treats
            # as a strong lead elsewhere) -- both duplicated as
            # literals rather than imported, same reasoning as
            # OpportunityEngine's own PEAK_SALES_DROPS_THRESHOLD: this
            # module already sits below OpportunityEngine/
            # ProductRepository in the dependency graph and shouldn't
            # import back up to them just for one constant.
            MIN_VIABLE_ROI_PCT = 17.0

            for day_price in daily_prices:
                if not day_price:
                    continue

                priced_days += 1
                day_roi = FeeEngine.roi_at_price(
                    day_price, cost_price, category_name, fees.fba_fee, fees.eu_vat_rate_used,
                )

                if day_roi >= MIN_VIABLE_ROI_PCT:
                    days_at_min_roi += 1
                if day_roi >= FeeEngine.OA_TARGET_ROI_PCT:
                    days_at_target_roi += 1

            viable_days_90d = {
                "priced_days": priced_days,
                "days_at_min_roi": days_at_min_roi,
                "days_at_target_roi": days_at_target_roi,
            }

        monthly_sales_as_of = parser.monthly_sales_as_of()

        competitor_stock_levels = None
        sp_api_live_check = None

        if deep_dive:
            competitor_stock_levels = parser.competitor_stock_levels()

            sp_client = get_sp_api_client()
            if sp_client is not None:
                # None (not a dict) means the call itself failed to
                # produce a usable answer -- see SPAPIClient.
                # get_item_offers' own docstring for why that's kept
                # distinct from "no offer" (a real dict with price=None).
                sp_api_live_check = sp_client.get_item_offers(asin, "UK")

        return {
            "deep_dive": deep_dive,
            "competitor_stock_levels": competitor_stock_levels,
            "sp_api_live_check": sp_api_live_check,

            "asin": product.asin,
            "title": product.title,
            "brand": product.brand,
            "category_name": category_name,
            "ean": product.ean,

            # Profitability -- Keepa estimate only (None if no
            # cost_price was supplied); VA/SAS figures, when present,
            # live on the Lead itself and are never derived here.
            "keepa_estimate_profit": keepa_estimate_profit,
            "keepa_estimate_roi": keepa_estimate_roi,
            "keepa_estimate_margin": keepa_estimate_margin,
            "keepa_estimate_profit_90d": keepa_estimate_profit_90d,
            "keepa_estimate_roi_90d": keepa_estimate_roi_90d,
            "keepa_estimate_profit_peak": keepa_estimate_profit_peak,
            "keepa_estimate_roi_peak": keepa_estimate_roi_peak,
            "fee_breakdown": fee_breakdown,
            "viable_days_90d": viable_days_90d,

            # Demand & competition
            "price_avg_30d": parser.price_avg(30),
            "price_avg_90d": parser.price_avg(90),
            "price_avg_180d": parser.price_avg(180),
            "buy_box_percentage": parser.buy_box_percentage(),
            "amazon_buy_box_percentage": parser.amazon_buy_box_percentage(),
            "offers_now": parser.offers_now(),
            "offers_90d_avg": parser.offers_90d(),
            "offers_fba_present": parser.offers_fba_present(),
            "offer_count_fba": parser.offer_count_fba(),
            "offer_trend": parser.offer_trend(),
            "monthly_sales": parser.monthly_sales(),
            "monthly_sales_as_of": monthly_sales_as_of.isoformat() if monthly_sales_as_of else None,
            "sales_drops_30d": parser.sales_drops_30d(),
            "rating": parser.rating(),
            "review_count": parser.review_count(),
            "is_amazon_on_listing": parser.is_amazon_on_listing(),

            # Price history / stability
            "buy_box_now": parser.buy_box_now(),
            "price_drop_count_30d": parser.price_drop_count(30),
            "price_drop_count_90d": parser.price_drop_count(90),
            "price_drop_count_180d": parser.price_drop_count(180),
            "price_min_ever": parser.price_min_ever(),
            "price_min_90d": parser.buy_box_min_90d(),
            "price_max": parser.price_max(),
            "price_max_90d": parser.buy_box_max_90d(),
            "is_out_of_stock": parser.is_out_of_stock(),
        }
