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
        price_swing_triggered = abs(trend.price_change) > 15
        competition_triggered = trend.offer_change > 50
        low_velocity_triggered = product.sales_drops_30d < 10

        if price_swing_triggered:
            price_label = f"Large price swing ({trend.price_change:+.1f}%, exceeds ±15%)"
        else:
            price_label = f"Price stable ({trend.price_change:+.1f}%, within ±15%)"

        if competition_triggered:
            competition_label = f"Competition surging (offers {trend.offer_change:+.1f}%, exceeds +50%)"
        else:
            competition_label = f"Competition stable (offers {trend.offer_change:+.1f}%, within +50%)"

        if low_velocity_triggered:
            velocity_label = f"Low sales velocity ({product.sales_drops_30d} rank drops in 30d, under 10)"
        else:
            velocity_label = f"Sufficient sales velocity ({product.sales_drops_30d} rank drops in 30d, 10+)"

        return [
            ConfidenceFactor(price_label, price_swing_triggered, -30),
            ConfidenceFactor(competition_label, competition_triggered, -20),
            ConfidenceFactor(velocity_label, low_velocity_triggered, -20),
        ]

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:
        factors = ConfidenceEngine.explain(product, trend)
        total = 100 + sum(f.points for f in factors if f.triggered)
        return max(0, min(total, 100))