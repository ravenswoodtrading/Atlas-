from app.models.product import Product
from app.keepa.parser import KeepaParser
from app.services.currency_service import CurrencyService


# Keepa marketplace domain codes -> the currency that marketplace sells in
MARKETPLACE_CURRENCY = {
    "UK": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "ES": "EUR",
    "IT": "EUR",
}

# Product field to write each marketplace's raw (non-GBP) cost into
MARKETPLACE_COST_FIELD = {
    "DE": "de_cost",
    "FR": "fr_cost",
    "ES": "es_cost",
    "IT": "it_cost",
}


class ProductMapper:

    @staticmethod
    def from_keepa(k: dict) -> Product:
        """
        Maps a single (UK-only) Keepa product into a Product, with no
        cross-marketplace cost data. Kept for callers that only need
        UK-side stats (e.g. /debug/{brand}).
        """
        parser = KeepaParser(k)

        return Product(
            asin=k.get("asin") or "",
            title=k.get("title") or "",
            brand=k.get("brand") or "",
            category=str(k.get("rootCategory", "")),

            buy_box_now=parser.buy_box_now(),
            buy_box_90d=parser.buy_box_90d(),

            offers_now=parser.offers_now(),
            offers_90d=parser.offers_90d(),

            sales_rank_now=parser.sales_rank_now(),
            sales_rank_90d=parser.sales_rank_90d(),
            sales_drops_30d=parser.sales_drops_30d(),
            monthly_sales=parser.monthly_sales(),

            fba_fee=parser.fba_fee(),

            hazmat=parser.is_hazmat(),
            adult=False,
        )

    @staticmethod
    def from_keepa_multi(uk_product: dict, eu_products: dict) -> Product:
        """
        Builds a Product using UK data for selling-side stats, plus
        buy-side cost data pulled from each EU marketplace so we can
        find the cheapest A2A source.

        eu_products: {"DE": keepa_dict_or_None, "FR": ..., "ES": ..., "IT": ...}
        Pass None for a marketplace where the ASIN wasn't found there.
        """
        product = ProductMapper.from_keepa(uk_product)

        best_marketplace = ""
        best_cost_gbp = None

        for marketplace, eu_product in eu_products.items():
            if not eu_product:
                continue

            eu_parser = KeepaParser(eu_product)
            raw_cost = eu_parser.buy_box_now()

            if not raw_cost:
                continue

            field = MARKETPLACE_COST_FIELD.get(marketplace)
            if field:
                setattr(product, field, raw_cost)

            currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
            cost_gbp = CurrencyService.to_gbp(raw_cost, currency)

            if best_cost_gbp is None or cost_gbp < best_cost_gbp:
                best_cost_gbp = cost_gbp
                best_marketplace = marketplace

        if best_marketplace:
            product.best_source_marketplace = best_marketplace
            product.best_source_cost_gbp = best_cost_gbp

        return product