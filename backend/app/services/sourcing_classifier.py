import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.models.product import Product
from app.keepa.parser import KeepaParser
from app.services.fee_engine import FeeEngine
from app.services.currency_service import CurrencyService
from app.services.product_mapper import MARKETPLACE_CURRENCY

# How many of the most recent calendar days to judge EU A2A / UK A2A
# evidence against. Competitor Watch detects a listing at roughly the
# time it was ADDED to the seller's inventory, so evidence needs to be
# recent to actually explain THIS listing -- a margin or dip from
# months ago says nothing about why it showed up now.
#
# 30 days (atlas-competitor-watch-classification-v1.md section 7,
# 2026-09-03 -- was 10 days until this change). A real, credible A2A
# opportunity 15-20 days ago was being missed entirely under the old
# 10-day window (confirmed: the spec's own worked examples all fall
# in the 11-20-day range) and misread as OA/unclear. Widening this
# costs no extra Keepa tokens -- ProductService.get_products already
# fetches full price-change history (history=True), so
# KeepaParser.daily_buy_box_prices is just reading further back into
# data that was already being pulled.
#
# IMPORTANT: this widens the window used to compute the CURRENT
# classification each time SourcingClassifier.classify() runs -- it
# does NOT by itself stop a real EU/UK A2A finding from eventually
# aging out of a later reclassify's 30-day window and reading as
# OA/unclear again. That's expected/correct (see classify()'s own
# docstring) -- what must never happen is losing the ORIGINAL
# evidence when that happens. See merge_evidence() below for how
# that's actually preserved, in SellerNewListing.sourcing_reasoning_json,
# independently of whatever the current sourcing_tag says.
RECENT_WINDOW_DAYS = 30

# "Would have been worth buying" bar for a single recent-window day's
# EU-sourced margin. Mirrors OpportunityEngine.MIN_VIABLE_ROI (the
# app's absolute floor, raised 10%->17% 2026-08-23) rather than
# is_notable's stronger 25% bar -- this is asking "is there real
# evidence a purchase like this could have happened", not "is this a
# great lead".
RECENT_VIABLE_ROI_PCT = 17.0

# How far below its 90-day average a price has to have fallen to count
# as a genuine "dip" (as opposed to normal day-to-day noise). Named
# constant, not inlined, so it's easy to recalibrate later against real
# detection outcomes -- same spirit as the existing ROI-cutoff
# recalibration note in exclusions.py.
DIP_THRESHOLD = 0.70

# How close back to its 90-day average the CURRENT price needs to be
# to count as "recovered" from a past dip. Below this, the price is
# still considered near the dip -- i.e. a live opportunity right now,
# not just historical evidence of one.
RECOVERY_THRESHOLD = 0.90

# Listings with this many or fewer current new offers are treated as
# having a "small, stable seller count" -- one of the structural
# signals for a likely wholesale/standing-supply relationship rather
# than an opportunistic A2A find.
WHOLESALE_MAX_SELLERS = 3

# How many PRIOR detections of the same brand from the same tracked
# seller count as "recurring" for the wholesale signal (i.e. this is
# at least the Nth time we've seen them list this brand).
WHOLESALE_BRAND_REPEAT_THRESHOLD = 2

MULTIPACK_PATTERN = re.compile(
    r"\b\d+\s*[- ]?\s*(?:pack|pk|ct|count)\b|\bpack of \d+\b|\bcase of \d+\b|\bmulti[- ]?pack\b",
    re.IGNORECASE,
)


