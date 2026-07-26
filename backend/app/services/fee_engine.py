from dataclasses import dataclass

from app.models.product import Product


@dataclass
class FeeResult:
    referral_fee: float
    fba_fee: float


class FeeEngine:

    DEFAULT_REFERRAL_RATE = 0.15

    # Temporary FBA fee until we implement Amazon's full fee tables
    DEFAULT_FBA_FEE = 3.48

    @staticmethod
    def calculate(product: Product) -> FeeResult:

        referral_fee = (
            product.buy_box_now *
            FeeEngine.DEFAULT_REFERRAL_RATE
        )

        return FeeResult(
            referral_fee=round(referral_fee, 2),
            fba_fee=FeeEngine.DEFAULT_FBA_FEE
        )