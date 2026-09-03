from app.models.product import Product
from app.keepa.parser import KeepaParser
from app.services.currency_service import CurrencyService


# UK customs/import-duty cap on a single EU A2A source cost (2026-08-29,
# Tamara's own instruction: "we have to limit EU A2A leads for under the
# import duty"). HMRC's actual current duty-relief line for a commercial
# consignment entering the UK is GBP 135 (Low Value Consignment relief --
# confirmed live via gov.uk's Nov 2025 "Reforming the customs treatment of
# low value imports" proposal; due to be WITHDRAWN entirely by Oct 2028 at
# the latest, not raised -- https://assets.publishing.service.gov.uk/media/
# 692576c7aca6213a492dcfda/FINAL_-_Reforming_the_customs_treatment_of_low_
# value_imports_into_the_United_Kingdom.pdf). Tamara deliberately chose GBP
# 175 here, above that real GBP 135 line, as her own conservative buffer --
# not a claim that 175 is HMRC's actual threshold. This is a business-
# policy cap, not tax advice; re-verify against HMRC's current rules if the
# underlying £135 relief changes or is withdrawn.
#
# Also note: this checks each UNIT's price alone. HMRC's relief is actually
# assessed per CONSIGNMENT (the whole shipment) -- ordering multiple units
# of an under-cap item in one order can still push the total shipment value
# over the real duty line even though every individual unit passed this
# check. Treat this as a per-unit guardrail, not a guarantee that a
# multi-unit order stays duty-free.
EU_A2A_DUTY_CAP_GBP = 175.0


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

        Same treatment for a marketplace priced above EU_A2A_DUTY_CAP_GBP
        (see that constant's docstring) -- excluded entirely, not just
        passed over for the cheaper option, so an over-cap EU price can
        never surface anywhere downstream (ROI/profit, EU A2A tagging,
        Discord, Watchlist auto-add, OA candidate pool) as a real,
        buyable source.
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

            currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
            cost_gbp = CurrencyService.to_gbp(raw_cost, currency)

            # Above the UK import-duty cap -- treated exactly like an
            # FBM listing above: not recorded into its raw *_cost
            # field, never a candidate for best_source_marketplace
            # (see EU_A2A_DUTY_CAP_GBP's docstring).
            if cost_gbp > EU_A2A_DUTY_CAP_GBP:
                continue

            field = MARKETPLACE_COST_FIELD.get(marketplace)
            if field:
                setattr(product, field, raw_cost)

            min_90d_by_marketplace[marketplace] = eu_parser.buy_box_min_90d()

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