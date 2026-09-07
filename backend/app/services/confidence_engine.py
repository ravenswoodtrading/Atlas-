from dataclasses import dataclass

from app.models.product import Product
from app.services.trend_engine import TrendAnalysis


@dataclass
class ConfidenceFactor:
    label: str
    triggered: bool
    points: int


class ConfidenceEngine:
    """
    Opportunity Engine 2.0 (2026-09-04): price-swing and competition-
    surge used to live here as -30/-20 "confidence" penalties. They
    were never actually evidence-quality signals -- they measure
    whether the economics might DETERIORATE, not whether the data
    itself is trustworthy, and conflating the two meant a real,
    well-evidenced opportunity (e.g. DDR RAM whose UK price genuinely
    crashed) read as "low confidence" identically to a product with
    zero sales data at all. Both moved to OpportunityLensService as
    RISK signals instead (see that module -- same 15%/50% thresholds,
    unchanged, just relocated and correctly labelled). Confidence here
    is now purely about evidence quality: how much real sales/velocity
    data backs this number, which is the one thing left in this file.
    """

    @staticmethod
    def explain(product: Product, trend: TrendAnalysis):
        low_velocity_triggered = product.sales_drops_30d < 10

        if low_velocity_triggered:
            velocity_label = f"Low sales velocity ({product.sales_drops_30d} rank drops in 30d, under 10)"
        else:
            velocity_label = f"Sufficient sales velocity ({product.sales_drops_30d} rank drops in 30d, 10+)"

        return [
            ConfidenceFactor(velocity_label, low_velocity_triggered, -20),
        ]

    @staticmethod
    def score(product: Product, trend: TrendAnalysis) -> int:
        factors = ConfidenceEngine.explain(product, trend)
        total = 100 + sum(f.points for f in factors if f.triggered)
        return max(0, min(total, 100))