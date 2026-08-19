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
            ean=parser.ean(),

            buy_box_now=parser.buy_box_now(),
            buy_box_90d=parser.buy_box_90d(),
            buy_box_min_90d=parser.buy_box_min_90d(),
            buy_box_max_90d=parser.buy_box_max_90d(),

            offers_now=parser.offers_now(),
            offers_90d=parser.offers_90d(),

            sales_rank_now=parser.sales_rank_now(),
            sales_rank_90d=parser.sales_rank_90d(),
            sales_drops_30d=parser.sales_drops_30d(),
            price_drop_count_90d=parser.price_drop_count(90),
            monthly_sales=parser.monthly_sales(),
            monthly_sales_as_of=parser.monthly_sales_as_of(),

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

        A marketplace whose CURRENT buy box isn't Amazon/FBA-fulfilled
        (see KeepaParser.buy_box_is_amazon_fulfilled) is never
        considered here at all -- not as the winning source, not even
        recorded into its raw *_cost field -- because it isn't
        something Atlas can actually buy and reclaim EU VAT on: a
        merchant-fulfilled (FBM) EU listing ships and invoices
        directly from the third-party seller, never from Amazon, so
        there's no Amazon VAT invoice to reclaim against regardless of
        how good the price/spread looks on paper.
        """
        product = ProductMapper.from_keepa(uk_product)

        best_marketplace = ""
        best_cost_gbp = None
        # Raw (non-GBP) 90-day-low per marketplace -- kept alongside the
        # current-price comparison so, once the winning marketplace is
        # known, its OWN historical low can be converted too. Not used
        # to pick the winner -- see from_keepa_multi's docstring/plan:
        # "best current source only", not "best historical dip".
        min_90d_by_marketplace = {}

        for marketplace, eu_product in eu_products.items():
            if not eu_product:
                continue

            eu_parser = KeepaParser(eu_product)
            raw_cost = eu_parser.buy_box_now()

            if not raw_cost:
                continue

            # FBM-won buy box -- not a real, buyable-with-VAT-reclaim
            # A2A source. Skip this marketplace entirely rather than
            # letting it win on price alone (see docstring above).
            if not eu_parser.buy_box_is_amazon_fulfilled():
                continue

            field = MARKETPLACE_COST_FIELD.get(marketplace)
            if field:
                setattr(product, field, raw_cost)

            min_90d_by_marketplace[marketplace] = eu_parser.buy_box_min_90d()

            currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
            cost_gbp = CurrencyService.to_gbp(raw_cost, currency)

            if best_cost_gbp is None or cost_gbp < best_cost_gbp:
                best_cost_gbp = cost_gbp
                best_marketplace = marketplace

        if best_marketplace:
            product.best_source_marketplace = best_marketplace
            product.best_source_cost_gbp = best_cost_gbp

            raw_min_90d = min_90d_by_marketplace.get(best_marketplace)
            if raw_min_90d:
                currency = MARKETPLACE_CURRENCY.get(best_marketplace, "EUR")
                product.best_source_cost_min_90d_gbp = CurrencyService.to_gbp(raw_min_90d, currency)

        return product