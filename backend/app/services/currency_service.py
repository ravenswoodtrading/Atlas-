import time

import requests


class CurrencyService:
    """
    Converts marketplace prices to GBP so European buy costs can be
    compared against UK sell prices.

    Fetches a live EUR->GBP rate from Frankfurter (api.frankfurter.dev
    -- ECB-sourced, free, no API key required) and caches it for
    CACHE_TTL_SECONDS so a scan doesn't hit the network on every
    single price conversion (a brand scan can convert hundreds of EU
    prices). Falls back to the last successfully-fetched rate if a
    refresh attempt fails (network blip, API down, offline) so a
    temporary outage doesn't interrupt scanning, and only falls back
    to the hardcoded FALLBACK_RATE_EUR_TO_GBP below if no live rate
    has EVER been fetched yet this run (e.g. first call with no
    internet access at all).

    This replaces what used to be a single hardcoded constant (0.84)
    with no update mechanism -- confirmed via a live check on
    2026-08-17 that the real rate had already drifted to ~0.855, a
    ~1.8% error that was making every EU cost (and therefore every
    profit/ROI figure derived from it) look better than it really is.
    On thin-margin A2A deals near the 10-25% ROI thresholds that
    OpportunityEngine/ReviewQueueService gate on, that's large enough
    to flip a genuinely marginal product to looking like a real one,
    or vice versa as the real rate drifts the other way over time.
    """

    FALLBACK_RATE_EUR_TO_GBP = 0.84

    # ECB (Frankfurter's source) only publishes a new rate once per
    # weekday, so refreshing twice a day is already more than enough
    # -- this just bounds the network calls, not a freshness target.
    CACHE_TTL_SECONDS = 12 * 60 * 60

    FX_API_URL = "https://api.frankfurter.dev/v1/latest"

    _cached_rate: float | None = None
    _cached_at: float = 0.0

    @classmethod
    def _get_eur_to_gbp_rate(cls) -> float:
        now = time.time()

        if cls._cached_rate is not None and (now - cls._cached_at) < cls.CACHE_TTL_SECONDS:
            return cls._cached_rate

        try:
            response = requests.get(
                cls.FX_API_URL, params={"base": "EUR", "symbols": "GBP"}, timeout=5,
            )
            response.raise_for_status()
            rate = float(response.json()["rates"]["GBP"])

            cls._cached_rate = rate
            cls._cached_at = now

            return rate

        except Exception as exc:
            if cls._cached_rate is not None:
                # A refresh failed, but we already have a real rate
                # from an earlier successful call this run -- that's
                # still far better than the hardcoded placeholder, so
                # keep using it (just don't reset _cached_at, so the
                # next call tries to refresh again rather than
                # treating this stale value as freshly confirmed).
                print(f"CurrencyService: FX refresh failed ({exc}), reusing last known rate {cls._cached_rate}")
                return cls._cached_rate

            print(f"CurrencyService: FX fetch failed ({exc}), using hardcoded fallback rate {cls.FALLBACK_RATE_EUR_TO_GBP}")
            return cls.FALLBACK_RATE_EUR_TO_GBP

    @classmethod
    def to_gbp(cls, amount: float, currency: str) -> float:
        currency = currency.upper()

        if currency == "GBP":
            return round(amount, 2)

        if currency == "EUR":
            return round(amount * cls._get_eur_to_gbp_rate(), 2)

        raise ValueError(f"No GBP conversion rate for currency: {currency}")
