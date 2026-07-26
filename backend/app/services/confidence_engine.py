from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


class ConfidenceEngine:

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:

        confidence = 100

        # Large price swings reduce confidence
        if abs(trend.price_change) > 15:
            confidence -= 30

        # Rapid increase in competition
        if trend.offer_change > 50:
            confidence -= 20

        # Low sales velocity
        if product.sales_drops_30d < 10:
            confidence -= 20

        # Keep confidence between 0 and 100
        return max(0, min(confidence, 100))