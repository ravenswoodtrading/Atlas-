from dataclasses import dataclass

from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


@dataclass
class ConfidenceFactor:
    label: str
    triggered: bool
    points: int


class ConfidenceEngine:

    @staticmethod
    def explain(product: Product, trend: TrendAnalysis):
        return [
            ConfidenceFactor("Large price swing (over 15%)", abs(trend.price_change) > 15, -30),
            ConfidenceFactor("Competition surging (offers up over 50%)", trend.offer_change > 50, -20),
            ConfidenceFactor("Low sales velocity (under 10 rank drops in 30d)", product.sales_drops_30d < 10, -20),
        ]

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:
        factors = ConfidenceEngine.explain(product, trend)
        total = 100 + sum(f.points for f in factors if f.triggered)
        return max(0, min(total, 100))