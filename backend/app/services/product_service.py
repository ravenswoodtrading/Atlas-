
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

    def get_products(self, asins, marketplace="UK", retries=1):

        domain = self.domains.get(marketplace.upper())

        if domain is None:
            raise ValueError(f"Unknown marketplace: {marketplace}")

        for attempt in range(retries + 1):
            try:
                return self.api.query(
                    items=asins,
                    domain=domain,
                    history=True,
                    offers=20,
                    buybox=True,
                    stats=90,
                    progress_bar=False
                )
            except Exception as exc:
                if attempt < retries:
                    continue

                # A single slow/failed Keepa response (network timeout,
                # server hiccup) shouldn't crash the whole scan -- treat
                # this marketplace as having no data for this batch,
                # same as if the ASINs simply weren't listed there.
                print(f"Keepa query failed for {marketplace} after {retries + 1} attempt(s): {exc}")
                return []