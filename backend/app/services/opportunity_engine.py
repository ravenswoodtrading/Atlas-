from dataclasses import dataclass

from app.models.product import Product

from app.services.trend_engine import TrendEngine, TrendAnalysis
from app.services.scoring_engine import ScoringEngine
from app.services.confidence_engine import ConfidenceEngine


@dataclass
class OpportunityReport:
    asin: str
    title: str
    brand: str

    score: int
    confidence: int

    recommendation: str

    trend: TrendAnalysis
    score_breakdown: list
    confidence_breakdown: list

    # Only meaningful when recommendation == "PEAK_WINDOW" -- the
    # profit/ROI/price Atlas would see if stock could be bought and
    # sold during Amazon's recent 90-day price PEAK rather than
    # today's price or the 90-day average. Left at Product's own
    # (0.0-default) peak fields for every other recommendation.
    peak_profit: float = 0.0
    peak_roi: float = 0.0
    peak_price: float = 0.0

    # How many of the last 90 days were actually profitable at today's
    # EU source cost -- the real evidence behind a PEAK_WINDOW call
    # (see PEAK_MIN_VIABLE_DAYS_90D below). Carried through to the
    # Review Queue's "why" panel so a PEAK lead's recurrence can be
    # seen, not just trusted.
    peak_viable_days_90d: int = 0

    # Profit as a % of sale price (as opposed to ROI, profit as a % of
    # cost) -- see Product.margin's docstring. Whichever of
    # today's/90d-average price ROI was calculated from.
    margin: float = 0.0
    margin_90d: float = 0.0

    # Same, at the 90-day PEAK price -- mirrors peak_profit/peak_roi
    # above. Only meaningful when recommendation == "PEAK_WINDOW", same
    # convention as those two fields.
    margin_peak: float = 0.0


