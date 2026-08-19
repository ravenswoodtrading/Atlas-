from dataclasses import dataclass

from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


@dataclass
class ScoreFactor:
    label: str
    passed: bool
    points: int


class ScoringEngine:

    # When monthly_sales has no confirmed figure at all, sales-RANK
    # drops in the last 30 days (Keepa's salesRankDrops30) stand in as
    # a weaker substitute demand signal -- a rank drop only happens
    # when something actually sold, unlike a PRICE drop (which can
    # happen with zero units sold, e.g. a competitor repricing), so
    # rank drops are the more honest proxy. Deliberately capped well
    # below every confirmed-sales tier above, and always labelled as
    # an estimate, so it can never score as trustworthy as real Amazon
    # data. Same signal (and a lower threshold, 3) that
    # ProductRepository.is_notable already uses as its own "any sales
    # evidence" bar for a different purpose (Review Queue/Discord).
    NO_SALES_DATA_STRONG_RANK_DROPS = 15
    NO_SALES_DATA_WEAK_RANK_DROPS = 5

    @staticmethod
    def _monthly_sales_factor(monthly_sales: int, sales_drops_30d: int = 0) -> ScoreFactor:
        """
        Tiered scoring for Keepa's confirmed monthly sales count --
        this is real Amazon sales data, not an estimate, so it's
        weighted as the single largest factor: a product with strong
        confirmed sales is a fundamentally safer bet than one that
        merely looks good on price/trend but has no proof anyone
        actually buys it.

        When monthly_sales is 0 (no confirmed figure -- see
        Product.monthly_sales' docstring for why that's ambiguous
        rather than proof of no sales), falls back to sales_drops_30d
        as a weaker estimate instead of an automatic flat loss -- see
        the NO_SALES_DATA_* constants above for why rank drops
        specifically, not price drops.
        """
        if monthly_sales >= 50:
            return ScoreFactor(f"Strong confirmed sales ({monthly_sales}/month)", True, 25)
        if monthly_sales >= 20:
            return ScoreFactor(f"Good confirmed sales ({monthly_sales}/month)", True, 18)
        if monthly_sales >= 5:
            return ScoreFactor(f"Some confirmed sales ({monthly_sales}/month)", True, 10)
        if monthly_sales > 0:
            return ScoreFactor(f"Minimal confirmed sales ({monthly_sales}/month)", True, 5)

        if sales_drops_30d >= ScoringEngine.NO_SALES_DATA_STRONG_RANK_DROPS:
            return ScoreFactor(
                f"No confirmed sales figure -- estimated from {sales_drops_30d} rank drops in 30d", True, 12
            )
        if sales_drops_30d >= ScoringEngine.NO_SALES_DATA_WEAK_RANK_DROPS:
            return ScoreFactor(
                f"No confirmed sales figure -- estimated from {sales_drops_30d} rank drops in 30d", True, 6
            )

        return ScoreFactor("No confirmed monthly sales data from Keepa, and no rank-drop evidence either", False, 25)

    @staticmethod
    def _roi_factor(roi: float) -> ScoreFactor:
        """
        Tiered like _monthly_sales_factor -- ROI is one of the two
        biggest single factors (alongside profit) precisely because a
        product can look great on every other signal (demand, sales
        velocity, competition, price stability) while still being a
        bad buy if the margin itself is thin. A flat pass/fail at one
        threshold let weak-ROI products coast to REVIEW on other
        factors alone -- this graduates the reward so low ROI can't
        hide behind everything else looking good.
        """
        if roi >= 50:
            return ScoreFactor(f"Excellent ROI ({roi:.0f}%, best of current/typical)", True, 30)
        if roi >= 35:
            return ScoreFactor(f"Strong ROI ({roi:.0f}%, best of current/typical)", True, 22)
        if roi >= 20:
            return ScoreFactor(f"Decent ROI ({roi:.0f}%, best of current/typical)", True, 14)
        if roi >= 10:
            return ScoreFactor(f"Marginal ROI ({roi:.0f}%, best of current/typical)", True, 6)

        return ScoreFactor(f"Weak ROI ({roi:.0f}%, best of current/typical, below 10%)", False, 30)

    @staticmethod
    def _profit_factor(profit: float) -> ScoreFactor:
        """Tiered like _roi_factor -- see that docstring."""
        if profit >= 15:
            return ScoreFactor(f"Excellent profit (GBP {profit:.2f}, best of current/typical)", True, 20)
        if profit >= 8:
            return ScoreFactor(f"Solid profit (GBP {profit:.2f}, best of current/typical)", True, 14)
        if profit >= 3:
            return ScoreFactor(f"Thin profit (GBP {profit:.2f}, best of current/typical)", True, 6)

        return ScoreFactor(f"No real profit (GBP {profit:.2f}, best of current/typical)", False, 20)

    @staticmethod
    def explain(product: Product, trend: TrendAnalysis):
        # Use whichever is better -- today's price or the 90-day
        # typical price. A temporary Amazon-driven discount on today's
        # price shouldn't sink the score for a product that's normally
        # a good opportunity at its typical price.
        effective_roi = max(product.roi, product.roi_90d)
        effective_profit = max(product.profit, product.profit_90d)

        factors = [
            ScoreFactor("Demand improving (sales rank better than 90d ago)", trend.demand_improving, 10),
            ScoreFactor("High sales velocity (30+ rank drops in 30d)", product.sales_drops_30d >= 30, 10),
            ScoringEngine._monthly_sales_factor(product.monthly_sales, product.sales_drops_30d),
            ScoreFactor("Competition easing (fewer offers than 90d ago)", trend.competition_improving, 15),
            ScoreFactor("Price stable (within 10% of 90d average)", trend.price_stable, 15),
            ScoringEngine._roi_factor(effective_roi),
            ScoringEngine._profit_factor(effective_profit),
        ]

        if product.hazmat:
            factors.append(ScoreFactor("Hazmat penalty", True, -100))

        if product.adult:
            factors.append(ScoreFactor("Adult product penalty", True, -100))

        return factors

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:
        factors = ScoringEngine.explain(product, trend)
        total = sum(f.points for f in factors if f.passed)
        return max(0, min(total, 100))