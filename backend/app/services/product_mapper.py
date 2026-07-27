from app.models.product import Product
from app.keepa.parser import KeepaParser


class ProductMapper:

    @staticmethod
    def from_keepa(k: dict) -> Product:

        parser = KeepaParser(k)

        return Product(

            # Identity
            asin=k.get("asin", ""),
            title=k.get("title", ""),
            brand=k.get("brand", ""),
            category=str(k.get("rootCategory", "")),

            # Pricing
            buy_box_now=parser.buy_box_now(),
            buy_box_90d=0,

            # Competition
            offers_now=0,
            offers_90d=0,

            # Sales
            sales_rank_now=parser.sales_rank_now(),
            sales_rank_90d=0,
            sales_drops_30d=parser.monthly_sales(),

            # Marketplace Costs
            uk_cost=0,
            fr_cost=0,
            de_cost=0,
            it_cost=0,
            es_cost=0,

            # Amazon Fees
            fba_fee=0,
            referral_fee=0,

            # Financials
            profit=0,
            roi=0,

            # Flags
            hazmat=False,
            adult=False,
        )