class OpportunityEngine:

    # A hard floor on ROI, below which a product can never be
    # recommended regardless of how good every other signal looks --
    # otherwise strong demand/velocity/competition/price-stability
    # factors alone (75 of the old 105 achievable points) could carry
    # a product to REVIEW even at a near-zero or barely-positive ROI.
    # 17% is a deliberately conservative "not even worth the risk"
    # line, not a "good" ROI -- ScoringEngine's tiered ROI factor is
    # what actually rewards genuinely strong margins. Raised from 10%
    # to 17% -- user's own explicit floor ("I will never buy a product
    # that low"), 2026-08-23.
    MIN_VIABLE_ROI = 17

    # Two more AND-conditions alongside MIN_VIABLE_ROI, confirmed by
    # the user 2026-08-23: a lead needs 13%+ margin (profit as a % of
    # the GROSS sale price -- see FeeResult.margin's docstring for why
    # this is a different, always-lower number than ROI) AND £2+
    # absolute profit per unit, on top of clearing MIN_VIABLE_ROI, to
    # be viable at all. Margin catches a high-ROI product whose sale
    # price barely covers its own fees; the £ floor catches a
    # high-margin product whose absolute profit is still pennies (a
    # cheap item can clear both ROI and margin bars on tiny numbers).
    MIN_VIABLE_MARGIN_PCT = 13
    MIN_VIABLE_PROFIT_GBP = 2

    # PEAK_WINDOW requires the product to have actually BEEN at a
    # profitable price on at least this many of the last 90 days
    # (see Product.peak_viable_days_90d / SourcingClassifier.
    # compute_peak_window_evidence) before a high buy_box_max_90d is
    # trusted as a genuinely repeating pattern, rather than a single
    # spike. Replaces an earlier version of this gate (2026-08-20)
    # that counted ANY downward price move in 90 days
    # (price_drop_count_90d) as "volatility evidence" -- that counted
    # pure noise (a product bouncing between low, unprofitable prices
    # racks up plenty of "drops" unrelated to the peak) and let
    # single-spike leads through, per direct user report ("one price
    # spike doesn't mean this is potentially profitable"). First-pass
    # figure -- more than the bare minimum of 1 (a single lucky day),
    # but low enough that a genuinely recurring window (even a short
    # one, or two separate short windows across the quarter) still
    # clears it; revisit once real PEAK_WINDOW volume post-fix is
    # visible.
    PEAK_MIN_VIABLE_DAYS_90D = 5

    # PEAK_WINDOW also requires real evidence the product actually
    # SELLS -- confirmed monthly sales, or (absent that) at least this
    # many sales-rank drops in 30 days as a proxy. Deliberately mirrors
    # ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD/is_notable's own
    # bar rather than importing it (product_repository is the
    # persistence layer and depends on this module, not the other way
    # round) -- without this, a product with zero sales evidence could
    # still reach the Review Queue purely on a volatile price history,
    # which is exactly the "low scored, no sales" leak this guards
    # against.
    PEAK_SALES_DROPS_THRESHOLD = 3

    @staticmethod
    def analyse(product: Product) -> OpportunityReport:

        # Analyse trends
        trend = TrendEngine.calculate(product)

        # Calculate score
        score = ScoringEngine.score(product, trend)
        score_breakdown = ScoringEngine.explain(product, trend)

        # Calculate confidence
        confidence = ConfidenceEngine.score(product, trend)
        confidence_breakdown = ConfidenceEngine.explain(product, trend)

        # Use whichever is better -- today's or the 90-day typical --
        # same convention ScoringEngine and the ceiling check elsewhere
        # already use, so a temporary discount doesn't unfairly sink a
        # normally-viable product.
        effective_roi = max(product.roi, product.roi_90d)
        effective_profit = max(product.profit, product.profit_90d)
        effective_margin = max(product.margin, product.margin_90d)

        # Recommendation
        if product.gated:
            # Gated brand (see Product.gated / app/config/exclusions.py's
            # is_gated) -- can't currently be sold regardless of how
            # good the opportunity looks, so this always wins over
            # every other tier below. Score/confidence/trend are still
            # computed above as normal so the Gated Brand Opportunities
            # page can show a genuine ROI/score for building an
            # ungating case -- this only overrides the final
            # recommendation, never counts as BUY/CONSIDER/PEAK_WINDOW
            # anywhere else (see ProductRepository.is_notable).
            recommendation = "GATED"

        elif (
            effective_profit < OpportunityEngine.MIN_VIABLE_PROFIT_GBP
            or effective_roi < OpportunityEngine.MIN_VIABLE_ROI
            or effective_margin < OpportunityEngine.MIN_VIABLE_MARGIN_PCT
        ):
            # Not viable at today's price OR the 90-day average --
            # "viable" now means ALL THREE of ROI/margin/absolute
            # profit clear their own floor (see MIN_VIABLE_ROI/
            # MIN_VIABLE_MARGIN_PCT/MIN_VIABLE_PROFIT_GBP above), not
            # just profit being positive. But before writing it off
            # entirely, check whether it would be viable at the recent
            # 90-day PEAK price, with real evidence (repeated price
            # drops) that the peak actually recurs rather than being a
            # one-off spike. This is a deliberately separate,
            # lower-trust tier -- PEAK_WINDOW never counts as
            # BUY/CONSIDER anywhere else in the app
            # (ProductRepository.is_notable's roi/roi_90d checks don't
            # see this figure, so it's never auto-pinged to Discord at
            # the same trust level as a real BUY).
            has_sales_evidence = (
                product.monthly_sales > 0
                or product.sales_drops_30d >= OpportunityEngine.PEAK_SALES_DROPS_THRESHOLD
            )

            if (
                product.profit_peak >= OpportunityEngine.MIN_VIABLE_PROFIT_GBP
                and product.roi_peak >= OpportunityEngine.MIN_VIABLE_ROI
                and product.margin_peak >= OpportunityEngine.MIN_VIABLE_MARGIN_PCT
                and product.peak_viable_days_90d >= OpportunityEngine.PEAK_MIN_VIABLE_DAYS_90D
                and has_sales_evidence
            ):
                recommendation = "PEAK_WINDOW"
            else:
                recommendation = "IGNORE"

        elif score >= 85 and confidence >= 80:
            recommendation = "BUY"

        elif score >= 65 and confidence >= 60:
            # Named CONSIDER, not REVIEW -- "review" is a separate
            # concept in this app (the thumbs up/down marking whether
            # YOU'VE looked at a result), and using the same word for
            # both was confusing.
            recommendation = "CONSIDER"

        else:
            recommendation = "IGNORE"

        return OpportunityReport(
            asin=product.asin,
            title=product.title,
            brand=product.brand,
            score=score,
            confidence=confidence,
            recommendation=recommendation,
            trend=trend,
            score_breakdown=score_breakdown,
            confidence_breakdown=confidence_breakdown,
            peak_profit=product.profit_peak,
            peak_roi=product.roi_peak,
            peak_price=product.buy_box_max_90d,
            peak_viable_days_90d=product.peak_viable_days_90d,
            margin=product.margin,
            margin_90d=product.margin_90d,
            margin_peak=product.margin_peak,
        )