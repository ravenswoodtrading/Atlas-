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
    def explain(product: Product, trend: TrendAnalysis):
        factors = [
            ScoreFactor("Demand improving (sales rank better than 90d ago)", trend.demand_improving, 15),
            ScoreFactor("High sales velocity (30+ rank drops in 30d)", product.sales_drops_30d >= 30, 15),
            ScoreFactor("Competition easing (fewer offers than 90d ago)", trend.competition_improving, 20),
            ScoreFactor("Price stable (within 10% of 90d average)", trend.price_stable, 20),
            ScoreFactor("Strong ROI (35% or higher)", product.roi >= 35, 20),
            ScoreFactor("Solid profit (GBP 8 or more)", product.profit >= 8, 20),
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