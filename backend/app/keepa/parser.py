class KeepaParser:

    def __init__(self, product: dict):
        self.product = product

    @staticmethod
    def _last_value(series):
        if not series:
            return 0

        for i in range(len(series) - 1, 0, -2):
            value = series[i]
            if value not in (-1, None):
                return value

        return 0

    def buy_box_now(self) -> float:
        csv = self.product.get("csv")

        if not csv:
            return 0

        value = self._last_value(csv[18])

        return value / 100 if value else 0

    def sales_rank_now(self) -> int:
        ranks = self.product.get("salesRanks") or {}

        if not ranks:
            return 0

        first = next(iter(ranks.values()))

        return self._last_value(first)

    def monthly_sales(self) -> int:
        stats = self.product.get("stats")

        if not stats:
            return 0

        return stats.get("monthlySold") or 0