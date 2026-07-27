from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper


class BrandScanService:

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def scan(self, brand: str):

        # Step 1 - Find ASINs
        asins = self.finder.find_brand(brand)

        # Keep first 20 while we're developing
        asins = asins[:20]

        # Step 2 - Load products from each marketplace
        uk_products = self.product_service.get_products(asins, "UK")
        de_products = self.product_service.get_products(asins, "DE")
        fr_products = self.product_service.get_products(asins, "FR")
        es_products = self.product_service.get_products(asins, "ES")
        it_products = self.product_service.get_products(asins, "IT")

        # Step 3 - Build lookup dictionaries
        de_lookup = {p.get("asin"): p for p in de_products}
        fr_lookup = {p.get("asin"): p for p in fr_products}
        es_lookup = {p.get("asin"): p for p in es_products}
        it_lookup = {p.get("asin"): p for p in it_products}

        results = []

        # Step 4 - Build response
        for uk in uk_products:

            product = ProductMapper.from_keepa(uk)

            results.append({
                "product": product,
                "de_found": uk.get("asin") in de_lookup,
                "fr_found": uk.get("asin") in fr_lookup,
                "es_found": uk.get("asin") in es_lookup,
                "it_found": uk.get("asin") in it_lookup,
            })

        return {
            "brand": brand,
            "count": len(results),
            "products": results
        }