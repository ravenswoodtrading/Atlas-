from app.keepa.client import get_keepa_client


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str):

        query = {
            "productType": ["0"],
            "brand": [brand.strip().lower()],
            "sort": [
                ["current_SALES", "asc"],
                ["monthlySold", "desc"]
            ],
            "perPage": 100,
            "page": 0
        }

        print("Calling Product Finder...")

        products = self.api.product_finder(query)

        print("Type:", type(products))
        print("Count:", len(products))

        return products