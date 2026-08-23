from dataclasses import dataclass

from app.models.product import Product
from app.config.fees import (
    DEFAULT_REFERRAL_RATE,
    REFERRAL_RATE_BY_CATEGORY_NAME,
    REFERRAL_FEE_TIERS_BY_CATEGORY_NAME,
    MINIMUM_REFERRAL_FEE_GBP,
    PREP_FEE_GBP,
    UK_VAT_STANDARD_RATE,
    UK_VAT_ZERO_RATED_CATEGORY_NAMES,
    EU_VAT_RATE_BY_MARKETPLACE,
)


@dataclass
class FeeResult:
    referral_fee: float
    fba_fee: float
    prep_fee: float
    profit: float
    roi: float

    # Profit as a % of the GROSS sale price (what's actually on the
    # Amazon listing / what the customer paid), rather than of cost --
    # see Product.margin's docstring for why this exists alongside
    # ROI, not instead of it.
    #
    # FIXED 2026-08-19: this used to divide by the VAT-EXCLUSIVE net
    # revenue (buy_box_now / (1+VAT)) instead of the gross price --
    # mechanically inflated every margin figure by exactly the VAT
    # factor (20% relative, on standard-rated UK categories), e.g. a
    # real 10.1%-of-gross-price margin was showing as 12.1%. Caught by
    # the user noticing margin figures read too high vs. what they
    # expected -- same root cause/fix pattern as the earlier
    # ROI-vs-SAS mismatch documented in roi_at_price's docstring
    # above (Atlas's own internal accounting-correct math differing
    # from what a seller/SAS actually means by the term). Profit
    # itself was never wrong -- only which denominator "margin" used.
    margin: float

    profit_90d: float
    roi_90d: float
    margin_90d: float

    # Same calculation again, using the 90-day PEAK UK price
    # (product.buy_box_max_90d) instead of today's or the average --
    # see Product.profit_peak/roi_peak for what this is for.
    profit_peak: float
    roi_peak: float
    margin_peak: float

    # What rates were actually applied -- lets callers persist/display
    # WHY a given profit/roi came out the way it did (see
    # app/config/fees.py for where these come from). For a TIERED
    # category this is the EFFECTIVE blended rate for today's price
    # (referral_fee / price), not a single tier's rate -- there isn't
    # one "the" rate to show once a price crosses a tier boundary.
    referral_rate_used: float
    uk_vat_rate_used: float
    eu_vat_rate_used: float


