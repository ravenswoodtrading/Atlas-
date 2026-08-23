from app.keepa.client import get_keepa_client
from app.services.token_usage_service import TokenUsageService


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

    def get_products(self, asins, marketplace="UK", retries=1, full=True, stats_days=90,
                      include_rating=False, include_offers=False, usage_category="other"):
        """
        `full=True` (used for UK) requests `stats_days`-day stats too
        -- needed for trend/scoring. `full=False` (used for EU price
        checks) drops it, since EU data is only ever used for its
        current price (see ProductMapper.from_keepa_multi).

        stats_days defaults to 90 (what every existing caller needs).
        VerdictService passes 180 so KeepaParser.price_avg(180) has
        real data instead of 0 -- Keepa's stats.avg180 is only
        populated if the request actually asked for a >=180-day
        window.

        offers=N is deliberately NEVER requested for scan/discovery/
        watchlist/replen/competitors -- confirmed by inspecting every
        field KeepaParser reads: nothing in those pipelines needs the
        raw offers list, so requesting it there is pure wasted cost.
        VerdictService is the one exception (include_offers) -- see
        below.

        include_rating (default False, so no extra Keepa token cost on
        scan/discovery/watchlist/replen/competitors -- none of them
        read rating/review_count today) requests Keepa's `rating`
        option. Without it, Keepa omits the RATING/COUNT_REVIEWS csv
        series entirely (csv indices 16/17), so
        KeepaParser.rating()/review_count() always read back 0
        regardless of the product's real review count -- found live:
        the Verdict Checker reported "no reviews" on an ASIN the user
        had manually confirmed does have reviews. VerdictService is
        the only caller that turns this on, since it's the only one
        that shows/uses rating or review count.

        include_offers (default False) requests Keepa's `offers`
        option (minimum 20 -- Keepa's own floor, can't ask for fewer).
        A handful of stats fields Keepa documents as "only set when
        the offers parameter was used" depend on this: offerCountFBA
        (exact current FBA offer count), buyBoxIsAmazon/buyBoxIsFBA/
        buyBoxSellerId (who currently holds the buy box). Without it,
        KeepaParser falls back to weaker proxies inferred from price
        history alone -- found live: Atlas reported no FBA offer on
        B0DMV3SPV1 despite real FBA offers being visible on Amazon,
        because the old NEW_FBA-price-history proxy only sees whichever
        FBA offer was CHEAPEST last time Keepa refreshed the snapshot,
        not "is there one at all". Only VerdictService turns this on
        -- it's one of the priciest Keepa options (per-ASIN token cost
        scales with the offer count requested), not worth paying on
        every bulk scan, but a single manual Verdict Check can afford
        the accuracy.

        usage_category: which Atlas feature is calling this, purely
        for the Settings > Token Usage page (see TokenUsageEvent) --
        has no effect on the Keepa call itself. Defaults to "other"
        for any caller that hasn't been given a real label yet.
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
            query_kwargs["stats"] = stats_days

        if include_rating:
            query_kwargs["rating"] = True

        if include_offers:
            query_kwargs["offers"] = 20

        for attempt in range(retries + 1):
            tokens_before = self.api.tokens_left

            try:
                result = self.api.query(**query_kwargs)
                TokenUsageService.record_keepa_spend(
                    usage_category, "keepa_query", tokens_before, self.api.tokens_left,
                    marketplace=marketplace, asins_count=len(asins),
                )
                return result
            except Exception as exc:
                if attempt < retries:
                    continue

                # A single slow/failed Keepa response (network timeout,
                # server hiccup) shouldn't crash the whole scan -- treat
                # this marketplace as having no data for this batch,
                # same as if the ASINs simply weren't listed there.
                print(f"Keepa query failed for {marketplace} after {retries + 1} attempt(s): {exc}")
                return []