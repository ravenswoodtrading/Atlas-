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

    def get_products(self, asins, marketplace="UK", retries=1, full=True):
        """
        `full=True` (used for UK) requests offers + 90-day stats too --
        needed for trend/scoring. `full=False` (used for EU price
        checks) drops both, since EU data is only ever used for its
        current price (see ProductMapper.from_keepa_multi) -- offers
        and stats would just be fetched and thrown away, at real
        token cost for no benefit.
        """

        domain = self.domains.get(marketplace.upper())

        if domain is None:
            raise ValueError(f"Unknown marketplace: {marketplace}")

        query_kwargs = dict(
            items=asins,
            domain=domain,
            history=True,
            buybox=True,
            progress_bar=False,
        )

        if full:
            query_kwargs["offers"] = 20
            query_kwargs["stats"] = 90

        for attempt in range(retries + 1):
            try:
                return self.api.query(**query_kwargs)
            except Exception as exc:
                if attempt < retries:
                    continue

                # A single slow/failed Keepa response (network timeout,
                # server hiccup) shouldn't crash the whole scan -- treat
                # this marketplace as having no data for this batch,
                # same as if the ASINs simply weren't listed there.
                print(f"Keepa query failed for {marketplace} after {retries + 1} attempt(s): {exc}")
                return []