class FeeEngine:

    # Fallback only -- used when Keepa hasn't returned real FBA fee
    # data for this ASIN (see KeepaParser.fba_fee).
    DEFAULT_FBA_FEE = 3.48

    # "Worth the extra manual effort" ROI bar for an OA lead --
    # deliberately mirrors ProductRepository.is_notable's own >25% ROI
    # bar (not the looser 10% OpportunityEngine.MIN_VIABLE_ROI floor,
    # which just keeps something out of IGNORE) so "a price worth
    # chasing in-store" means the same thing here as "notable"
    # everywhere else in Atlas. Raised from 18% to 25% -- 18% wasn't a
    # high enough bar for what counts as a genuinely good lead.
    OA_TARGET_ROI_PCT = 25.0

    @staticmethod
    def _referral_fee(price: float, category_key: str) -> float:
        """
        Portion-based referral fee -- if the category has tiered bands
        (REFERRAL_FEE_TIERS_BY_CATEGORY_NAME), each band of the price
        is charged at ITS OWN rate (like a tax bracket: e.g. "8% up to
        £20, 15% above" charges 8% on the first £20 and 15% only on
        the remainder), not the whole price at whichever rate the top
        of it falls into. Falls back to the flat
        REFERRAL_RATE_BY_CATEGORY_NAME/DEFAULT_REFERRAL_RATE for every
        category with no tier entry. Always floored at
        MINIMUM_REFERRAL_FEE_GBP, matching Amazon's own per-unit
        minimum referral fee.
        """
        tiers = REFERRAL_FEE_TIERS_BY_CATEGORY_NAME.get(category_key)

        if not tiers:
            rate = REFERRAL_RATE_BY_CATEGORY_NAME.get(category_key, DEFAULT_REFERRAL_RATE)
            fee = price * rate
        else:
            fee = 0.0
            floor = 0.0
            for ceiling, rate in tiers:
                band_top = price if ceiling is None else min(price, ceiling)
                band_amount = max(0.0, band_top - floor)
                fee += band_amount * rate
                if ceiling is None or price <= ceiling:
                    break
                floor = ceiling

        return round(max(fee, MINIMUM_REFERRAL_FEE_GBP), 2)

    @staticmethod
    def roi_at_price(price: float, cost_gross: float, category_name: str, fba_fee: float,
                      eu_vat_rate: float = UK_VAT_STANDARD_RATE) -> float:
        """
        ROI at an arbitrary UK sale price, against a fixed cost --
        the single-price core that calculate() already runs three
        times (today/90d-avg/90d-peak) for a Product, pulled out here
        so VerdictService can run the same real fee/VAT math against
        ~90 individual daily prices (see
        KeepaParser.daily_buy_box_prices) without duplicating it, for
        "how many of the last 90 days would this have been at a
        viable price" -- something the min/max/avg stats fields alone
        can't answer.

        Deliberately mirrors calculate()'s profit/ROI formula exactly
        (VAT-exclusive net revenue/cost for profit, GROSS cost_gross
        as ROI's denominator -- see calculate()'s own comments for
        why) rather than calling calculate() itself, since calculate()
        is built around a full Product object's today/90d/peak trio,
        not one arbitrary price.
        """
        if not price or not cost_gross:
            return 0.0

        category_key = category_name.lower()
        uk_vat_rate = 0.0 if category_key in UK_VAT_ZERO_RATED_CATEGORY_NAMES else UK_VAT_STANDARD_RATE
        effective_fba_fee = fba_fee if fba_fee else FeeEngine.DEFAULT_FBA_FEE
        referral_fee = FeeEngine._referral_fee(price, category_key)

        net_revenue = price / (1 + uk_vat_rate)
        net_cost = cost_gross / (1 + eu_vat_rate)

        profit = net_revenue - effective_fba_fee - referral_fee - PREP_FEE_GBP - net_cost

        return round((profit / cost_gross) * 100, 2)

    @staticmethod
    def max_source_cost(buy_box_now: float, category_name: str, fba_fee: float,
                         target_roi_pct: float) -> float:
        """
        Inverse of calculate(): given today's UK sale price and
        category (for referral fee + VAT treatment) and a target ROI,
        works out the highest GROSS price you could pay for stock and
        still hit that ROI. Built for the Competitors OA tab, where
        there's no cost yet to price against -- finding one is the
        whole point of going and looking -- only a UK sale price to
        work backwards from.

        Deliberately recomputes referral fee and VAT treatment fresh
        from buy_box_now/category_name rather than trusting a
        ProductRecord's stored referral_fee/uk_vat_rate_used columns:
        those are left at 0 (never calculated) for the lightweight
        "Step 5b" records BrandScanService saves for OA/unclear leads
        that were filtered out before a real EU cost was ever priced
        against -- see brand_scan_service.py's Step 5b comment. Only
        buy_box_now, category_name and fba_fee are trustworthy on
        those records.

        Assumes the OA purchase is a normal UK VAT-inclusive retail
        price with a reclaimable VAT receipt -- same assumption Atlas
        already makes about every other UK-domestic cost.

        Returns 0.0 if Amazon's own fees already exceed the sale
        price, i.e. no purchase price, however low, would be viable.
        """
        category_key = category_name.lower()
        uk_vat_rate = 0.0 if category_key in UK_VAT_ZERO_RATED_CATEGORY_NAMES else UK_VAT_STANDARD_RATE
        effective_fba_fee = fba_fee if fba_fee else FeeEngine.DEFAULT_FBA_FEE
        referral_fee = FeeEngine._referral_fee(buy_box_now, category_key)

        net_revenue = buy_box_now / (1 + uk_vat_rate)
        headroom = net_revenue - effective_fba_fee - referral_fee - PREP_FEE_GBP

        if headroom <= 0:
            return 0.0

        denominator = (target_roi_pct / 100) + (1 / (1 + uk_vat_rate))
        return round(headroom / denominator, 2) if denominator else 0.0

    @staticmethod
    def _max_source_cost_for_net_profit(buy_box_now: float, category_name: str, fba_fee: float,
                                         target_profit_gbp: float) -> float:
        """
        Shared core for max_source_cost_for_margin/max_source_cost_for_profit
        below -- both ultimately ask "what's the highest GROSS cost that
        still leaves at least this much NET profit at this sale price",
        just expressed as a % of the price (margin) or a flat £ figure
        (the absolute profit floor). Same VAT-inclusive OA assumption as
        max_source_cost, which this deliberately mirrors rather than
        reuses (that one solves for a target ROI -- a genuinely different
        equation, not a special case of this one).
        """
        category_key = category_name.lower()
        uk_vat_rate = 0.0 if category_key in UK_VAT_ZERO_RATED_CATEGORY_NAMES else UK_VAT_STANDARD_RATE
        effective_fba_fee = fba_fee if fba_fee else FeeEngine.DEFAULT_FBA_FEE
        referral_fee = FeeEngine._referral_fee(buy_box_now, category_key)

        net_revenue = buy_box_now / (1 + uk_vat_rate)
        headroom = net_revenue - effective_fba_fee - referral_fee - PREP_FEE_GBP
        net_cost_ceiling = headroom - target_profit_gbp

        if net_cost_ceiling <= 0:
            return 0.0

        return round(net_cost_ceiling * (1 + uk_vat_rate), 2)

    @staticmethod
    def max_source_cost_for_margin(buy_box_now: float, category_name: str, fba_fee: float,
                                    target_margin_pct: float) -> float:
        """
        Margin-target counterpart to max_source_cost -- solves for the
        highest GROSS cost that still clears a target MARGIN (profit as
        a % of the sale price) rather than a target ROI (profit as a %
        of cost). These are different questions with different answers
        at the same sale price (see FeeResult.margin's docstring for why
        margin and ROI diverge) -- added 2026-08-23 alongside the
        confirmed 13% margin floor (OpportunityEngine.MIN_VIABLE_MARGIN_PCT),
        since a cost ceiling that only satisfies a target ROI can still
        leave margin below its own floor for a lower-cost/higher-price
        product. Callers wanting a ceiling that respects BOTH floors
        together should take the min() of this and max_source_cost's
        result -- each is independently monotonic in cost, so the lower
        of the two is the genuine combined answer.

        Returns 0.0 if Amazon's own fees already exceed the sale price.
        """
        target_profit_gbp = buy_box_now * (target_margin_pct / 100)
        return FeeEngine._max_source_cost_for_net_profit(buy_box_now, category_name, fba_fee, target_profit_gbp)

    @staticmethod
    def max_source_cost_for_profit(buy_box_now: float, category_name: str, fba_fee: float,
                                    target_profit_gbp: float) -> float:
        """
        Absolute-£-profit-target counterpart to max_source_cost/
        max_source_cost_for_margin -- solves for the highest GROSS cost
        that still leaves at least target_profit_gbp of actual cash
        profit, regardless of what that is as a % of cost or price.
        Added 2026-08-23 alongside the confirmed £2 absolute profit
        floor (OpportunityEngine.MIN_VIABLE_PROFIT_GBP) -- catches a
        cheap item that clears both the ROI and margin bars on pennies
        of real profit (see MIN_VIABLE_PROFIT_GBP's own docstring).

        Returns 0.0 if Amazon's own fees already exceed the sale price.
        """
        return FeeEngine._max_source_cost_for_net_profit(buy_box_now, category_name, fba_fee, target_profit_gbp)

    @staticmethod
    def calculate(product: Product, category_name: str = "") -> FeeResult:

        fba_fee = product.fba_fee if product.fba_fee else FeeEngine.DEFAULT_FBA_FEE

        category_key = category_name.lower()
        uk_vat_rate = 0.0 if category_key in UK_VAT_ZERO_RATED_CATEGORY_NAMES else UK_VAT_STANDARD_RATE
        eu_vat_rate = EU_VAT_RATE_BY_MARKETPLACE.get(product.best_source_marketplace, UK_VAT_STANDARD_RATE)

        # Today's price
        referral_fee = FeeEngine._referral_fee(product.buy_box_now, category_key)
        referral_rate_used = round(referral_fee / product.buy_box_now, 4) if product.buy_box_now else DEFAULT_REFERRAL_RATE

        profit = 0.0
        roi = 0.0
        margin = 0.0

        if product.best_source_cost_gbp:
            # Amazon's displayed prices (UK and EU alike) are VAT-inclusive
            # consumer prices. As a VAT-registered seller (Standard VAT
            # accounting), the user remits output VAT on the UK sale price
            # and reclaims input VAT on the EU cost price -- so true
            # revenue/cost for a PROFIT calc are the VAT-EXCLUSIVE amounts,
            # not the raw gross prices. Referral fee itself stays computed
            # on the GROSS sale price above, matching how Amazon actually
            # calculates it (a percentage of the total price the buyer
            # pays); FBA fee and the prep fee are treated as VAT-neutral
            # (their VAT is reclaimed as input VAT / under the UK reverse
            # charge, net zero cash effect) -- neither is adjusted here.
            net_revenue = product.buy_box_now / (1 + uk_vat_rate)
            net_cost = product.best_source_cost_gbp / (1 + eu_vat_rate)

            profit = round(
                net_revenue - fba_fee - referral_fee - PREP_FEE_GBP - net_cost,
                2,
            )
            # ROI, unlike profit, is measured against the GROSS amount
            # actually paid at purchase (product.best_source_cost_gbp,
            # VAT included) rather than the VAT-exclusive net_cost used
            # above -- matches SAS and how most sellers think about
            # ROI (the real capital tied up today), even though the
            # VAT portion comes back later via the VAT return. Using
            # net_cost here (a smaller denominator) would mechanically
            # inflate ROI% relative to that -- confirmed as the main
            # driver of a real Atlas-vs-SAS ROI mismatch report.
            roi = round(
                (profit / product.best_source_cost_gbp) * 100, 2
            ) if product.best_source_cost_gbp else 0.0
            # Margin against the GROSS sale price (what the listing
            # actually shows/what the customer paid), NOT net_revenue
            # (VAT-exclusive) -- see the 2026-08-19 fix note below.
            margin = round(
                (profit / product.buy_box_now) * 100, 2
            ) if product.buy_box_now else 0.0

        # 90-day typical price -- catches opportunities where today's
        # price is temporarily discounted but the product is normally
        # profitable. Referral fee is recalculated too, since it's a
        # percentage of whichever sale price is being used.
        profit_90d = 0.0
        roi_90d = 0.0
        margin_90d = 0.0

        if product.best_source_cost_gbp and product.buy_box_90d:
            referral_fee_90d = FeeEngine._referral_fee(product.buy_box_90d, category_key)
            net_revenue_90d = product.buy_box_90d / (1 + uk_vat_rate)
            net_cost = product.best_source_cost_gbp / (1 + eu_vat_rate)

            profit_90d = round(
                net_revenue_90d
                - fba_fee
                - referral_fee_90d
                - PREP_FEE_GBP
                - net_cost,
                2,
            )
            roi_90d = round(
                (profit_90d / product.best_source_cost_gbp) * 100, 2
            )
            margin_90d = round(
                (profit_90d / product.buy_box_90d) * 100, 2
            ) if product.buy_box_90d else 0.0

        # 90-day PEAK price -- catches the opposite case from
        # profit_90d: a product whose price swings between a low and a
        # high (rather than sitting near one number), where the
        # AVERAGE washes out a recurring high-price window that could
        # genuinely be sold into. Riskier by nature (you have to catch
        # the price at its high, not just any day) -- kept as its own
        # figure rather than blended into profit/profit_90d anywhere,
        # so OpportunityEngine can gate it separately (see PEAK_WINDOW).
        profit_peak = 0.0
        roi_peak = 0.0
        margin_peak = 0.0

        if product.best_source_cost_gbp and product.buy_box_max_90d:
            referral_fee_peak = FeeEngine._referral_fee(product.buy_box_max_90d, category_key)
            net_revenue_peak = product.buy_box_max_90d / (1 + uk_vat_rate)
            net_cost = product.best_source_cost_gbp / (1 + eu_vat_rate)

            profit_peak = round(
                net_revenue_peak
                - fba_fee
                - referral_fee_peak
                - PREP_FEE_GBP
                - net_cost,
                2,
            )
            roi_peak = round(
                (profit_peak / product.best_source_cost_gbp) * 100, 2
            )
            margin_peak = round(
                (profit_peak / product.buy_box_max_90d) * 100, 2
            ) if product.buy_box_max_90d else 0.0

        return FeeResult(
            referral_fee=referral_fee,
            fba_fee=fba_fee,
            prep_fee=PREP_FEE_GBP,
            profit=profit,
            roi=roi,
            margin=margin,
            profit_90d=profit_90d,
            roi_90d=roi_90d,
            margin_90d=margin_90d,
            profit_peak=profit_peak,
            roi_peak=roi_peak,
            margin_peak=margin_peak,
            referral_rate_used=referral_rate_used,
            uk_vat_rate_used=uk_vat_rate,
            eu_vat_rate_used=eu_vat_rate,
        )
