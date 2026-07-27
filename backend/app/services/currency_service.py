class CurrencyService:
    """
    Converts marketplace prices to GBP so European buy costs can be
    compared against UK sell prices.

    MVP: fixed rates. Swap FIXED_RATES for a live FX API call
    (e.g. exchangerate.host, or your Keepa-adjacent supplier) before
    relying on this for real purchasing decisions -- EUR/GBP moves
    enough to matter on thin-margin A2A deals.
    """

    FIXED_RATES_TO_GBP = {
        "GBP": 1.0,
        "EUR": 0.84,
    }

    @classmethod
    def to_gbp(cls, amount: float, currency: str) -> float:
        rate = cls.FIXED_RATES_TO_GBP.get(currency.upper())

        if rate is None:
            raise ValueError(f"No GBP conversion rate for currency: {currency}")

        return round(amount * rate, 2)
