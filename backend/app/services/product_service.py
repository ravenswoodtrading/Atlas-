from app.keepa.client import get_keepa_client


class ProductService:

    def __init__(self):
        self.api = get_keepa_client()

        # Keepa marketplace codes
        self.domains = {
            "UK": "GB",
            "DE": "DE",
            "FR": "FR",
            "ES": "ES",
            "IT": "IT",
        }

    def get_products(self, asins, marketplace="UK"):

        domain = self.domains.get(marketplace.upper())

        if domain is None:
            raise ValueError(f"Unknown marketplace: {marketplace}")

        return self.api.query(
            items=asins,
            domain=domain,
            history=True,
            offers=20,
            buybox=True,
            progress_bar=False
        )