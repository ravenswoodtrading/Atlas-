from dataclasses import dataclass

from app.models.product import Product


@dataclass
class FeeResult:
    referral_fee: float
    fba_fee: float
    profit: float
    roi: float
    profit_90d: float
    roi_90d: float


class FeeEngine:

    DEFAULT_REFERRAL_RATE = 0.15

    # Fallback only -- used when Keepa hasn't returned real FBA fee
    # data for this ASIN (see KeepaParser.fba_fee).
    DEFAULT_FBA_FEE = 3.48

    @staticmethod
    def calculate(product: Product) -> FeeResult:

        fba_fee = product.fba_fee if product.fba_fee else FeeEngine.DEFAULT_FBA_FEE

        # Today's price
        referral_fee = round(
            product.buy_box_now * FeeEngine.DEFAULT_REFERRAL_RATE, 2
        )

        profit = 0.0
        roi = 0.0

        if product.best_source_cost_gbp:
            profit = round(
                product.buy_box_now
                - fba_fee
                - referral_fee
                - product.best_source_cost_gbp,
                2,
            )
            roi = round(
                (profit / product.best_source_cost_gbp) * 100, 2
            ) if product.best_source_cost_gbp else 0.0

        # 90-day typical price -- catches opportunities where today's
        # price is temporarily discounted but the product is normally
        # profitable. Referral fee is recalculated too, since it's a
        # percentage of whichever sale price is being used.
        profit_90d = 0.0
        roi_90d = 0.0

        if product.best_source_cost_gbp and product.buy_box_90d:
            referral_fee_90d = round(
                product.buy_box_90d * FeeEngine.DEFAULT_REFERRAL_RATE, 2
            )
            profit_90d = round(
                product.buy_box_90d
                - fba_fee
                - referral_fee_90d
                - product.best_source_cost_gbp,
                2,
            )
            roi_90d = round(
                (profit_90d / product.best_source_cost_gbp) * 100, 2
            )

        return FeeResult(
            referral_fee=referral_fee,
            fba_fee=fba_fee,
            profit=profit,
            roi=roi,
            profit_90d=profit_90d,
            roi_90d=roi_90d,
        )