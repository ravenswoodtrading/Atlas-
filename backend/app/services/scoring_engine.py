from dataclasses import dataclass

from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


@dataclass
class ScoreFactor:
    label: str
    passed: bool
    points: int


class ScoringEngine:

    @staticmethod
    def _monthly_sales_factor(monthly_sales: int) -> ScoreFactor:
        """
        Tiered scoring for Keepa's confirmed monthly sales count --
        this is real Amazon sales data, not an estimate, so it's
        weighted as the single largest factor: a product with strong
        confirmed sales is a fundamentally safer bet than one that
        merely looks good on price/trend but has no proof anyone
        actually buys it.
        """
        if monthly_sales >= 50:
            return ScoreFactor(f"Strong confirmed sales ({monthly_sales}/month)", True, 25)
        if monthly_sales >= 20:
            return ScoreFactor(f"Good confirmed sales ({monthly_sales}/month)", True, 18)
        if monthly_sales >= 5:
            return ScoreFactor(f"Some confirmed sales ({monthly_sales}/month)", True, 10)
        if monthly_sales > 0:
            return ScoreFactor(f"Minimal confirmed sales ({monthly_sales}/month)", True, 5)

        return ScoreFactor("No confirmed monthly sales data from Keepa", False, 25)

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
            ScoringEngine._monthly_sales_factor(product.monthly_sales),
            ScoreFactor("Competition easing (fewer offers than 90d ago)", trend.competition_improving, 15),
            ScoreFactor("Price stable (within 10% of 90d average)", trend.price_stable, 15),
            ScoreFactor("Strong ROI (35% or higher, best of current/typical)", effective_roi >= 35, 15),
            ScoreFactor("Solid profit (GBP 8+, best of current/typical)", effective_profit >= 8, 15),
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