@dataclass
class SourcingClassification:
    sourcing_tag: str
    # JSON-serializable -- the numbers behind the tag, so the "why" is
    # inspectable rather than just the label. See SellerNewListing.sourcing_reasoning_json.
    reasoning: dict = field(default_factory=dict)

    # This round's raw EU A2A / UK A2A evidence, independent of which
    # one actually won as the primary sourcing_tag -- None when that
    # check found nothing this round. Exists purely so a caller (see
    # merge_evidence below) can archive BOTH kinds of evidence found
    # this round, not just whichever is reflected in `reasoning`/
    # `sourcing_tag` above (e.g. when both EU and UK evidence exist,
    # `reasoning` above only carries EU's, per classify()'s own
    # precedence). eu_evidence is the FULL per-marketplace dict (see
    # Product.eu_source_evidence_by_marketplace) -- possibly covering
    # more than one qualifying EU marketplace at once (e.g. DE and ES
    # both), not just whichever one is "primary" in `reasoning` above.
    eu_evidence: dict | None = None
    uk_evidence: dict | None = None

    # NOTE (2026-09-03, atlas-competitor-watch-classification-v1.md):
    # there is deliberately NO currently_buyable field here any more.
    # "Can we buy this right now" is a completely separate question
    # from "how did the competitor likely source it" -- it's already
    # answered by the existing OpportunityEngine/ProductRecord.
    # recommendation for whatever the CURRENT best source actually is,
    # which may be a different marketplace entirely from whatever
    # justified this historical classification. Computing a second,
    # SourcingClassifier-owned buyability signal here would silently
    # duplicate that (and could disagree with it) -- see
    # SellerWatchService._persist_classification, the one place
    # currently_buyable actually gets set now, reading it straight off
    # the linked ProductRecord instead.


