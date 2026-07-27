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
    def _last_buybox_price(series):
        """
        Confirmed against real Keepa data: unlike the plain [time, value]
        pairs used by series like SALES rank or COUNT_NEW, the