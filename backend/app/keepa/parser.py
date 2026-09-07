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

    # Amazon's own stable seller ID on amazon.co.uk -- confirmed live,
    # 2026-09-07 (Tamara flagged B0CFV7Z7SJ: "this only had amazon on
    # the buy box in the past 30 days"): cross-referenced against a
    # SECOND real ASIN where stats.buyBoxIsAmazon was True at the
    # moment stats.buyBoxSellerId equalled this exact value -- the same
    # ID also appears in B0CFV7Z7SJ's own buyBoxSellerIdHistory. Amazon
    # sells under one stable account per marketplace, so this is safe
    # to treat as a fixed constant (not derived per-product) -- the
    # only reliable way to identify Amazon in buyBoxSellerIdHistory when
    # the product's OWN stats.buyBoxIsAmazon happens to be False at the
    # current moment (e.g. Amazon is between stock, as B0CFV7Z7SJ was
    # when this was found -- stats.buyBoxIsAmazon alone would have
    # silently missed a real 30-day pattern of total Amazon dominance).
    AMAZON_UK_SELLER_ID = "A3P5ROKL5A1OLE"

    # buyBoxSellerIdHistory's own "no current buy box holder" sentinel
    # values (confirmed live: -1 appears constantly, e.g. out of stock/
    # no qualifying offer; -2 seen occasionally, meaning unclear from
    # Keepa's own docs but never a real seller ID either way) -- neither
    # counts as a competitor OR as Amazon.
    NO_BUY_BOX_HOLDER_CODES = ("-1", "-2")

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

    def daily_buy_box_peak_prices(self, window_days: int) -> list:
        """
        Real bug found live, 2026-09-07 (Tamara, re: B0FQCB7YS9: "it had
        a buy box of £229 for a number of days in August" -- turned out
        to be Amazon's own listed price, not the buy box, but chasing
        that down surfaced a genuine, separate gap): daily_buy_box_prices
        above takes exactly ONE snapshot per day -- whatever price was in
        effect at that day's own midnight boundary -- so a real intraday
        price spike that both rose AND reverted within the same day (a
        real one confirmed live: 2026-08-27, buy box jumped to £181.54 at
        17:36 and was back down to £153.99 by 19:25) is invisible to it
        entirely; the midnight snapshot for that day shows £154.69, and
        a genuine several-hour buying window that would have cleared a
        real ROI bar is silently never counted.

        This is deliberately its OWN method, not a change to
        daily_buy_box_prices' existing behaviour -- that function has 8+
        callers across EU A2A/UK A2A/competition-spike classification
        (see SourcingClassifier) whose thresholds were calibrated against
        its snapshot semantics; changing it under them risks shifting
        real classifications in ways not modelled here. This is used
        ONLY for VerdictService.compute_metrics' viable-days-at-ROI
        metric, which is explicitly asking "was there EVER a real buying
        window this day", not "what was the price at midnight" -- the
        MAX price seen at any point during the day is the right answer
        to that question (a higher buy box price is a BETTER outcome for
        whoever holds it, i.e. what WE would have sold at that day).

        Same triple-walk/step-carry-forward mechanics as daily_buy_box_
        prices (a day with no price change of its own still carries
        forward whatever was already in effect from a previous day), but
        tracks the MAX price observed within each day's own boundaries
        instead of a single point-in-time value. 0.0 for a day with no
        price data at all, same convention as daily_buy_box_prices.
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_BUY_BOX:
            return [0.0] * window_days

        series = csv[self.CSV_BUY_BOX]

        if not series or len(series) % 3 != 0:
            return [0.0] * window_days

        MAX_SANE_PRICE_CENTS = 100_000  # GBP 1000 -- same ceiling as daily_buy_box_prices

        changes = []
        for i in range(0, len(series), 3):
            minutes, price = series[i], series[i + 1]
            price_gbp = price / 100 if price not in (-1, None) and 0 < price < MAX_SANE_PRICE_CENTS else 0.0
            timestamp = self.KEEPA_EPOCH + timedelta(minutes=minutes)
            changes.append((timestamp, price_gbp))

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=window_days)

        daily_peak_prices = []
        carried_price = 0.0  # whatever was in effect BEFORE this day started
        change_index = 0

        for day_offset in range(window_days):
            day_start = window_start + timedelta(days=day_offset)
            day_end = day_start + timedelta(days=1)

            day_peak = carried_price

            while change_index < len(changes) and changes[change_index][0] < day_end:
                carried_price = changes[change_index][1]
                if carried_price > day_peak:
                    day_peak = carried_price
                change_index += 1

            daily_peak_prices.append(round(day_peak, 2))

        return daily_peak_prices

    def buy_box_holder_breakdown(self, window_days: int) -> dict:
        """
        Real gap found live, 2026-09-07 (Tamara, re: B0CFV7Z7SJ: "this
        only had amazon on the buy box in the past 30 days which is a
        red flag I should see") -- confirmed true against the real raw
        Keepa data (buyBoxSellerIdHistory): every single buy-box win in
        the last 30 days went to Amazon itself, with gaps where nobody
        held it, and genuinely ZERO third-party wins. The existing
        amazon_buy_box_percentage() below can't catch this: it reads
        stats.buyBoxIsAmazon (the CURRENT moment only) and returns 0 the
        instant Amazon isn't the CURRENT holder -- which is common
        precisely when Amazon rotates in and out of stock, exactly
        B0CFV7Z7SJ's own situation (stats.buyBoxIsAmazon was False --
        Amazon between stock -- at the moment this was found, silently
        hiding a genuine 30-day pattern of total Amazon dominance).

        Walks the raw buyBoxSellerIdHistory field (a TOP-LEVEL product
        key -- an [minutes_str, sellerId_str, ...] pair series, NOT the
        csv[CSV_BUY_BOX] triple series, which only carries price/shipping,
        never seller identity), holding each seller "in effect" between
        change points the same step/carry-forward convention as
        daily_buy_box_prices, then buckets TIME (not raw event count,
        which would over-weight a day with many rapid flips) into three
        totals over the window: amazon_minutes, competitor_minutes,
        no_holder_minutes (NO_BUY_BOX_HOLDER_CODES, e.g. out of stock).

        `third_party_ever_won` is the clean, unambiguous flag this was
        actually built for: True the instant ANY real competitor ID
        appears anywhere in the window, regardless of how briefly --
        the honest "has ANY reseller ever actually cracked this buy box
        recently" question, distinct from a percentage that a single
        long competitor stretch could dominate either direction.

        Returns all-zero / third_party_ever_won=False (never a crash or
        a guess) when buyBoxSellerIdHistory is missing or empty --
        e.g. an older/partial Keepa response.
        """
        history = self.product.get("buyBoxSellerIdHistory") or []

        if len(history) < 2:
            return {"amazon_minutes": 0, "competitor_minutes": 0, "no_holder_minutes": 0, "third_party_ever_won": False}

        usable_len = len(history) - (len(history) % 2)

        changes = []
        for i in range(0, usable_len, 2):
            try:
                minutes = int(history[i])
            except (TypeError, ValueError):
                continue
            seller_id = history[i + 1]
            timestamp = self.KEEPA_EPOCH + timedelta(minutes=minutes)
            changes.append((timestamp, seller_id))

        if not changes:
            return {"amazon_minutes": 0, "competitor_minutes": 0, "no_holder_minutes": 0, "third_party_ever_won": False}

        changes.sort(key=lambda c: c[0])

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=window_days)

        # Whoever was in effect AT window_start, carried forward from
        # before the window even began -- same "a day with no change of
        # its own still reflects the last real state" reasoning as
        # daily_buy_box_prices/daily_buy_box_peak_prices above.
        current_seller = None
        change_index = 0
        while change_index < len(changes) and changes[change_index][0] <= window_start:
            current_seller = changes[change_index][1]
            change_index += 1

        amazon_minutes = 0
        competitor_minutes = 0
        no_holder_minutes = 0
        third_party_ever_won = False
        segment_start = window_start

        def bucket(seller_id, duration_minutes):
            nonlocal amazon_minutes, competitor_minutes, no_holder_minutes, third_party_ever_won
            if seller_id is None or seller_id in self.NO_BUY_BOX_HOLDER_CODES:
                no_holder_minutes += duration_minutes
            elif seller_id == self.AMAZON_UK_SELLER_ID:
                amazon_minutes += duration_minutes
            else:
                competitor_minutes += duration_minutes
                third_party_ever_won = True

        while change_index < len(changes) and changes[change_index][0] < now:
            change_time, next_seller = changes[change_index]
            duration = (change_time - segment_start).total_seconds() / 60
            bucket(current_seller, duration)
            current_seller = next_seller
            segment_start = change_time
            change_index += 1

        bucket(current_seller, (now - segment_start).total_seconds() / 60)

        return {
            "amazon_minutes": round(amazon_minutes),
            "competitor_minutes": round(competitor_minutes),
            "no_holder_minutes": round(no_holder_minutes),
            "third_party_ever_won": third_party_ever_won,
        }

    def daily_offer_counts(self, window_days: int) -> list:
        """
        Same "step" reconstruction as daily_buy_box_prices, for the
        offer-count series instead of price (2026-09-04, Opportunity
        Engine 2.0 -- Tamara's own ask: "for a listing where offers are
        rising we should see how much of a problem this is", i.e. what
        did price actually do the last time competition spiked, not
        just whether it's spiked before).

        CSV_OFFER_COUNT_NEW (index 11) is plain [time, value] PAIR-
        structured (see _last_value's own docstring), NOT triple-
        structured like BUY_BOX_SHIPPING -- so this walks pairs, not
        triples, but holds each count constant between change points
        the same way. 0 for a day with no offer-count data at all.
        """
        csv = self.product.get("csv")

        if not csv or len(csv) <= self.CSV_OFFER_COUNT_NEW:
            return [0] * window_days

        series = csv[self.CSV_OFFER_COUNT_NEW]

        if not series or len(series) < 2:
            return [0] * window_days

        # Odd-length series has a dangling trailing timestamp with no
        # paired value yet -- same edge case _last_value guards against.
        usable_len = len(series) - 1 if len(series) % 2 else len(series)

        changes = []
        for i in range(0, usable_len, 2):
            minutes, count = series[i], series[i + 1]
            offer_count = int(count) if count not in (-1, None) else 0
            timestamp = self.KEEPA_EPOCH + timedelta(minutes=minutes)
            changes.append((timestamp, offer_count))

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=window_days)

        daily_counts = []
        effective_count = 0
        change_index = 0

        for day_offset in range(window_days):
            day = window_start + timedelta(days=day_offset)

            while change_index < len(changes) and changes[change_index][0] <= day:
                effective_count = changes[change_index][1]
                change_index += 1

            daily_counts.append(effective_count)

        return daily_counts

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

    def buy_box_holder(self) -> str:
        """
        WHO currently holds the buy box, as a label rather than the
        yes/no gate buy_box_is_amazon_fulfilled gives:
        "amazon" | "fba" | "fbm" | "none" | "unknown".

        Prefers Keepa's authoritative stats.buyBoxIsAmazon /
        stats.buyBoxIsFBA, which are only populated when the query
        requested the `offers` option -- so this is only ever fully
        accurate for VerdictService, the one caller that turns
        include_offers on (see ProductService.get_products' docstring
        for why bulk scans can't afford it).

        Without those fields it falls back to the same price-match
        proxy buy_box_is_amazon_fulfilled documents, which CANNOT tell
        a genuine FBM winner apart from Keepa's per-series snapshots
        being slightly out of sync. So the fallback returns "unknown"
        for any non-match, never "fbm" -- claiming a specific seller
        type Atlas can't actually see would be worse than admitting
        the gap. The fail-closed BUYABILITY decision still belongs to
        buy_box_is_amazon_fulfilled; this method only reports.
        """
        stats = self.product.get("stats") or {}

        is_amazon = stats.get("buyBoxIsAmazon")
        is_fba = stats.get("buyBoxIsFBA")

        if is_amazon is not None or is_fba is not None:
            if is_amazon:
                return "amazon"
            if is_fba:
                return "fba"
            # Both explicitly false and a live buy-box price exists:
            # a merchant-fulfilled seller genuinely holds it.
            return "fbm" if self.buy_box_now() else "none"

        buy_box_price = self.buy_box_now()

        if not buy_box_price:
            return "none"

        if buy_box_price == self._current_pair_price(self.CSV_AMAZON):
            return "amazon"

        if buy_box_price == self._current_pair_price(self.CSV_NEW_FBA):
            return "fba"

        return "unknown"

    def amazon_availability(self):
        """
        Keepa's raw availabilityAmazon code, or None if the response
        doesn't carry it. Keepa's meanings: -1 no Amazon offer exists
        at all, 0 in stock and shippable, 1 not currently in stock,
        2 unknown, 3 available with a delay (preorder/backorder).
        Returned raw so callers can distinguish "Amazon doesn't sell
        this" from "Amazon sells it but is out of stock right now" --
        two very different answers for an A2A source check.
        """
        return self.product.get("availabilityAmazon")

    def amazon_in_stock(self) -> bool:
        """
        True only when Amazon itself has this in stock and shippable
        RIGHT NOW (availabilityAmazon == 0) -- narrower than
        is_amazon_on_listing, which is just "does an Amazon offer
        exist at all" and stays True while Amazon is out of stock.

        Falls back to "the AMAZON price series (index 0) has a current
        value" for responses that omit availabilityAmazon, same
        fallback is_amazon_on_listing uses. That fallback can't tell
        in-stock from recently-in-stock, so it errs towards True; the
        authoritative field is present on ordinary Keepa responses.
        """
        availability = self.amazon_availability()

        if availability is not None:
            return availability == 0

        return self._current_pair_price(self.CSV_AMAZON) > 0

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

    def image(self) -> str:
        """
        Full URL to the product's primary image, or "" if Keepa has
        none on file. Zero extra API cost -- this is already part of
        the same product object every scan already fetches.

        Real bug, fixed 2026-09-05: this originally read "imagesCSV" as
        a comma-separated list of filenames (confirmed against a Keepa
        Product Finder CSV export's own "Image" column) -- but that's
        the CSV EXPORT's shape, not the keepa Python library's actual
        product object, which returns "images" as a LIST OF DICTS
        instead (each with 'l'/'m' large/medium filenames and a
        'variant' tag like "MAIN"/"FRNT"/"SIDE"/"BACK"). "imagesCSV"
        doesn't exist on the real response at all, so this silently
        returned "" for every single product ever scanned -- confirmed
        live: 0 of 17,863 saved records had an image, including scans
        from today, well after this was first added. Prefers the
        MAIN-variant entry (falls back to the first one if no variant
        is tagged MAIN), and its large filename (falls back to medium).
        """
        images = self.product.get("images") or []
        if not images:
            return ""
        main = next((img for img in images if img.get("variant") == "MAIN"), images[0])
        filename = main.get("l") or main.get("m") or ""
        return f"https://m.media-amazon.com/images/I/{filename}" if filename else ""

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
