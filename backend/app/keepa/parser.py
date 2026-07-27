class KeepaParser:
    """
    Parses a raw Keepa product dict into the flat values Atlas needs.

    Keepa csv index reference (the parts we use):
        3  = SALES        (sales rank history)
        11 = COUNT_NEW     (new offer count history)
        18 = BUY_BOX_SHIPPING (buy box price history, in cents)

    `stats.avg90` (when present) is a parallel array using the SAME
    index positions as `csv`, holding the 90-day rolling average for
    each series. NOTE: this hasn't been verified against a live Keepa
    response yet -- confirm the avg90 indices line up once real data
    is flowing (see KeepaInspector).
    """

    CSV_SALES_RANK = 3
    CSV_OFFER_COUNT_NEW = 11
    CSV_BUY_BOX = 18

    def __init__(self, product: dict):
        self.product = product

    @staticmethod
    def _last_value(series):
        """
        Keepa history series are pairs: [timestamp, value, timestamp,
        value, ...]. Value always sits at an ODD index. Sometimes Keepa
        returns a dangling extra timestamp at the end with no paired
        value yet (an odd-length array) -- if we don't account for
        that, we read a raw timestamp as if it were a price/rank and
        corrupt it (this was the source of the ~76000-79000 "phantom
        cost" bug: a 2026-era Keepa-minutes timestamp divided by 100).
        """
        if not series:
            return 0

        start = len(series) - 1
        if start % 2 == 0:
            # Odd-length array -- last element is a dangling timestamp,
            # not a value. Step back one to land on the real last value.
            start -= 1

        for i in range(start, 0, -2):
            value = series[i]
            if value not in (-1, None):
                return value

        return 0

    def _avg90(self, csv_index, divisor=1):
        stats = self.product.get("stats") or {}
        avg90 = stats.get("avg90")

        if not avg90 or csv_index >= len(avg90):
            return 0

        value = avg90[csv_index]

        if value in (-1, None):
            return 0

        return value / divisor if divisor != 1 else value

    # ---- Pricing ----

    def buy_box_now(self) -> float:
        csv = self.product.get("csv")

        if not csv:
            return 0

        value = self._last_value(csv[self.CSV_BUY_BOX])

        return value / 100 if value else 0

    def buy_box_90d(self) -> float:
        return round(self._avg90(self.CSV_BUY_BOX, divisor=100), 2)

    # ---- Sales rank ----

    def sales_rank_now(self) -> int:
        ranks = self.product.get("salesRanks") or {}

        if not ranks:
            return 0

        first = next(iter(ranks.values()))

        return self._last_value(first)

    def sales_rank_90d(self) -> int:
        return int(self._avg90(self.CSV_SALES_RANK))

    # ---- Competition ----

    def offers_now(self) -> int:
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_OFFER_COUNT_NEW:
            return 0

        return int(self._last_value(csv[self.CSV_OFFER_COUNT_NEW]) or 0)

    def offers_90d(self) -> int:
        return int(self._avg90(self.CSV_OFFER_COUNT_NEW))

    # ---- Sales velocity ----

    def monthly_sales(self) -> int:
        stats = self.product.get("stats")

        if not stats:
            return 0

        return stats.get("monthlySold") or 0

    def sales_drops_30d(self) -> int:
        stats = self.product.get("stats") or {}
        return stats.get("salesRankDrops30") or 0

    # ---- Fees ----

    def fba_fee(self) -> float:
        """
        Real FBA pick & pack fee from Keepa, in the marketplace's own
        currency. Falls back to 0 if Keepa hasn't returned fee data
        for this product (common for low-data/new listings) -- the
        caller should fall back to FeeEngine's default in that case.
        """
        fees = self.product.get("fbaFees") or {}
        cents = fees.get("pickAndPackFee")

        if not cents:
            return 0

        return round(cents / 100, 2)

    # ---- Flags ----

    def is_hazmat(self) -> bool:
        return bool(self.product.get("isHazMat", False))