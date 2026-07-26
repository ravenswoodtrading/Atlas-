from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


class ScoringEngine:

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:

        score = 0

        # Demand
        if trend.demand_improving:
            score += 15

        if product.sales_drops_30d >= 30:
            score += 15

        # Competition
        if trend.competition_improving:
            score += 20

        # Pricing
        if trend.price_stable:
            score += 20

        # Profitability
        if product.roi >= 35:
            score += 20

        if product.profit >= 8:
            score += 20

        # Risks
        if product.hazmat:
            score -= 100

        if product.adult:
            score -= 100

        # Keep score between 0 and 100
        return max(0, min(score, 100))