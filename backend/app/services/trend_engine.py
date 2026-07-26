from dataclasses import dataclass

from app.models.product import Product


@dataclass
class TrendAnalysis:
    price_change: float
    offer_change: float
    rank_change: float

    competition_improving: bool
    demand_improving: bool
    price_stable: bool


class TrendEngine:

    @staticmethod
    def calculate(product: Product) -> TrendAnalysis:

        # Price movement (%)
        if product.buy_box_90d:
            price_change = (
                (product.buy_box_now - product.buy_box_90d)
                / product.buy_box_90d
            ) * 100
        else:
            price_change = 0

        # Competition movement (%)
        if product.offers_90d:
            offer_change = (
                (product.offers_now - product.offers_90d)
                / product.offers_90d
            ) * 100
        else:
            offer_change = 0

        # Sales rank improvement (%)
        if product.sales_rank_90d:
            rank_change = (
                (product.sales_rank_90d - product.sales_rank_now)
                / product.sales_rank_90d
            ) * 100
        else:
            rank_change = 0

        return TrendAnalysis(
            price_change=round(price_change, 2),
            offer_change=round(offer_change, 2),
            rank_change=round(rank_change, 2),
            competition_improving=offer_change < 0,
            demand_improving=rank_change > 0,
            price_stable=abs(price_change) < 10,
        )