from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Product:
    asin: str
    title: str
    brand: str
    category: str

    # First EAN Keepa has on file, or "" -- reference/display only,
    # see KeepaParser.ean for why this isn't used to drive OA search.
    ean: str = ""

    # Full URL to the primary product image, or "" -- see
    # KeepaParser.image (2026-09-04). Display only.
    image: str = ""

    # UK selling data
    buy_box_now: float = 0.0
    buy_box_90d: float = 0.0

    # Lowest buy-box price seen in the last 90 days -- used by
    # SourcingClassifier's UK A2A dip-and-recovery check. See
    # KeepaParser.buy_box_min_90d for why this is NOT the same as an
    # all-time low.
    buy_box_min_90d: float = 0.0

    # Highest buy-box price seen in the last 90 days -- the peak
    # counterpart to buy_box_min_90d, used by OpportunityEngine's
    # PEAK_WINDOW recommendation for products whose price swings
    # between a low and a high rather than sitting near one number
    # (e.g. B08QNBGL6Q-style leads). See KeepaParser.buy_box_max_90d.
    buy_box_max_90d: float = 0.0

    offers_now: int = 0
    offers_90d: int = 0

    sales_rank_now: int = 0
    sales_rank_90d: int = 0

    sales_drops_30d: int = 0

    # Downward price moves in the last 90 days -- informational only
    # (shown on the Verdict Checker page). NOT used to gate
    # PEAK_WINDOW any more (see peak_viable_days_90d below for why) --
    # a count of ANY price move, at any level, is a weak proxy for
    # "the peak genuinely recurs". See KeepaParser.price_drop_count.
    price_drop_count_90d: int = 0

    # How many of the last 90 days would ACTUALLY have cleared a
    # viable ROI at TODAY's best EU source cost -- the real evidence
    # OpportunityEngine's PEAK_WINDOW recommendation gates on
    # (2026-08-20), replacing the old price_drop_count_90d proxy.
    # Computed day-by-day (re-running the real ROI formula against
    # each of the last 90 reconstructed daily UK prices), not from a
    # single min/max/avg stat -- see
    # SourcingClassifier.compute_peak_window_evidence for why "the
    # 90-day peak recurs" needs to mean "was profitable on more than
    # a handful of real days", not "the price moved around". 0 if
    # there's no EU source to price against at all.
    peak_viable_days_90d: int = 0

    # How many of the last 90 days would have cleared FeeEngine.
    # OA_TARGET_ROI_PCT (25%, the SAME bar ProductRepository.is_notable
    # already uses) at today's EU source cost -- and priced_days_90d,
    # the number of those 90 days that actually had a real price to
    # check at all (see SourcingClassifier.compute_peak_window_evidence,
    # 2026-09-04). Distinct from peak_viable_days_90d above, which uses
    # the looser 17% MIN_VIABLE_ROI floor for a different purpose
    # (PEAK_WINDOW eligibility) -- this one answers "how often would
    # THIS have actually looked like a real, notable buy", the
    # recurrence evidence behind a PRICE_DROP_BUY_NOW risk flag. 0/0 if
    # there's no EU source to price against.
    days_at_25pct_roi_90d: int = 0
    priced_days_90d: int = 0

    # "The last time offers spiked (SourcingClassifier.
    # compute_competition_spike_evidence, 2026-09-04), what did price
    # actually do afterward" -- days_since is how long ago that spike
    # was, price_change_pct is the real % move in buy-box price from
    # that spike day to the latest priced day (positive = price rose
    # since, negative = fell). Both None (not 0) when the 90-day window
    # never saw a genuine spike at all -- "never spiked" is real
    # information, distinct from "spiked and price didn't move".
    days_since_last_competition_spike: int | None = None
    price_change_since_competition_spike_pct: float | None = None

    # The mirror question (SourcingClassifier.
    # compute_price_drop_offer_context, 2026-09-04): the last time price
    # genuinely dipped, how far were offers from their own 90-day
    # average on that same day. Positive = offers were elevated when
    # the price dropped (suggests competition-driven); negative/near
    # zero = offers were normal or low then (suggests the drop wasn't
    # about competition). None when no dip was found in the window.
    days_since_last_price_dip: int | None = None
    offers_change_at_last_price_dip_pct: float | None = None

    # Keepa's confirmed monthly sales count (their "monthlySold" stat,
    # based on actual Amazon sales data, not an estimate). 0 means
    # Keepa has no confirmed sales data for this product -- not
    # necessarily that it doesn't sell, just that Amazon hasn't
    # published a figure for it.
    monthly_sales: int = 0

    # When Keepa last updated monthly_sales -- can be well in the past
    # (e.g. a stale "January 2026" figure), since Amazon doesn't
    # republish this every month for every listing. None if Keepa has
    # never had a confirmed figure at all.
    monthly_sales_as_of: datetime | None = None

    # Marketplace buy prices
    uk_cost: float = 0.0
    fr_cost: float = 0.0
    de_cost: float = 0.0
    it_cost: float = 0.0
    es_cost: float = 0.0

    # Amazon fees
    fba_fee: float = 0.0
    referral_fee: float = 0.0

    # Calculated values (based on today's UK price)
    profit: float = 0.0
    roi: float = 0.0

    # Profit as a % of the GROSS sale price (what's actually on the
    # Amazon listing) -- a different question from ROI (profit as a %
    # of what you PAID). On a high-ticket item, a great ROI can still
    # mean a thin sliver of the sale price is actually profit -- more
    # exposure if the price drops, gets returned, or a fee estimate is
    # slightly off. Shown alongside ROI everywhere, never replacing it
    # -- see FeeEngine. Was computed against the VAT-exclusive net
    # revenue instead of the gross price until 2026-08-19 (see
    # FeeResult.margin's docstring) -- inflated every figure ~20%
    # relative on standard-rated categories.
    margin: float = 0.0

    # Same calculation but using the 90-day average UK price instead of
    # today's -- catches opportunities where today's price is
    # temporarily discounted (e.g. by Amazon itself) but the product
    # is normally profitable. Never hidden/discounted just because
    # today's number looks worse -- see is_excluded ceiling check and
    # the Discovery page's profitable filter, both of which use
    # whichever of profit/profit_90d is better.
    profit_90d: float = 0.0
    roi_90d: float = 0.0
    margin_90d: float = 0.0

    # Same calculation again, but using the 90-day PEAK UK price
    # (buy_box_max_90d) instead of today's or the average. Only
    # meaningful for OpportunityEngine's PEAK_WINDOW tier -- a
    # riskier lead that's only profitable during Amazon's higher-price
    # stretches, never blended into profit/roi or profit_90d/roi_90d.
    profit_peak: float = 0.0
    roi_peak: float = 0.0
    margin_peak: float = 0.0

    # Best A2A source found across DE/FR/IT/ES, cost already converted to GBP
    best_source_marketplace: str = ""
    best_source_cost_gbp: float = 0.0

    # The 90-day LOW of best_source_marketplace's own price (not the
    # cheapest across all 4 -- just whether THIS source has been
    # cheaper before), already converted to GBP. Used by
    # WatchlistService to auto-watch products that aren't profitable
    # at today's EU cost but would be at their recent low -- a real,
    # recurring spread rather than one that never existed. 0.0 if no
    # EU source was found at all.
    best_source_cost_min_90d_gbp: float = 0.0

    # ---- Recent-window sourcing evidence (SourcingClassifier) ----
    # A day-by-day reconstruction over the last
    # SourcingClassifier.RECENT_WINDOW_DAYS calendar days -- built to
    # judge whether a competitor's newly-detected listing was plausibly
    # sourced EU A2A / UK A2A around the time it likely entered their
    # inventory, not "was this ever true at some point in the wider
    # 90-day window" (which best_source_cost_min_90d_gbp/buy_box_min_90d
    # above answer, and which WatchlistService still uses as-is -- these
    # new fields are additive, not a replacement for those). Computed in
    # BrandScanService.scan (the one place both the raw UK/EU Keepa
    # history and the resolved category/fee context are in scope at
    # once) via SourcingClassifier.compute_recent_evidence -- see there
    # for how each is derived. 0 for every field if a product has no EU
    # source at all (nothing to compare against).

    # How many of the recent-window days had both a real UK price and a
    # real EU source price to compare -- i.e. were actually priceable.
    #
    # 2026-09-03 (atlas-competitor-watch-classification-v1.md's
    # follow-up fix): these eu_source_* fields now describe the BEST
    # evidence found across ALL FOUR EU marketplaces (DE/FR/ES/IT),
    # not just whichever one happens to be best_source_marketplace
    # (today's cheapest) -- see SourcingClassifier.compute_recent_
    # evidence and eu_source_evidence_by_marketplace below for the
    # full per-marketplace breakdown these are summarized from.
    eu_source_priced_days_recent: int = 0

    # Of those priced days, how many cleared a genuinely viable margin
    # (that day's real UK price against that day's real EU cost).
    eu_source_viable_days_recent: int = 0

    # Best ROI actually achievable on any single recent-window day,
    # across any of the 4 EU marketplaces.
    eu_source_best_roi_recent: float = 0.0

    # WHICH EU marketplace produced eu_source_best_roi_recent -- may
    # differ from best_source_marketplace (today's cheapest), which is
    # exactly the point: a competitor's historical sourcing evidence
    # and today's live buying opportunity are two separate questions.
    eu_source_best_roi_marketplace: str = ""

    # Full per-marketplace breakdown -- {"DE": {"viable_days": int,
    # "best_roi": float, "best_buy_price": float, "best_date": str},
    # "ES": {...}, ...} -- only marketplaces with at least one genuinely
    # viable day in the window are included. This is what lets DE
    # evidence and ES evidence be tracked/preserved independently (see
    # SourcingClassifier.merge_evidence) rather than only the single
    # strongest one surviving.
    eu_source_evidence_by_marketplace: dict = field(default_factory=dict)

    # Real gap found live, 2026-09-07 (Tamara, re: B076H61X15: "I can
    # see that this is for sale in other EU countries ... even if it
    # has never been profitable this is things I want to see and I
    # want the cheapest price in the last 30 days and which country").
    # eu_source_evidence_by_marketplace above only records a market
    # once it clears a VIABLE margin -- a market that was genuinely
    # checked and had a real price, just never a profitable one,
    # produces NO entry there at all, which is exactly what made this
    # ASIN's real (if unprofitable) Italian price invisible. These
    # three describe the single cheapest EU price seen on ANY day in
    # the window, on ANY checked marketplace, regardless of viability --
    # 0.0/""/"" when no EU marketplace had a price at all.
    eu_cheapest_price_recent_gbp: float = 0.0
    eu_cheapest_price_recent_marketplace: str = ""
    eu_cheapest_price_recent_date: str = ""

    # Which of DE/FR/ES/IT actually had a Keepa fetch attempted for
    # this ASIN this scan (see BrandScanService's top-up pass) --
    # distinct from eu_source_evidence_by_marketplace's keys, which
    # only include markets that found something. A market missing from
    # THIS list was never even checked (e.g. Keepa token budget ran
    # out) -- surfaced so "OA / unclear" can be told apart from
    # "OA / unclear, but not every market was actually checked".
    eu_markets_checked: list = field(default_factory=list)

    # How many recent-window days had the UK price down at/near a
    # genuine dip (see SourcingClassifier.DIP_THRESHOLD) relative to
    # the 90-day average.
    uk_dip_days_recent: int = 0

    # Lowest UK buy-box price actually seen in the recent window.
    uk_price_min_recent: float = 0.0

    # Calendar date (ISO "YYYY-MM-DD") that uk_price_min_recent was
    # actually seen on -- "" if there's no priced day at all. Together
    # with uk_price_min_recent, this is the UK A2A "best guess" of
    # when/what a competitor likely bought: the day the UK price was
    # lowest is the day buying at that dip (expecting recovery) would
    # have made most sense.
    uk_price_min_recent_date: str = ""

    # The GBP-converted EU cost on the SAME day eu_source_best_roi_recent
    # was achieved (not just the ROI figure itself) -- the EU A2A "best
    # guess" purchase price. 0.0 if there's no viable EU day at all.
    eu_source_best_roi_cost_gbp: float = 0.0

    # Calendar date (ISO "YYYY-MM-DD") of the day eu_source_best_roi_recent
    # was achieved -- the EU A2A "best guess" purchase date (the day
    # with the strongest margin is the day buying would have made most
    # sense). "" if there's no viable EU day at all.
    eu_source_best_roi_date: str = ""

    # True if brand (or brand+category) is currently on the gated-
    # brands list (see app/config/exclusions.py's is_gated() and
    # app/database/models.py's GatedBrand) -- set in
    # BrandScanService.scan Step 3 for an incidentally-discovered ASIN
    # (Competitor Watch, an uploaded list, Replen, a Scan Queue rescan
    # of an already-tracked ASIN) that wasn't blocked earlier because
    # it wasn't found via a brand-name search. Read by
    # OpportunityEngine.analyse to force recommendation="GATED"
    # regardless of how good the underlying numbers look -- the
    # product still gets fully priced/scored (so the Gated Brand
    # Opportunities page can show a genuine ROI/score for building an
    # ungating case), it just never surfaces as an actionable BUY/
    # CONSIDER anywhere else (see ProductRepository.is_notable).
    gated: bool = False

    hazmat: bool = False
    adult: bool = False

    # Human-readable category name (resolved from Keepa's numeric
    # `category` root ID) and the referral/VAT rates FeeEngine actually
    # applied when computing profit/roi -- kept for display/audit so a
    # given ROI number can be explained, not just trusted. See
    # app/config/fees.py and FeeEngine.calculate.
    category_name: str = ""
    referral_rate_used: float = 0.0
    uk_vat_rate_used: float = 0.0
    eu_vat_rate_used: float = 0.0