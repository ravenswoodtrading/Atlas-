from pprint import pprint


class KeepaInspector:

    @staticmethod
    def inspect(product):

        print("\n==============================")
        print(product.get("asin"))
        print("==============================\n")

        interesting = [
            "title",
            "brand",
            "buyBoxPrice",
            "offerCountNew",
            "monthlySold",
            "salesRanks",
            "fbaFees",
            "csv",
            "stats",
            "data",
            "offers",
            "liveOffersOrder",
        ]

        for key in interesting:
            print(f"\n----- {key} -----")
            pprint(product.get(key))