class SourcingClassifier:
    """
    Guesses how a competitor likely SOURCED a newly-detected listing,
    from data the standard pipeline has already fetched (UK stats=90,
    EU current price + stats=90 -- see BrandScanService.scan, which
    confirmed EU's stats=90 costs nothing extra on top of history/
    buybox) -- no new Keepa tokens spent.

    IMPORTANT FRAMING (per spec): Keepa has no visibility into a
    seller's actual purchase history. EU A2A and UK A2A are inferred
    from a real, RECENT (last RECENT_WINDOW_DAYS days -- see there)
    price spread or dip -- reasonably solid evidence that a purchase
    like this was plausible around when the listing was actually
    detected, not just "this could currently be found the same way" or
    "this was true at some unspecified point in a 90-day window", both
    of which can attribute EU A2A to a listing that was never actually
    profitable from EU anywhere near when it was likely bought.
    Wholesale (likely) and OA / unclear are structural inferences only
    (offer count, title pattern, listing history) and should be
    labelled as a best guess in the UI, not a confirmed sourcing
    method.

    classify() and the _check_*/​_fallback_* methods are pure functions
    of an already-built Product (no DB access, no raw Keepa access) --
    easy to unit-test against constructed Product fixtures. The one
    exception is compute_recent_evidence(), which DOES need the raw UK/
    EU Keepa dicts (for day-by-day price reconstruction) and is called
    separately, once, by BrandScanService.scan right after FeeEngine.
    calculate -- the one place both that raw history and the resolved
    category/fee context are in scope together. Its output is written
    onto the Product itself (see Product's own field docstrings) before
    classify() ever runs, keeping classify() itself simple and testable.
    The caller (SellerWatchService) is responsible for looking up
    brand_repeat_count from seller_new_listings history, since that's
    the one signal that genuinely needs cross-record DB context.
    """

    @staticmethod
    def compute_recent_evidence(uk_raw: dict, eu_products: dict, product: Product,
                                 category_name: str) -> dict:
        """
        Builds the day-by-day recent-window fields consumed by
        _check_eu_a2a/_check_uk_a2a (see Product's own field docstrings
        for what each one means). Returns a plain {field_name: value}
        dict meant to be applied directly onto the Product being built,
        e.g. `for k, v in evidence.items(): setattr(product, k, v)` --
        same "caller assembles Product" convention FeeEngine.calculate's
        FeeResult already uses.

        eu_products: {"DE": raw_keepa_dict_or_None, "FR": ..., "ES": ...,
        "IT": ...} -- ALL FOUR EU marketplaces (2026-09-03,
        atlas-competitor-watch-classification-v1.md's follow-up fix --
        this used to only receive product.best_source_marketplace's own
        data). The caller (BrandScanService.scan) already fetches all
        four to pick TODAY's cheapest source (see ProductMapper.
        from_keepa_multi) -- passing the whole dict here costs NO extra
        Keepa calls, it's data already in scope. Historical evidence is
        examined INDEPENDENTLY per marketplace, because a real
        opportunity a competitor could have sourced from is not
        necessarily the marketplace that happens to be cheapest today
        (or ANY marketplace at all today -- see _check_eu_a2a's own
        docstring for why the old best_source_marketplace guard was
        removed). A marketplace with no data (None) is simply skipped,
        same "no real data, don't guess" convention as everywhere else
        in this file.

        product must already have fba_fee/eu_vat_rate_used/buy_box_90d
        set (i.e. called AFTER FeeEngine.calculate has been applied to
        it), since the ROI math here needs real fee/VAT context, not
        defaults.

        Also identifies, for the "best guess" shown on the Competitors
        page -- the single day within the window that best explains a
        likely purchase: the lowest UK price (for UK A2A -- buying at
        the dip) and, independently for EACH EU marketplace, the day
        with the strongest EU margin (for EU A2A -- the day buying
        would have made most sense from THAT marketplace), each with
        its real calendar date and price. Keepa has no actual purchase
        record, so this is always an inference from price history, not
        a confirmed fact -- labelled as a "best guess" everywhere it's
        shown (see _check_eu_a2a/_check_uk_a2a and competitors.html).
        """
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=RECENT_WINDOW_DAYS)

        def date_for_index(i: int) -> str:
            return (window_start + timedelta(days=i)).date().isoformat()

        uk_daily = KeepaParser(uk_raw).daily_buy_box_prices(RECENT_WINDOW_DAYS)

        uk_price_min_recent = 0.0
        uk_price_min_recent_date = ""

        for i, p in enumerate(uk_daily):
            if p and (not uk_price_min_recent or p < uk_price_min_recent):
                uk_price_min_recent = p
                uk_price_min_recent_date = date_for_index(i)

        uk_dip_days_recent = 0
        if product.buy_box_90d:
            dip_cutoff = product.buy_box_90d * DIP_THRESHOLD
            uk_dip_days_recent = sum(1 for p in uk_daily if p and p <= dip_cutoff)

        # ---- EU side: independently, per marketplace ----------------
        eu_source_evidence_by_marketplace = {}

        best_marketplace = ""
        best_priced_days = 0
        best_viable_days = 0
        best_roi = None
        best_roi_date = ""
        best_roi_cost_gbp = 0.0

        for marketplace, eu_raw in (eu_products or {}).items():
            if not eu_raw:
                continue

            eu_daily = KeepaParser(eu_raw).daily_buy_box_prices(RECENT_WINDOW_DAYS)

            # Convert each day's EU cost to GBP before comparing it
            # against the (already-GBP) UK price -- eu_daily comes back
            # in the EU marketplace's own currency (EUR cents/100), and
            # FeeEngine.roi_at_price expects both price and cost_gross
            # in the same currency (see its own docstring).
            currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")

            priced_days = 0
            viable_days = 0
            market_best_roi = None
            market_best_roi_date = ""
            market_best_roi_cost_gbp = 0.0

            for i, (uk_price, eu_price_raw) in enumerate(zip(uk_daily, eu_daily)):
                if not uk_price or not eu_price_raw:
                    continue

                priced_days += 1
                eu_cost_gbp = CurrencyService.to_gbp(eu_price_raw, currency)

                roi = FeeEngine.roi_at_price(
                    uk_price, eu_cost_gbp, category_name, product.fba_fee, product.eu_vat_rate_used,
                )

                if roi >= RECENT_VIABLE_ROI_PCT:
                    viable_days += 1

                if market_best_roi is None or roi > market_best_roi:
                    market_best_roi = roi
                    market_best_roi_date = date_for_index(i)
                    market_best_roi_cost_gbp = eu_cost_gbp

            if viable_days:
                eu_source_evidence_by_marketplace[marketplace] = {
                    "viable_days": viable_days,
                    "best_roi": market_best_roi,
                    "best_buy_price": market_best_roi_cost_gbp,
                    "best_date": market_best_roi_date,
                }

            # Track the single strongest marketplace (by best ROI) to
            # populate the scalar eu_source_* fields below -- see
            # Product's own field docstrings: these now describe "the
            # best evidence found across all 4", not "today's cheapest
            # one's evidence".
            if market_best_roi is not None and (best_roi is None or market_best_roi > best_roi):
                best_marketplace = marketplace
                best_priced_days = priced_days
                best_viable_days = viable_days
                best_roi = market_best_roi
                best_roi_date = market_best_roi_date
                best_roi_cost_gbp = market_best_roi_cost_gbp

        return {
            "uk_dip_days_recent": uk_dip_days_recent,
            "uk_price_min_recent": uk_price_min_recent,
            "uk_price_min_recent_date": uk_price_min_recent_date,
            "eu_source_priced_days_recent": best_priced_days,
            "eu_source_viable_days_recent": best_viable_days,
            "eu_source_best_roi_recent": best_roi or 0.0,
            "eu_source_best_roi_cost_gbp": best_roi_cost_gbp,
            "eu_source_best_roi_date": best_roi_date,
            "eu_source_best_roi_marketplace": best_marketplace,
            "eu_source_evidence_by_marketplace": eu_source_evidence_by_marketplace,
        }

    @staticmethod
    def compute_peak_window_evidence(uk_raw: dict, product: Product, category_name: str) -> dict:
        """
        How many of the last 90 days would ACTUALLY have cleared a
        viable ROI at TODAY's best EU source cost -- built for
        OpportunityEngine's PEAK_WINDOW recommendation (2026-08-20).

        PEAK_WINDOW used to trust price_drop_count_90d (a count of ANY
        downward buy-box move, at ANY price level, see
        KeepaParser.price_drop_count) as evidence the 90-day peak price
        genuinely recurs rather than being a one-off spike. That's a
        weak proxy -- a product that just bounces around at LOW,
        unprofitable prices racks up plenty of "drops" with no
        connection to the peak at all, while the actual peak
        (buy_box_max_90d) may have been touched on a single day. The
        user's own framing: "one price spike doesn't mean this is
        potentially profitable... it needs to be at a profitable price
        for more than just a day in 90 days."

        This instead re-runs the real ROI formula (FeeEngine.roi_at_price)
        against every one of the last 90 reconstructed daily UK prices
        (see KeepaParser.daily_buy_box_prices) -- the same day-by-day
        technique VerdictService.compute_metrics already uses for a
        manually-priced ASIN, and compute_recent_evidence above already
        uses for the 10-day competitor-detection window. "The peak
        recurs" now means "was actually profitable on real days", not
        "the price moved around a lot".

        product must already have fba_fee/eu_vat_rate_used/
        best_source_cost_gbp set (called AFTER FeeEngine.calculate).
        Returns {"peak_viable_days_90d": 0} untouched if there's no EU
        source to price against at all -- nothing to reconstruct.
        """
        if not product.best_source_cost_gbp:
            return {"peak_viable_days_90d": 0}

        daily_prices = KeepaParser(uk_raw).daily_buy_box_prices(90)
        viable_days = 0

        for day_price in daily_prices:
            if not day_price:
                continue

            day_roi = FeeEngine.roi_at_price(
                day_price, product.best_source_cost_gbp, category_name,
                product.fba_fee, product.eu_vat_rate_used,
            )

            # RECENT_VIABLE_ROI_PCT mirrors OpportunityEngine.MIN_VIABLE_ROI
            # (same 17% "not even worth the risk" floor, see its own
            # docstring above) -- reused rather than duplicated again,
            # since it's exactly the same bar applied to a wider window.
            if day_roi >= RECENT_VIABLE_ROI_PCT:
                viable_days += 1

        return {"peak_viable_days_90d": viable_days}

    @staticmethod
    def classify(product: Product, brand_repeat_count: int = 0) -> SourcingClassification:
        eu = SourcingClassifier._check_eu_a2a(product)
        uk = SourcingClassifier._check_uk_a2a(product)

        # eu_evidence is the FULL per-marketplace breakdown (possibly
        # more than one qualifying EU marketplace), read straight off
        # the Product regardless of which single marketplace ends up
        # named in `reasoning`/`eu.reasoning` below -- see merge_evidence
        # for why archiving needs the complete picture, not just the
        # "primary" one classify() picked for display.
        eu_evidence = product.eu_source_evidence_by_marketplace or None
        uk_evidence = uk.reasoning if uk is not None else None

        if eu is not None and uk is not None:
            # Both have real recent evidence -- EU A2A wins as primary
            # (a priced cross-border margin is more specific evidence
            # of HOW they sourced it than a UK dip alone, which only
            # says "the price was briefly low", not who bought it or
            # from where), but the UK dip is genuine corroborating
            # context, not something to silently drop just because EU
            # also matched -- flattened onto EU's own reasoning dict so
            # it survives into the UI's generic key/value display (see
            # competitors.html) rather than needing template changes.
            eu.reasoning["uk_dip_also_present"] = True
            eu.reasoning["uk_dip_days_recent"] = uk.reasoning["uk_dip_days_recent"]
            eu.reasoning["uk_price_min_recent"] = uk.reasoning["uk_price_min_recent"]
            eu.reasoning["uk_price_min_recent_date"] = uk.reasoning["uk_price_min_recent_date"]
            result = eu
        elif eu is not None:
            result = eu
        elif uk is not None:
            result = uk
        else:
            wholesale = SourcingClassifier._check_wholesale(product, brand_repeat_count)
            result = wholesale if wholesale is not None else (
                SourcingClassifier._fallback_unclear(product, brand_repeat_count)
            )

        result.eu_evidence = eu_evidence
        result.uk_evidence = uk_evidence
        return result

    @staticmethod
    def merge_evidence(previous_reasoning: dict | None, classification: SourcingClassification) -> dict:
        """
        atlas-competitor-watch-classification-v1.md section 14 --
        "current classification should not destroy historical
        evidence". Builds the dict to actually persist as
        SellerNewListing.sourcing_reasoning_json: everything in
        classification.reasoning (today's CURRENT classification,
        exactly the same flat shape as before this change -- every
        existing template/key lookup against it keeps working
        unchanged) PLUS one new additive key, "historical_a2a_evidence",
        that is refresh-only and never cleared.

        previous_reasoning: the listing's PREVIOUSLY stored reasoning
        dict (already json.loads'd by the caller), or None/{} for a
        brand-new detection with nothing to preserve yet.

        EU side (2026-09-03 follow-up fix): eu_a2a is now a dict KEYED
        BY MARKETPLACE ({"DE": {...}, "ES": {...}}), not a single
        entry -- each marketplace's evidence is refreshed/preserved
        INDEPENDENTLY. Concretely: if this round found evidence in ES
        but not DE (DE's opportunity has since aged out of the window,
        or DE just isn't buyable today), ES gets set/refreshed while
        DE's previously-recorded entry is carried forward completely
        untouched -- DE is never removed just because ES is the story
        now, and vice versa. classification.eu_evidence is the FULL
        per-marketplace dict this round found (see Product.
        eu_source_evidence_by_marketplace) -- a marketplace simply
        absent from it this round is left alone, exactly like the
        single-entry rule below already does for "found nothing".

        UK side: single entry, same refresh-on-find/preserve-on-miss
        rule as before (there is only one UK marketplace, so no
        per-marketplace breakdown is meaningful there) --
        classification.uk_evidence not None -> set/refresh, stamping
        last_confirmed_at now (first_found_at carried forward from
        whatever was already stored, or now if this is the first
        time); None -> leave whatever was previously stored exactly as
        it was.

        Either way, "found nothing this round" NEVER erases what was
        already recorded -- that's what lets sourcing_tag flip freely
        back to "OA / unclear"/"Wholesale (likely)" as evidence ages
        out of the window (see classify()'s own docstring -- that
        reversal is correct, expected behaviour, NOT something this
        guards against) while the fact that a real EU/UK A2A
        opportunity was ever found stays visible indefinitely.

        Pure function, no DB access -- SellerWatchService (the only
        caller) owns reading the listing's existing
        sourcing_reasoning_json and writing the result back, same
        "SourcingClassifier stays a pure classifier, the caller owns
        persistence" split as the rest of this module.
        """
        previous = previous_reasoning or {}
        previous_evidence = previous.get("historical_a2a_evidence") or {}
        previous_eu_by_marketplace = previous_evidence.get("eu_a2a") or {}
        previous_uk = previous_evidence.get("uk_a2a")
        now_iso = datetime.now(timezone.utc).isoformat()

        merged_eu_by_marketplace = dict(previous_eu_by_marketplace)
        for marketplace, fresh in (classification.eu_evidence or {}).items():
            existing = previous_eu_by_marketplace.get(marketplace)
            entry = dict(fresh)
            entry["first_found_at"] = existing["first_found_at"] if existing else now_iso
            entry["last_confirmed_at"] = now_iso
            merged_eu_by_marketplace[marketplace] = entry

        if classification.uk_evidence is not None:
            merged_uk = dict(classification.uk_evidence)
            merged_uk["first_found_at"] = previous_uk["first_found_at"] if previous_uk else now_iso
            merged_uk["last_confirmed_at"] = now_iso
        else:
            merged_uk = previous_uk

        merged = dict(classification.reasoning)
        merged["historical_a2a_evidence"] = {
            "eu_a2a": merged_eu_by_marketplace,
            "uk_a2a": merged_uk,
        }
        return merged

    @staticmethod
    def _check_eu_a2a(product: Product) -> SourcingClassification | None:
        """
        Real evidence of a genuinely viable EU-sourced margin on at
        least one of the last RECENT_WINDOW_DAYS days, in ANY of
        DE/FR/ES/IT independently, computed day-by-day against THAT
        day's actual UK price and THAT day's actual EU cost (see
        compute_recent_evidence) -- not today's price, not a 90-day
        average, and not a single cheap EU day that could sit anywhere
        in a much wider 90-day window.

        IMPORTANT (2026-09-03 follow-up fix, atlas-competitor-watch-
        classification-v1.md): deliberately NOT gated on
        product.best_source_marketplace any more. That field is
        TODAY's live cheapest-marketplace pick (see ProductMapper.
        from_keepa_multi) -- a completely different, separate question
        from "did a real EU A2A opportunity exist in the last 30
        days". A competitor may have sourced from Germany 10 days ago;
        Germany can be unbuyable (or simply not the cheapest) today
        and that historical evidence must still count. This was
        confirmed as the actual cause of real false negatives: a
        blank best_source_marketplace (no EU marketplace currently has
        a qualifying Amazon/FBA-held buy box) used to block this whole
        check regardless of how strong the historical evidence was.

        There is deliberately no currently_buyable check here any
        more either -- "can we buy it right now" is answered
        separately and independently by the linked ProductRecord's own
        OpportunityEngine recommendation (see SourcingClassification's
        own docstring for why duplicating that here would be wrong).
        """
        if not product.eu_source_priced_days_recent:
            return None

        if not product.eu_source_viable_days_recent:
            return None

        return SourcingClassification(
            sourcing_tag="EU A2A",
            reasoning={
                "marketplace": product.eu_source_best_roi_marketplace,
                "recent_window_days": RECENT_WINDOW_DAYS,
                "priced_days_recent": product.eu_source_priced_days_recent,
                "viable_days_recent": product.eu_source_viable_days_recent,
                "best_roi_recent_pct": product.eu_source_best_roi_recent,
                "viable_roi_threshold_pct": RECENT_VIABLE_ROI_PCT,
                # Best guess at WHEN and WHAT they likely paid -- the
                # day in the recent window with the strongest EU
                # margin (best_roi_recent_pct, above) is the day
                # buying would have made most sense. Inferred from
                # price history, not a confirmed purchase -- see
                # compute_recent_evidence's docstring.
                "guessed_buy_date": product.eu_source_best_roi_date,
                "guessed_buy_price_gbp": product.eu_source_best_roi_cost_gbp,
            },
        )

    @staticmethod
    def _check_uk_a2a(product: Product) -> SourcingClassification | None:
        """
        Real UK price dip within the last RECENT_WINDOW_DAYS days (not
        "at some point in the last 90 days", which could easily be
        stale relative to when this listing was actually detected) --
        see Product.uk_dip_days_recent / compute_recent_evidence.

        No currently_buyable check here -- see SourcingClassification's
        own docstring: "can we buy it right now" is answered
        separately by the linked ProductRecord's OpportunityEngine
        recommendation, not computed here. `recovered`/`recovery_pct`
        are still recorded in the reasoning below purely as
        explanatory context (has the dip already closed?), not as a
        buyability verdict.
        """
        if not product.buy_box_90d or not product.uk_dip_days_recent:
            return None

        recovery_pct = product.buy_box_now / product.buy_box_90d
        recovered = recovery_pct >= RECOVERY_THRESHOLD

        return SourcingClassification(
            sourcing_tag="UK A2A",
            reasoning={
                "recent_window_days": RECENT_WINDOW_DAYS,
                "uk_dip_days_recent": product.uk_dip_days_recent,
                "uk_price_min_recent": product.uk_price_min_recent,
                "uk_price_min_recent_date": product.uk_price_min_recent_date,
                "dip_threshold_pct": round(DIP_THRESHOLD * 100, 1),
                "buy_box_now": product.buy_box_now,
                "buy_box_90d_avg": product.buy_box_90d,
                "recovery_pct": round(recovery_pct * 100, 1),
                "recovery_threshold_pct": round(RECOVERY_THRESHOLD * 100, 1),
                "recovered": recovered,
                # Best guess at WHEN and WHAT they likely paid -- the
                # day the UK price was lowest (uk_price_min_recent,
                # above) is the day buying at the dip (expecting
                # recovery) would have made most sense. Inferred from
                # price history, not a confirmed purchase.
                "guessed_buy_date": product.uk_price_min_recent_date,
                "guessed_buy_price_gbp": product.uk_price_min_recent,
            },
        )

    @staticmethod
    def _check_wholesale(product: Product, brand_repeat_count: int) -> SourcingClassification | None:
        """
        No recent A2A pattern in either direction -- check for
        structural signals of a standing supply relationship instead of
        an opportunistic find. ANY one signal is enough to tag this as
        a best guess, not all three; each raw value is still stored so
        the guess can be judged rather than taken on faith.
        """
        small_seller_count = 0 < product.offers_now <= WHOLESALE_MAX_SELLERS
        multipack_match = MULTIPACK_PATTERN.search(product.title or "")
        brand_recurring = brand_repeat_count >= WHOLESALE_BRAND_REPEAT_THRESHOLD

        if not (small_seller_count or multipack_match or brand_recurring):
            return None

        return SourcingClassification(
            sourcing_tag="Wholesale (likely)",
            reasoning={
                "offer_count": product.offers_now,
                "small_seller_count_threshold": WHOLESALE_MAX_SELLERS,
                "small_seller_count_signal": small_seller_count,
                "multipack_signal": bool(multipack_match),
                "multipack_match": multipack_match.group(0) if multipack_match else None,
                "brand_repeat_count": brand_repeat_count,
                "brand_repeat_threshold": WHOLESALE_BRAND_REPEAT_THRESHOLD,
                "brand_recurring_signal": brand_recurring,
            },
        )

    @staticmethod
    def _fallback_unclear(product: Product, brand_repeat_count: int) -> SourcingClassification:
        """
        Nothing matched -- explicitly note which checks ran and came
        back negative (including the recent-window evidence itself),
        so this reads as "we looked, recently, and found nothing"
        rather than implying false confidence. Per the user's own
        framing: no recent EU margin and no recent UK dip means this
        probably wasn't EU/UK A2A at all -- OA or wholesale (checked
        above and also negative here) is the more likely explanation.
        """
        return SourcingClassification(
            sourcing_tag="OA / unclear",
            reasoning={
                "recent_window_days": RECENT_WINDOW_DAYS,
                "eu_a2a_checked": True,
                "eu_a2a_priced_days_recent": product.eu_source_priced_days_recent,
                "eu_a2a_viable_days_recent": product.eu_source_viable_days_recent,
                "uk_a2a_checked": bool(product.buy_box_90d),
                "uk_a2a_dip_days_recent": product.uk_dip_days_recent,
                "wholesale_checked": True,
                "wholesale_matched": False,
                "offer_count": product.offers_now,
                "brand_repeat_count": brand_repeat_count,
                "note": "No recent EU margin or UK dip in the last "
                        f"{RECENT_WINDOW_DAYS} days, and no wholesale structural "
                        "signal either -- most likely OA, or a sourcing method "
                        "outside what Atlas can infer from price history.",
            },
        )
