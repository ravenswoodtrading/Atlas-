from dataclasses import dataclass

from app.models.product import Product


@dataclass
class FeeResult:
    referral_fee: float
    fba_fee: float
    profit: float
    roi: float


class FeeEngine:

    DEFAULT_REFERRAL_RATE = 0.15

    # Fallback only -- used when Keepa hasn't returned real FBA fee
    # data for this ASIN (see KeepaParser.fba_fee).
    DEFAULT_FBA_FEE = 3.48

    @staticmethod
    def calculate(product: Product) -> FeeResult:

        referral_fee = round(
            product.buy_box_now * FeeEngine.DEFAULT_REFERRAL_RATE, 2
        )

        fba_fee = product.fba_fee if product.fba_fee else FeeEngine.DEFAULT_FBA_FEE

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

        return FeeResult(
            referral_fee=referral_fee,
            fba_fee=fba_fee,
            profit=profit,
            roi=roi,
        )
