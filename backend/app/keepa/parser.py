from datetime import datetime, timedelta, timezone


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

    CSV_AMAZON = 0
    CSV_SALES_RANK = 3
    CSV_NEW_FBA = 10
    CSV_OFFER_COUNT_NEW = 11
    CSV_RATING = 16
    CSV_COUNT_REVIEWS = 17
    CSV_BUY_BOX = 18

    # Keepa timestamps ("Keepa minutes", e.g. lastSoldUpdate) count
    # minutes since this fixed epoch -- confirmed via the keepa
    # library's own KEEPA_ST_ORDINAL constant.
    KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)

    def __init__(self, product: dict):
        self.product = product

    @staticmethod
    def _last_buybox_price(series):
        """
        Confirmed against real Keepa data: unlike the plain [time, value]
        pairs used by series like SALES rank or COUNT_NEW, the
        BUY_BOX_SHIPPING series (csv index 18) comes back as TRIPLES:
        [time, price, shipping], repeated. e.g.:
            [..., 7980168, 13266, 4229, 7981976, -1, -1]
        The last triple here has price=-1 (no current data). Stepping
        by 2 (as if this were pairs) walks completely out of alignment
        and periodically reads a raw timestamp as if it were a price --
        this was the actual cause of the recurring ~79819 "phantom
        cost" bug, not the odd-length dangling-timestamp theory used
        for the plain-pair series below.

        MAX_SANE_PRICE_CENTS is a defensive safety net on top of the
        correct triple parsing above -- borrowed from an older scanner
        script that filtered out any implausible price (e.g. a
        misread timestamp) the same way, regardless of root cause.
        Keeps future edge cases in Keepa's data from silently
        producing a phantom cost even if this parsing logic turns out
        to be wrong somewhere else we haven't seen yet. GBP 1000 is an
        arbitrary but generous ceiling for FBA-sourced products.
        """
        MAX_SANE_PRICE_CENTS = 100_000  # GBP 1000

        if not series or len(series) % 3 != 0:
            return 0

        for i in range(len(series) - 3, -1, -3):
            price = series[i + 1]
            if price not in (-1, None) and 0 < price < MAX_SANE_PRICE_CENTS:
                return price

        return 0

    @staticmethod
    def _last_value(series):
        """
        Keepa history series are pairs: [timestamp, value, timestamp,
        value, ...]. Value always sits at an ODD index. Sometimes Keepa
        returns a dangling extra timestamp at the end with no paired
        value yet (an odd-length array) -- if we don't account for
        that, we read a raw timestamp as if it were a price/rank and
        corrupt it.

        NOTE: this is for plain pair-structured series (SALES rank,
        COUNT_NEW). BUY_BOX_SHIPPING uses a different, triple
        structure -- see _last_buybox_price above. The pair assumption
        here is still unverified against real csv[3]/csv[11] data;
        flag it if sales rank or offer counts look wrong.
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

        value = self._last_buybox_price(csv[self.CSV_BUY_BOX])

        return value / 100 if value else 0

    def buy_box_90d(self) -> float:
        return round(self._avg90(self.CSV_BUY_BOX, divisor=100), 2)

    def buy_box_min_90d(self) -> float:
        """
        Lowest buy-box price seen within the last 90 days. Reads
        stats.minInInterval, NOT stats.min -- CONFIRMED via a live
        product query (stats=90 requested) that these are genuinely
        different fields: stats.min is an ALL-TIME low regardless of
        the requested stats window (can be years stale), while
        stats.minInInterval is scoped to that window. Using stats.min
        here would silently treat an old, irrelevant price crash as if
        it were a recent dip.

        Format confirmed live: minInInterval[CSV_BUY_BOX] is a single
        [timestamp, price_cents] pair (not a stepped series like csv),
        same (time, value) convention used elsewhere in this file.
        """
        stats = self.product.get("stats") or {}
        min_in_interval = stats.get("minInInterval")

        if not min_in_interval or self.CSV_BUY_BOX >= len(min_in_interval):
            return 0

        pair = min_in_interval[self.CSV_BUY_BOX]

        if not pair or pair[1] in (-1, None):
            return 0

        return round(pair[1] / 100, 2)

    def buy_box_max_90d(self) -> float:
        """
        Highest buy-box price seen within the last 90 days -- the peak
        counterpart to buy_box_min_90d. Reads stats.maxInInterval, NOT
        stats.max (see price_max, which is all-time and can be years
        stale). Same [timestamp, price_cents] pair shape as
        minInInterval; assumed symmetric with it since Keepa's stats
        object documents them as a pair -- flag if a live response
        ever shows maxInInterval absent/shaped differently.

        This is what a "peak pricing window" opportunity should be
        judged against -- a product whose price genuinely swings
        between a low and a high (e.g. Amazon repeatedly raising then
        dropping it) rather than one that spiked once, years ago, and
        never again. See OpportunityEngine's PEAK_WINDOW recommendation.
        """
        stats = self.product.get("stats") or {}
        max_in_interval = stats.get("maxInInterval")

        if not max_in_interval or self.CSV_BUY_BOX >= len(max_in_interval):
            return 0

        pair = max_in_interval[self.CSV_BUY_BOX]

        if not pair or pair[1] in (-1, None):
            return 0

        return round(pair[1] / 100, 2)

    def price_min_ever(self) -> float:
        """
        All-time low buy-box price -- reads stats.min, NOT
        stats.minInInterval (see buy_box_min_90d, which is scoped to
        the requested stats window). Can be years stale; shown
        alongside buy_box_min_90d so the UI can distinguish "cheapest
        ever" from "cheapest recently".
        """
        stats = self.product.get("stats") or {}
        min_all_time = stats.get("min")

        if not min_all_time or self.CSV_BUY_BOX >= len(min_all_time):
            return 0

        pair = min_all_time[self.CSV_BUY_BOX]

        if not pair or pair[1] in (-1, None):
            return 0

        return round(pair[1] / 100, 2)

    def price_max(self) -> float:
        """
        All-time high buy-box price -- reads stats.max, same [time,
        price_cents] pair shape as stats.min/minInInterval.
        """
        stats = self.product.get("stats") or {}
        max_all_time = stats.get("max")

        if not max_all_time or self.CSV_BUY_BOX >= len(max_all_time):
            return 0

        pair = max_all_time[self.CSV_BUY_BOX]

        if not pair or pair[1] in (-1, None):
            return 0

        return round(pair[1] / 100, 2)

    def price_avg(self, window_days: int) -> float:
        """
        Average buy-box price over the given window -- 30/90/180 only
        (stats.avg30/avg90/avg180 are the only windows Keepa exposes;
        there is no native 60-day average, so callers wanting a
        30/60/90/180 trend should interpolate or omit 60 rather than
        treat this as returning one for window_days=60).
        """
        stats = self.product.get("stats") or {}
        key = f"avg{window_days}"
        avg = stats.get(key)

        if not avg or self.CSV_BUY_BOX >= len(avg):
            return 0

        value = avg[self.CSV_BUY_BOX]

        if value in (-1, None):
            return 0

        return round(value / 100, 2)

    def price_drop_count(self, window_days: int) -> int:
        """
        Number of downward price moves in the buy-box series within
        the last `window_days` -- each drop is a rough proxy for a
        discount/repricing event, not necessarily a sale. Walks the
        [time, price, shipping] triples (see _last_buybox_price for
        why this series is triple- not pair-structured) newest-first,
        stopping once a timestamp falls outside the window.
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_BUY_BOX:
            return 0

        series = csv[self.CSV_BUY_BOX]

        if not series or len(series) % 3 != 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        prices = []

        for i in range(0, len(series), 3):
            minutes, price = series[i], series[i + 1]

            if price in (-1, None):
                continue

            timestamp = self.KEEPA_EPOCH + timedelta(minutes=minutes)

            if timestamp < cutoff:
                continue

            prices.append(price)

        drops = 0
        for prev, curr in zip(prices, prices[1:]):
            if curr < prev:
                drops += 1

        return drops

    def daily_buy_box_prices(self, window_days: int) -> list:
        """
        Reconstructs the buy-box price actually in effect on each of
        the last `window_days` calendar days (oldest first) -- not a
        single min/max/avg summary. Built so VerdictService can answer
        "how many of the last 90 days would this actually have been
        at a viable price", which the stats-window aggregates above
        can't: a product profitable on most days but dragged under by
        a handful of low outliers reads identically to one that's
        marginal every day, if all you have is the average.

        Walks the BUY_BOX_SHIPPING triples the same way price_drop_count
        does (see _last_buybox_price's docstring for why this series is
        triple- not pair-structured), but keeps every change point
        rather than counting drops, then holds each price constant from
        one change point to the next ("step" reconstruction) to fill
        every day in between -- the same convention Keepa's own charts
        use for a step-series price history.

        0.0 for a day with no price data at all (before the product's
        very first tracked price, or genuinely no buy box that day --
        e.g. out of stock). Callers should treat 0.0 as "no sale
        possible that day", not "free".
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_BUY_BOX:
            return [0.0] * window_days

        series = csv[self.CSV_BUY_BOX]

        if not series or len(series) % 3 != 0:
            return [0.0] * window_days

        MAX_SANE_PRICE_CENTS = 100_000  # GBP 1000 -- same ceiling as _last_buybox_price

        changes = []
        for i in range(0, len(series), 3):
            minutes, price = series[i], series[i + 1]
            price_gbp = price / 100 if price not in (-1, None) and 0 < price < MAX_SANE_PRICE_CENTS else 0.0
            timestamp = self.KEEPA_EPOCH + timedelta(minutes=minutes)
            changes.append((timestamp, price_gbp))

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=window_days)

        daily_prices = []
        effective_price = 0.0
        change_index = 0

        for day_offset in range(window_days):
            day = window_start + timedelta(days=day_offset)

            while change_index < len(changes) and changes[change_index][0] <= day:
                effective_price = changes[change_index][1]
                change_index += 1

            daily_prices.append(round(effective_price, 2))

        return daily_prices

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

    def offer_count_fba(self):
        """
        Exact current FBA offer count from stats.offerCountFBA -- only
        populated when the query requested Keepa's `offers` option
        (see ProductService.get_products' include_offers; only
        VerdictService turns this on). Returns None when unavailable
        (offers wasn't requested this call, or Keepa itself couldn't
        resolve it) -- Keepa's own sentinel for "not set" is -2, not 0,
        so this must NOT collapse to 0 (that would silently claim "no
        FBA offers" when the truth is "we didn't ask").
        """
        stats = self.product.get("stats") or {}
        value = stats.get("offerCountFBA")

        return value if value is not None and value >= 0 else None

    def offers_fba_present(self) -> bool:
        """
        True if at least one FBA offer currently exists. Prefers
        offer_count_fba() (Keepa's own authoritative current count)
        when available. Falls back to a weaker proxy -- whether the
        NEW_FBA price series (index 10, the CHEAPEST current FBA
        offer's price) has a recent real value -- for every caller
        that doesn't request `offers` (scan/discovery/watchlist/
        replen/competitors, i.e. everything except VerdictService).

        The fallback proxy is genuinely weaker, not just a cheaper
        version of the same thing: it only reflects whichever FBA
        offer was CHEAPEST as of Keepa's last snapshot, so a real FBA
        offer that isn't currently the cheapest (or that Keepa hasn't
        re-priced recently) can be missed entirely. Confirmed live:
        Atlas reported no FBA offer on B0DMV3SPV1 despite real FBA
        offers being visible on the actual Amazon listing.
        """
        offer_count = self.offer_count_fba()

        if offer_count is not None:
            return offer_count > 0

        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_NEW_FBA:
            return False

        series = csv[self.CSV_NEW_FBA]

        if not series:
            return False

        return self._last_value(series) not in (0, None)

    def competitor_stock_levels(self) -> list[dict] | None:
        """
        Per-seller current stock, from Keepa's raw `offers` list --
        only populated when the query requested BOTH offers AND stock
        (see ProductService.get_products' include_offers/include_stock;
        VerdictService's deep-dive check is the only caller of
        include_stock today). Returns None when offers weren't
        requested at all, or none of the returned offers carry a
        stockCSV (stock wasn't requested, or Keepa simply has no stock
        data for this listing yet).

        Each offer's stockCSV is Keepa's raw, UNDECODED [keepaMinutes,
        stock, keepaMinutes, stock, ...] pair series (confirmed against
        the keepa package's own field docs, 2026-08-23 -- unlike the
        top-level product `csv` series, the package does not convert
        this) -- only the most recent stock value (the last element) is
        read here; the history isn't needed for a point-in-time deep
        dive.

        stock is Keepa's own reported figure, capped: Keepa (mirroring
        Amazon's own listing page) can only confirm EXACT stock up to
        10 units -- a value of 10 should be read as "10 or more", never
        as a precise count. Sorted highest-stock-first: the seller most
        able to sustain being undercut is the real "how much room does
        this listing have" signal a bare offer COUNT can't answer.
        """
        offers = self.product.get("offers")

        if not offers:
            return None

        levels = []
        for offer in offers:
            stock_csv = offer.get("stockCSV")

            if not stock_csv or len(stock_csv) < 2:
                continue

            stock = stock_csv[-1]

            if stock is None or stock < 0:
                continue

            levels.append({
                "seller_id": offer.get("sellerId") or "",
                "is_amazon": bool(offer.get("isAmazon")),
                "is_fba": bool(offer.get("isFBA")),
                "stock": stock,
            })

        if not levels:
            return None

        levels.sort(key=lambda o: o["stock"], reverse=True)
        return levels

    def _current_pair_price(self, csv_index: int) -> float:
        """
        Current price for a plain [time, value] PAIR-structured price
        series (AMAZON=0, NEW_FBA=10, etc) -- NOT for BUY_BOX_SHIPPING
        (18), which is TRIPLE-structured and handled by
        _last_buybox_price instead (see that method's docstring for
        why conflating the two caused the real ~79819 "phantom cost"
        bug this file already documents). Same cents-to-GBP/EUR
        conversion as buy_box_now. 0 if the series is absent/empty or
        Keepa has no current value.
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= csv_index:
            return 0

        value = self._last_value(csv[csv_index])

        return round(value / 100, 2) if value else 0

    def buy_box_is_amazon_fulfilled(self) -> bool:
        """
        Whether the CURRENT buy-box holder is fulfilled by Amazon --
        either Amazon itself, or a third-party seller using FBA -- as
        opposed to a merchant-fulfilled (FBM) seller shipping the item
        themselves. This is the distinction that actually matters for
        EU A2A sourcing: an Amazon-fulfilled EU purchase comes with an
        Amazon VAT invoice (reclaimable), an FBM one doesn't -- see
        ProductMapper.from_keepa_multi, the one place this gates
        whether a marketplace can become best_source_marketplace at
        all.

        PROXY, not Keepa's authoritative buyBoxIsFBA/buyBoxIsAmazon
        stats fields -- those require Keepa's `offers` option, which
        would multiply the token cost of every EU marketplace lookup
        for every ASIN in a bulk scan (see ProductService.get_products'
        own docstring on why only VerdictService, a single-ASIN manual
        check, can afford to turn it on). Instead, compares the live
        buy-box price (BUY_BOX_SHIPPING, index 18, via buy_box_now) to
        the current AMAZON (index 0) and NEW_FBA (index 10) prices --
        both already relied on elsewhere in this file
        (is_amazon_on_listing, offers_fba_present) -- and treats an
        exact match against either as Amazon-fulfilled.

        Deliberately FAILS CLOSED: any buy-box price that doesn't
        exactly match either series -- including a genuine FBM seller
        winning the box, Keepa's per-series snapshots being very
        slightly out of sync, or simply no buy-box price at all -- is
        treated as NOT usable, not as "unknown, assume it's fine".
        Given the real cost of getting this wrong (an EU "source" you
        can't actually buy and can't reclaim VAT on), under-counting
        genuine A2A leads is the safer failure direction than
        over-counting unbuyable ones. Not yet checked against a live
        Keepa response with a confirmed FBM buy-box winner -- worth
        confirming (and loosening the exact-match comparison to a
        small tolerance if it proves too strict) once real EU scan
        data is flowing.
        """
        buy_box_price = self.buy_box_now()

        if not buy_box_price:
            return False

        amazon_price = self._current_pair_price(self.CSV_AMAZON)
        fba_price = self._current_pair_price(self.CSV_NEW_FBA)

        return buy_box_price == amazon_price or buy_box_price == fba_price

    def offer_trend(self) -> str:
        """
        "rising" / "falling" / "saturating" / "stable" -- coarse
        classification comparing the current offer count against the
        90-day average. "saturating" is used (rather than just
        "rising") once offers_now clears a high absolute floor, since
        a jump from 2 to 4 offers and a jump from 20 to 22 both read
        as "+2" but mean very different things for how crowded the
        listing already is.
        """
        now = self.offers_now()
        avg = self.offers_90d()

        SATURATED_OFFER_FLOOR = 15

        if avg == 0 and now == 0:
            return "stable"

        if now >= SATURATED_OFFER_FLOOR and now >= avg:
            return "saturating"

        if now > avg:
            return "rising"

        if now < avg:
            return "falling"

        return "stable"

    def buy_box_percentage(self) -> float:
        """
        Highest single seller's share of buy-box wins over the
        requested stats window (0-100), from stats.buyBoxStats --
        Keepa keys this by sellerId, so this reports whichever seller
        held it most rather than assuming Amazon's ID (which differs
        per marketplace). A high value means one seller (often Amazon
        itself) dominates the buy box; a low/absent value means it
        rotates -- generally more room for a new FBA offer to win it.
        """
        stats = self.product.get("stats") or {}
        buy_box_stats = stats.get("buyBoxStats") or {}

        if not buy_box_stats:
            return 0

        best = max(
            (entry.get("percentageWon") or 0 for entry in buy_box_stats.values()),
            default=0,
        )

        return round(best, 1)

    def amazon_buy_box_percentage(self) -> float:
        """
        Amazon's OWN share of buy-box wins over the requested stats
        window (0-100) -- unlike buy_box_percentage() above (whichever
        seller held it most, not necessarily Amazon), this is Amazon's
        number specifically. Built for flagging "Amazon is on this
        listing AND dominates the buy box" as the real competitive
        risk, rather than Amazon merely being present (which on its
        own isn't a problem -- plenty of profitable A2A/OA leads have
        Amazon as one of several rotating offers).

        Resolved via stats.buyBoxSellerId (whoever holds the buy box
        RIGHT NOW) cross-referenced against stats.buyBoxStats -- only
        possible when stats.buyBoxIsAmazon is true, since Keepa's
        buyBoxStats entries aren't tagged with WHICH seller ID belongs
        to Amazon, and Amazon's own seller ID differs per marketplace
        and isn't reliably known ahead of time. Returns 0 if Amazon
        isn't the CURRENT buy box holder, even if it held a real share
        earlier in the window -- an honest "can't resolve it right
        now" rather than a guess.
        """
        stats = self.product.get("stats") or {}

        if not stats.get("buyBoxIsAmazon"):
            return 0

        seller_id = stats.get("buyBoxSellerId")
        buy_box_stats = stats.get("buyBoxStats") or {}
        entry = buy_box_stats.get(str(seller_id)) or {}

        return round(entry.get("percentageWon") or 0, 1)

    # ---- Sales velocity ----

    def monthly_sales(self) -> int:
        """
        Keepa's confirmed "bought in past month" figure. This lives at
        the TOP LEVEL of the raw product dict (product["monthlySold"]),
        NOT inside product["stats"] -- confirmed against Keepa's own
        schema, where the Stats object has no monthlySold field at
        all. Reading it from stats (the previous code here) silently
        returned 0 for every product regardless of Keepa's real data.

        This is Keepa's LAST CONFIRMED value, not necessarily current
        -- see monthly_sales_as_of() for how stale it is.
        """
        return self.product.get("monthlySold") or 0

    def monthly_sales_as_of(self):
        """
        The datetime Keepa last updated monthly_sales(), or None if it
        has never had a value. A nonzero monthly_sales can be months
        old if Amazon hasn't republished the "bought in past month"
        badge recently -- always show this alongside the figure
        rather than implying it's current.
        """
        minutes = self.product.get("lastSoldUpdate")

        if not minutes:
            return None

        return self.KEEPA_EPOCH + timedelta(minutes=minutes)

    def sales_drops_30d(self) -> int:
        stats = self.product.get("stats") or {}
        return stats.get("salesRankDrops30") or 0

    def ean(self) -> str:
        """
        First EAN Keepa has on file for this ASIN, or "" if none.
        Reference/display only -- CONFIRMED via a live SerpApi test
        that searching by bare EAN as query text does NOT work as a
        barcode lookup (returned completely unrelated products), so
        this is shown for the user's own manual cross-check, not used
        to drive any search itself.
        """
        eans = self.product.get("eanList") or []
        return eans[0] if eans else ""

    # ---- Reviews ----

    def rating(self) -> float:
        """
        Current star rating (0-5) -- Keepa's RATING series stores this
        in tenths (e.g. 45 = 4.5 stars), same _last_value pair-series
        pattern as sales rank/offers.
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_RATING:
            return 0

        value = self._last_value(csv[self.CSV_RATING])

        return round(value / 10, 1) if value else 0

    def review_count(self) -> int:
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_COUNT_REVIEWS:
            return 0

        return int(self._last_value(csv[self.CSV_COUNT_REVIEWS]) or 0)

    # ---- Availability ----

    def is_amazon_on_listing(self) -> bool:
        """
        True if Amazon itself is currently selling this listing.
        Keepa's own `availabilityAmazon` field (-1 = not sold by
        Amazon, >=0 = an availability code) is used when present;
        falls back to checking the AMAZON csv series (index 0) has a
        current value, for older/partial responses that omit the
        top-level field.
        """
        availability = self.product.get("availabilityAmazon")

        if availability is not None:
            return availability != -1

        csv = self.product.get("csv")

        if not csv or len(csv) == 0:
            return False

        return self._last_value(csv[0]) not in (0, None)

    def is_out_of_stock(self) -> bool:
        """
        Rough current-stock proxy: no live offers at all. This is a
        proxy, not a confirmed Keepa stock flag -- a listing can also
        show 0 offers because Keepa hasn't refreshed it recently, not
        only because it's genuinely unavailable.
        """
        return self.offers_now() == 0

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
