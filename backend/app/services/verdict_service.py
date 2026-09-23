import json
from dataclasses import replace

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper, MARKETPLACE_CURRENCY
from app.services.currency_service import CurrencyService
from app.services.category_survey_service import get_category_names
from app.services.fee_engine import FeeEngine
from app.services.product_repository import ProductRepository
from app.services.watchlist_service import WatchlistService
from app.config.exclusions import is_gated_by_name
from app.keepa.parser import KeepaParser
from app.sp_api.client import get_sp_api_client
from app.services.sourcing_classifier import SourcingClassifier

# Cap on VerdictService.get_similar_rejections' output -- a verdict
# prompt with 30 past rejections crammed in is worse than one with the
# 5 most relevant, and it's real prompt-token cost either way.
MAX_SIMILAR_REJECTIONS = 5

# Cap on VerdictService.get_all_reasoned_rejections' output (the
# /criteria/review pattern-analysis input, not a per-verdict prompt) --
# bounds a single analysis call's prompt size once the review queue has
# real history; most recent first, so it's the newest preferences that
# get seen if this cap is ever actually hit.
MAX_REJECTIONS_FOR_PATTERN_REVIEW = 200

# Label passed as best_source_marketplace when computing a rough
# Keepa-estimate profit for a verdict check -- not a real EU
# marketplace, so FeeEngine.calculate falls back to the UK VAT rate
# for the cost side, same convention OaLookupService uses for a
# domestic UK cost (see oa_lookup_service.search_candidates). A
# verdict check's supplied cost_price could be OA (domestic) or A2A
# (cross-border EU) -- this is a deliberate, documented approximation
# rather than a precise per-marketplace VAT treatment.
MANUAL_COST_LABEL = "MANUAL"

# Keepa stats windows this app can ask for (see
# ProductService.get_products' stats_days) -- 180 covers all three
# spec-required trend points (30/90/180) in a single request.
STATS_WINDOW_DAYS = 180

# EU marketplaces an A2A lead can actually be sourced from -- the same
# set ProductMapper.MARKETPLACE_COST_FIELD covers. "UK" is deliberately
# absent: a UK-sourced lead is OA, and its cost came from a retailer,
# not from an Amazon listing there's anything to verify.
EU_SOURCE_MARKETPLACES = ("DE", "FR", "ES", "IT")

# Free-text -> marketplace code, for the source-marketplace check below.
# Keyed on what actually shows up in the wild: an Amazon domain pasted
# into the Verdict Checker's "source" box or a VA sheet's source/link
# column, the bare country code, or the country name.
_MARKETPLACE_HINTS = {
    "DE": ("amazon.de", "germany", "german", "deutschland"),
    "FR": ("amazon.fr", "france", "french"),
    "ES": ("amazon.es", "spain", "spanish", "espana", "españa"),
    "IT": ("amazon.it", "italy", "italian", "italia"),
}


def resolve_source_marketplace(*candidates: str | None) -> str | None:
    """
    Best-effort "which EU marketplace did this lead come from" from
    whatever free text is available -- an explicit code the user picked
    on the Verdict Checker, an Amazon domain in a pasted source URL, or
    a country name in a VA sheet's source column. First candidate that
    resolves wins, so callers pass their most trustworthy signal first.

    Returns None when nothing resolves, which every caller treats as
    "don't run the source check" rather than "assume DE" -- an extra
    Keepa call against the wrong marketplace would produce a confident
    but meaningless answer, which is worse than no answer.
    """
    for candidate in candidates:
        if not candidate:
            continue

        text = str(candidate).strip().lower()

        if text.upper() in EU_SOURCE_MARKETPLACES:
            return text.upper()

        for code, hints in _MARKETPLACE_HINTS.items():
            if any(hint in text for hint in hints):
                return code

    return None


class VerdictService:

    @staticmethod
    def get_all_reasoned_rejections() -> list[dict]:
        """
        Every Lead rejection that carries a "why not" reason, most
        recent first, capped at MAX_REJECTIONS_FOR_PATTERN_REVIEW --
        the input to the /criteria/review pattern-analysis page (brief
        step 8, part 2). Unlike get_similar_rejections below, this
        isn't matched against any one candidate ASIN; it's the whole
        pool a pattern search runs over.
        """
        db = SessionLocal()
        try:
            rejected = (
                db.query(Lead)
                .filter(
                    Lead.decision == "rejected",
                    Lead.decision_reason.isnot(None),
                    Lead.decision_reason != "",
                )
                .order_by(Lead.reviewed_at.desc())
                .limit(MAX_REJECTIONS_FOR_PATTERN_REVIEW)
                .all()
            )
        finally:
            db.close()

        results = []
        for lead in rejected:
            m = {}
            if lead.keepa_metrics:
                try:
                    m = json.loads(lead.keepa_metrics)
                except Exception:
                    m = {}
            results.append({
                "asin": lead.asin,
                "brand": m.get("brand"),
                "category_name": m.get("category_name"),
                "decision_reason": lead.decision_reason,
            })

        return results

    @staticmethod
    def get_similar_rejections(asin: str) -> list[dict]:
        """
        Sourcing agent brief step 7 -- past Lead rejections of THIS
        EXACT ASIN, with the captured "why not" reason
        (Lead.decision_reason, added 2026-08-23 in the reject-flow
        change), surfaced as context for scoring it again.

        Deliberately scoped to Lead only, not ProductRecord/
        SellerNewListing's own review_reason columns -- those belong
        to Discovery/Competitor Watch's separate deterministic pipeline
        (see the brief's "repo split that matters" note), and blending
        their rejection reasons into the Verdict Checker's LLM prompt
        would mix two different review contexts together.

        SAME ASIN ONLY. This originally also matched same-brand and
        same-category rejections, and both were removed 2026-08-27
        because they generate false signal, not signal:

        A brand has good and bad ASINs. Rejecting one Philips item for
        "Not profitable enough" says nothing whatsoever about a
        different Philips item -- but it reached the prompt as PAST
        REJECTIONS context and Claude, correctly treating it as
        relevant, spent a whole rationale bullet on it ("Same brand
        (Philips) was rejected before on a different ASIN...") in a
        verdict that was otherwise a clean BUY. Category matching was
        worse still: the category here is the likes of "Home & Garden",
        so it fired on essentially unrelated products.

        The one brand-level property that DOES carry across ASINs --
        being gated on the brand -- is now handled separately and
        deterministically by get_brand_gating (below), off the
        GatedBrand table, rather than depending on someone having
        happened to type "gated" into the reject prompt.

        Still capped at MAX_SIMILAR_REJECTIONS: an ASIN rejected and
        resurfaced a dozen times doesn't need all twelve in the prompt.
        """
        db = SessionLocal()
        try:
            rejected = (
                db.query(Lead)
                .filter(
                    Lead.asin == asin,
                    Lead.decision == "rejected",
                    Lead.decision_reason.isnot(None),
                    Lead.decision_reason != "",
                )
                .order_by(Lead.reviewed_at.desc())
                .limit(MAX_SIMILAR_REJECTIONS)
                .all()
            )
        finally:
            db.close()

        def _metrics(lead: Lead) -> dict:
            if not lead.keepa_metrics:
                return {}
            try:
                return json.loads(lead.keepa_metrics)
            except Exception:
                return {}

        results = []
        for lead in rejected:
            m = _metrics(lead)
            results.append({
                "asin": lead.asin,
                "brand": m.get("brand"),
                "category_name": m.get("category_name"),
                "decision_reason": lead.decision_reason,
                "reviewed_at": lead.reviewed_at.isoformat() if lead.reviewed_at else None,
                # Retained, and always "same_asin" now, so the template
                # and prompt formatter keep working unchanged and a
                # stored metrics blob from before this change still
                # renders.
                "match_reason": "same_asin",
                "same_asin": True,
            })

        return results

    @staticmethod
    def get_brand_gating(brand: str | None, category_name: str | None) -> dict | None:
        """
        Whether Atlas is gated on this ASIN's brand -- the one
        brand-level fact that genuinely carries from one ASIN to
        another, unlike the per-ASIN economics that used to leak into
        the verdict via same-brand rejection matching (see
        get_similar_rejections).

        Reads the same sources every other gating check in Atlas reads:
        the static GATED_BRAND_CATEGORIES set plus the user-editable
        GatedBrand rows from the Exclusions page, matched by category
        NAME because that's what compute_metrics has (is_gated_by_name,
        the known_products variant -- the Keepa numeric category IDs
        is_gated() wants aren't carried on the metrics dict).

        Until now the Verdict Checker was the one scoring path that
        never consulted this at all: a gated brand could come back BUY
        with nothing saying Atlas can't actually sell it. Returns None
        when not gated, so the prompt section is skipped entirely.
        """
        if not brand:
            return None

        gated = is_gated_by_name(
            brand,
            category_name or "",
            ProductRepository.get_gated_brand_pairs_by_name(),
        )

        if not gated:
            return None

        return {"brand": brand, "category_name": category_name or None}

    @staticmethod
    def check_source_marketplace(asin: str, marketplace: str | None) -> dict | None:
        """
        Verifies that an EU A2A lead is actually BUYABLE on its source
        marketplace, not just profitable on paper. Returns None when
        there's nothing to check (no marketplace resolved, or a
        non-EU/UK one) -- so a domestic OA lead pays no extra Keepa
        cost for a check that doesn't apply to it.

        Why this exists: Atlas can only buy an EU A2A lead from Amazon
        itself or from an FBA seller, because those are the purchases
        that come with a reclaimable Amazon VAT invoice -- a
        merchant-fulfilled (FBM) EU seller ships and invoices direct,
        so there's nothing to reclaim against no matter how good the
        spread looks. The bulk scan paths already enforce exactly this
        (ProductMapper.from_keepa_multi and BrandScanService both skip
        an FBM-won marketplace outright), but the Verdict Checker
        never did: it takes a hand-typed cost_price and never looks at
        the source listing at all, so VA-sheet and manually-entered
        A2A leads reached the review queue unverified and had to be
        rejected by hand. See criteria.md's judgment notes.

        Costs ONE extra Keepa call, on the source marketplace's
        domain, with `offers` on -- which is what makes this better
        than the bulk paths' price-match proxy: with offers requested,
        Keepa populates the authoritative buyBoxIsAmazon/buyBoxIsFBA
        fields KeepaParser.buy_box_holder prefers, so a real FBM
        winner is named as such instead of collapsing into the proxy's
        fail-closed "doesn't match either series" bucket. Only this
        single-ASIN manual path can afford that (see
        ProductService.get_products' include_offers docstring).

        `blocker` is the single actionable outcome, and deliberately
        separates the two rejections that mean different things:
        "amazon_oos" (Amazon sells it but is out of stock right now)
        is a PARK -- it comes back when they restock, which is what
        the review queue's "oos" decision is for -- whereas
        "fbm_only" is a plain reject, since nothing about that listing
        is going to become buyable. "unverified" means Keepa answered
        but without the authoritative fields, so this is the weak
        proxy's opinion and shouldn't be treated as proof either way.
        """
        marketplace = (marketplace or "").strip().upper()

        if marketplace not in EU_SOURCE_MARKETPLACES:
            return None

        try:
            service = ProductService()
            products = service.get_products(
                [asin], marketplace, full=True, include_offers=True,
                usage_category="verdict_source_check",
            )
        except Exception as exc:
            # Never let the source check sink an otherwise-good verdict
            # -- the UK-side metrics are already computed by this point
            # and are worth showing. Report the failure as its own
            # blocker so it reads as "not verified", not "verified fine".
            print(f"Source marketplace check failed for {asin} on {marketplace}: {exc}")
            return {
                "marketplace": marketplace,
                "blocker": "check_failed",
                "note": f"Could not check Amazon {marketplace} -- {exc}",
            }

        if not products:
            return {
                "marketplace": marketplace,
                "blocker": "not_listed",
                "note": f"Not listed on Amazon {marketplace} at all.",
            }

        parser = KeepaParser(products[0])

        holder = parser.buy_box_holder()
        amazon_on_listing = parser.is_amazon_on_listing()
        amazon_in_stock = parser.amazon_in_stock()
        buy_box_price = parser.buy_box_now()

        currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
        buy_box_price_gbp = CurrencyService.to_gbp(buy_box_price, currency) if buy_box_price else None

        buyable = holder in ("amazon", "fba")

        if buyable:
            blocker = None
        elif amazon_on_listing and not amazon_in_stock:
            # Amazon normally sells this and is simply out right now --
            # park it, don't kill it (criteria.md: OOS -> revisit on
            # restock). Checked BEFORE the FBM case on purpose: an FBM
            # seller holding the box while Amazon is out of stock is
            # still an "Amazon restocks and this works again" lead.
            blocker = "amazon_oos"
        elif holder == "fbm":
            blocker = "fbm_only"
        elif holder == "none":
            blocker = "no_buy_box"
        else:
            blocker = "unverified"

        notes = {
            None: f"Buy box on Amazon {marketplace} is {holder.upper()} -- buyable.",
            "amazon_oos": f"Amazon {marketplace} sells this but is OUT OF STOCK right now.",
            "fbm_only": f"Amazon {marketplace} buy box is held by a merchant-fulfilled (FBM) seller -- not buyable for A2A.",
            "no_buy_box": f"No live buy box on Amazon {marketplace}.",
            "unverified": f"Could not confirm who holds the Amazon {marketplace} buy box.",
        }

        return {
            "marketplace": marketplace,
            "buy_box_holder": holder,
            "buyable": buyable,
            "blocker": blocker,
            "note": notes[blocker],
            "amazon_on_listing": amazon_on_listing,
            "amazon_in_stock": amazon_in_stock,
            "amazon_availability": parser.amazon_availability(),
            "offers_fba_present": parser.offers_fba_present(),
            "offer_count_fba": parser.offer_count_fba(),
            "buy_box_price": buy_box_price,
            "buy_box_price_gbp": buy_box_price_gbp,
            "currency": currency,
            # Internal only -- the raw Keepa payload this call already
            # paid for, which (per ProductService.get_products' own
            # docstring) always carries history=True regardless of the
            # `full` flag, i.e. the same day-by-day price-change series
            # SourcingClassifier needs. compute_metrics pops this back
            # off before the result is ever persisted/shown, so a real
            # sourcing classification can be computed for a Lead with
            # ZERO additional Keepa calls instead of duplicating this
            # fetch. Never leaks into keepa_metrics/the Claude prompt --
            # see compute_metrics' own handling.
            "_raw_product": products[0],
        }

    @staticmethod
    def compute_source_drop_evidence(
        asin: str, marketplace: str | None, product, category_name: str,
    ) -> dict | None:
        """
        Day-by-day evidence for "not profitable at today's EU source
        cost, but genuinely was on enough recent days to be worth
        watching" -- the A2A-lead mirror of viable_days_90d above (that
        one holds cost fixed and varies the UK sale price across ITS
        90-day history; this holds the UK sale price fixed at TODAY's
        and varies the EU SOURCE cost across its own 90-day history).

        Added 2026-09-03, Tamara: "lots of my VA leads are A2A drops
        that are not profitable today but on a price drop so may be
        profitable soon" -- and, in the same breath, "I want to be
        careful about these... the number of times it has been
        profitable in a 90 day window should be a factor" (echoing
        WatchlistService.prune_stale_auto_adds' own hard-won lesson: 94%
        of the watchlist there was auto-added off a single historical-
        low snapshot with no day-count evidence at all, and 88% of it
        never went profitable in 23+ days of rechecking -- a single low
        day proved almost nothing). See WatchlistService.
        maybe_auto_watch_lead for how this actually gets used, including
        why "profitable now" always ranks above this speculative signal.

        Returns {"priced_days", "viable_days_90d", "best_case_profit",
        "best_case_roi", "recent_low_source_cost_gbp"} or None when
        there's nothing to check (no marketplace resolved, not an EU
        one, the UK sale price alone already fails the floor regardless
        of source cost, or Keepa has no data) -- same None-vs-real-dict
        convention as check_source_marketplace above.

        Costs ONE Keepa call (full=True, no `offers` -- only the CSV
        price history matters here, unlike check_source_marketplace's
        buy-box-holder check), entirely separate from that method's own
        call. Callers MUST gate this behind "the baseline isn't already
        profitable" (see maybe_auto_watch_lead) so it's never paid for
        on a lead that doesn't need it at all.
        """
        marketplace = (marketplace or "").strip().upper()

        if marketplace not in EU_SOURCE_MARKETPLACES:
            return None

        # Same 17%/13%/GBP2/GBP10 floor as OpportunityEngine.MIN_VIABLE_
        # ROI/MARGIN_PCT/PROFIT_GBP/SALE_PRICE_GBP -- duplicated as
        # literals rather than imported, same reasoning as
        # MIN_VIABLE_ROI_PCT in compute_metrics below (this module sits
        # below OpportunityEngine in the dependency graph).
        MIN_VIABLE_ROI_PCT = 17.0
        MIN_VIABLE_MARGIN_PCT = 13.0
        MIN_VIABLE_PROFIT_GBP = 2.0
        MIN_SALE_PRICE_GBP = 10.0

        uk_price = max(product.buy_box_now, product.buy_box_90d)
        if uk_price < MIN_SALE_PRICE_GBP:
            # UK side alone already fails the sale-price floor -- no EU
            # cost, however low, makes this viable.
            return None

        try:
            service = ProductService()
            products = service.get_products(
                [asin], marketplace, full=True,
                usage_category="verdict_source_drop_check",
            )
        except Exception as exc:
            print(f"Source drop evidence check failed for {asin} on {marketplace}: {exc}")
            return None

        if not products:
            return None

        parser = KeepaParser(products[0])
        currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
        daily_source_prices = parser.daily_buy_box_prices(90)

        priced_days = 0
        viable_days = 0
        best_profit = 0.0
        best_roi = 0.0
        lowest_cost_gbp = None

        for day_price in daily_source_prices:
            if not day_price:
                continue

            priced_days += 1
            cost_gbp = CurrencyService.to_gbp(day_price, currency)

            if lowest_cost_gbp is None or cost_gbp < lowest_cost_gbp:
                lowest_cost_gbp = cost_gbp

            hypothetical = replace(
                product, best_source_marketplace=marketplace, best_source_cost_gbp=cost_gbp,
            )
            fees = FeeEngine.calculate(hypothetical, category_name=category_name)

            if (
                fees.profit >= MIN_VIABLE_PROFIT_GBP
                and fees.roi >= MIN_VIABLE_ROI_PCT
                and fees.margin >= MIN_VIABLE_MARGIN_PCT
            ):
                viable_days += 1
                if fees.profit > best_profit:
                    best_profit = fees.profit
                    best_roi = fees.roi

        if priced_days == 0:
            return None

        return {
            "priced_days": priced_days,
            "viable_days_90d": viable_days,
            "best_case_profit": best_profit,
            "best_case_roi": best_roi,
            "recent_low_source_cost_gbp": lowest_cost_gbp,
        }

    @staticmethod
    def compute_metrics(
        asin: str, cost_price: float | None = None, source_marketplace: str | None = None,
    ) -> dict | None:
        """
        Full Keepa-derived metric set for one ASIN (spec section 4:
        profitability, demand/competition, price history/stability).
        Returns None if Keepa has no data for this ASIN.

        cost_price, if given, produces a Keepa-ESTIMATE profit/ROI
        only -- clearly not VA/SAS-verified. Callers that already have
        a VA-supplied profit/ROI on the Lead should not pass a
        cost_price here; that ground truth is applied at the
        route/worker layer and must never be recomputed from Keepa
        (see Lead's docstring in app/database/models.py).

        source_marketplace, if given as an EU code (DE/FR/ES/IT),
        adds ONE extra Keepa call to verify the lead is actually
        buyable over there -- Amazon or an FBA seller on the buy box,
        not FBM, and Amazon not out of stock. Result lands in
        "source_check" (None when no marketplace was supplied, so a
        domestic OA lead costs exactly what it did before). See
        check_source_marketplace above for the full why.

        Returns "deep_dive": False and "sp_api_live_check": None --
        see add_deep_dive below for promoting an already-computed
        metrics dict to a real deep dive without a second Keepa call.

        Used to also take a deep_dive param that both requested extra
        Keepa fields (stock, dropped 2026-08-26 -- see
        KeepaParser's now-removed competitor_stock_levels, it never
        actually populated) and ran the free SP-API cross-check inline.
        Split apart 2026-08-26: with stock gone, deep_dive changed
        nothing about the Keepa request itself, so a second
        compute_metrics(deep_dive=True) call was re-fetching byte-for-
        byte identical Keepa data just to reach the SP-API branch --
        pure wasted tokens on every BUY/WATCH lead. See
        app/routes/verdict.py's two-pass orchestration for how the
        split is actually used now.
        """
        service = ProductService()
        products = service.get_products(
            [asin], "UK", full=True, stats_days=STATS_WINDOW_DAYS,
            include_rating=True, include_offers=True,
            usage_category="verdict",
        )

        if not products:
            return None

        raw = products[0]
        product = ProductMapper.from_keepa(raw)

        if not product.title:
            return None

        parser = KeepaParser(raw)
        category_names = get_category_names(service.api)
        category_name = category_names.get(product.category, "")

        keepa_estimate_profit = None
        keepa_estimate_roi = None
        keepa_estimate_margin = None
        keepa_estimate_profit_90d = None
        keepa_estimate_roi_90d = None
        keepa_estimate_profit_peak = None
        keepa_estimate_roi_peak = None
        fee_breakdown = None
        viable_days_90d = None

        if cost_price:
            priced_product = replace(
                product,
                best_source_marketplace=MANUAL_COST_LABEL,
                best_source_cost_gbp=cost_price,
            )
            fees = FeeEngine.calculate(priced_product, category_name=category_name)
            keepa_estimate_profit = fees.profit
            keepa_estimate_roi = fees.roi
            keepa_estimate_margin = fees.margin
            keepa_estimate_profit_90d = fees.profit_90d
            keepa_estimate_roi_90d = fees.roi_90d
            # What profit/ROI would look like at the 90-day PEAK UK
            # price instead of today's/average -- for volatile
            # "sawtooth" leads where the average alone would always
            # look unprofitable. Riskier (only real if you can catch
            # stock while the price is actually up there), which is
            # why this is shown as a separate figure, never blended
            # into the primary estimate above. See OpportunityEngine's
            # PEAK_WINDOW recommendation for the same idea applied to
            # scan results.
            keepa_estimate_profit_peak = fees.profit_peak
            keepa_estimate_roi_peak = fees.roi_peak

            # Full breakdown of what went INTO the estimate above --
            # added so a mismatch against SAS or another tool (wrong
            # category, wrong referral rate, a missing fee) can be
            # spotted at a glance instead of guessed at.
            fee_breakdown = {
                "referral_fee": fees.referral_fee,
                "referral_rate_used": fees.referral_rate_used,
                "fba_fee": fees.fba_fee,
                "prep_fee": fees.prep_fee,
                "digital_services_fee": fees.digital_services_fee,
                "uk_vat_rate_used": fees.uk_vat_rate_used,
            }

            # Day-by-day, not average-based: reconstructs the actual
            # buy-box price for each of the last 90 days and re-runs
            # the same ROI formula against every one of them, then
            # counts how many days would have cleared a viable ROI.
            # Answers "how many of the last N days was this actually
            # a good buy", which buy_box_90d's single average can't --
            # a product that's genuinely profitable on most days but
            # dragged under by a handful of low outliers looks
            # identical to one that's marginal every day if all you
            # have is the average (and vice versa for a mostly-bad
            # product with one high spike).
            #
            # Real gap found live, 2026-09-07 (Tamara, re: B0FQCB7YS9
            # showing zero days profitable at either bar): the original
            # pairing -- 25%+ ROI and a 17%-labelled-as-10% floor, BOTH
            # over the full 90 days -- meant a lead with a real recent
            # margin that only opened up in, say, the last 3 weeks read
            # identically to one that was never once viable in 90 days;
            # both show "0 of 90". Replaced with two genuinely different
            # windows/bars instead of one window at two bars: a TIGHTER
            # ROI floor (25%) over a SHORTER, more-recent window (30
            # days) -- "is this working RIGHT NOW" -- alongside a LOWER
            # bar (20%) over the FULL 90 days -- "has this ever cleared
            # a real margin recently at all". `daily_prices` is oldest-
            # first (see daily_buy_box_prices' own docstring), so its
            # last 30 entries are exactly the most recent 30 days.
            #
            # Real bug found live, 2026-09-07 follow-up (Tamara: "we are
            # ... looking at individual days that it passed the criteria
            # not an average") -- daily_buy_box_prices takes exactly ONE
            # midnight snapshot per day, so a real intraday price spike
            # that reverted the same day (confirmed live on B0FQCB7YS9:
            # 2026-08-27, buy box jumped to £181.54 for ~2 hours then
            # back down) was invisible to it, undercounting genuinely
            # viable days. daily_buy_box_peak_prices uses each day's
            # actual MAX price instead -- see its own docstring for why
            # this is a separate method, not a change to the original.
            daily_prices = parser.daily_buy_box_peak_prices(90)
            RECENT_30D_ROI_PCT = FeeEngine.OA_TARGET_ROI_PCT  # 25% -- same bar as is_notable's own
            RECENT_90D_ROI_PCT = 20.0

            priced_days_90d = 0
            days_at_20pct_90d = 0
            priced_days_30d = 0
            days_at_25pct_30d = 0
            first_index_of_last_30 = len(daily_prices) - 30

            for i, day_price in enumerate(daily_prices):
                if not day_price:
                    continue

                priced_days_90d += 1
                day_roi = FeeEngine.roi_at_price(
                    day_price, cost_price, category_name, fees.fba_fee, fees.eu_vat_rate_used,
                )

                if day_roi >= RECENT_90D_ROI_PCT:
                    days_at_20pct_90d += 1

                if i >= first_index_of_last_30:
                    priced_days_30d += 1
                    if day_roi >= RECENT_30D_ROI_PCT:
                        days_at_25pct_30d += 1

            viable_days_90d = {
                "priced_days_90d": priced_days_90d,
                "days_at_20pct_90d": days_at_20pct_90d,
                "priced_days_30d": priced_days_30d,
                "days_at_25pct_30d": days_at_25pct_30d,
            }

        # Deliberately last: everything above is already paid for by
        # the UK call, so a source check that fails or is skipped
        # costs the caller nothing it wouldn't have spent anyway.
        source_check = VerdictService.check_source_marketplace(asin, source_marketplace)
        eu_raw_product = source_check.pop("_raw_product", None) if source_check else None

        # Sourcing classification (2026-09-03, Review Queue backend
        # build) -- reuses SourcingClassifier (already built/tested for
        # Competitor Watch) against data ALREADY fetched above: `raw`
        # (this ASIN's UK payload, always fetched) covers UK A2A
        # regardless of source_marketplace; eu_raw_product (popped off
        # source_check just above) covers EU A2A ONLY for the ONE
        # marketplace the VA/sheet already told us about -- NOT a full
        # DE/FR/ES/IT search the way Competitor Watch does for a
        # completely unknown competitor detection, since that would
        # cost 3-4 NEW Keepa calls per lead and nothing here already
        # knows which marketplace to even ask about for an OA lead.
        # ZERO additional Keepa calls either way. Never persisted raw --
        # only the resulting sourcing_tag/reasoning below survives into
        # the returned metrics dict.
        sourcing_classification = None
        try:
            fee_context = FeeEngine.calculate(product, category_name=category_name)
            product.fba_fee = fee_context.fba_fee
            product.eu_vat_rate_used = fee_context.eu_vat_rate_used

            eu_products_for_classifier = (
                {source_marketplace: eu_raw_product} if (source_marketplace and eu_raw_product) else {}
            )
            recent_evidence = SourcingClassifier.compute_recent_evidence(
                raw, eu_products_for_classifier, product, category_name,
            )
            for field_name, value in recent_evidence.items():
                setattr(product, field_name, value)

            # No cross-lead "same seller relisted this brand" signal
            # exists for a Lead the way it does for a tracked
            # competitor's storefront history -- brand_repeat_count=0
            # just means that ONE wholesale signal never fires here;
            # the small-seller-count and multipack-title signals still
            # can (see SourcingClassifier._check_wholesale).
            classification = SourcingClassifier.classify(product, brand_repeat_count=0)
            sourcing_classification = {
                "sourcing_tag": classification.sourcing_tag,
                "reasoning": classification.reasoning,
            }
        except Exception as exc:
            print(f"Sourcing classification failed for {asin}: {exc}")

        # Speculative "not profitable now, but recently was at a lower
        # EU source cost" tracking (2026-09-03) -- see WatchlistService.
        # maybe_auto_watch_lead. Only for A2A leads (source_marketplace
        # resolved) that AREN'T already profitable at today's/90d cost --
        # a genuinely profitable-now lead gets a real BUY/WATCH verdict
        # and goes straight to the Review Queue, so this speculative,
        # lower-trust signal never even runs for it, let alone competes
        # with it (Tamara: "profitable now should be higher rated than
        # one that may be profitable in x days").
        if source_marketplace and max(keepa_estimate_profit or 0, keepa_estimate_profit_90d or 0) <= 0:
            source_drop_evidence = VerdictService.compute_source_drop_evidence(
                asin, source_marketplace, product, category_name,
            )
            if source_drop_evidence:
                WatchlistService.maybe_auto_watch_lead(
                    asin, product.title, product.brand, source_marketplace, source_drop_evidence,
                )

        monthly_sales_as_of = parser.monthly_sales_as_of()

        return {
            "deep_dive": False,
            "sp_api_live_check": None,

            "asin": product.asin,
            "title": product.title,
            "brand": product.brand,
            # Real gap found live, 2026-09-07: this Keepa fetch already
            # gives us product.image (see ProductMapper.from_keepa) at
            # zero extra cost -- it just never made it into the saved
            # metrics dict, so a VA sheet lead (which has no ProductRecord
            # of its own) never showed a photo even after analysis. Scan/
            # competitor items get their image from ProductRecord.image
            # instead; this closes the same gap for the Lead path.
            "image": product.image,
            "category_name": category_name,
            "ean": product.ean,

            # Amazon's own "frequently returned item" badge (Tamara,
            # 2026-09-11: "exclude leads from any queue that have the
            # frequently returned badge") -- see Product.frequently_returned
            # and generate_verdict's own use of this key to force AVOID
            # the same way brand_gating does, regardless of the numbers.
            "frequently_returned": product.frequently_returned,

            # EU A2A buyability -- None for OA/unknown-source leads.
            "source_check": source_check,

            # How Atlas thinks this was/would be sourced (EU A2A / UK
            # A2A / Wholesale (likely) / OA / unclear) -- independent
            # of, and never overriding, whatever sourcing_type the VA
            # typed into the sheet. See SourcingClassifier.
            "sourcing_classification": sourcing_classification,

            # Profitability -- Keepa estimate only (None if no
            # cost_price was supplied); VA/SAS figures, when present,
            # live on the Lead itself and are never derived here.
            "keepa_estimate_profit": keepa_estimate_profit,
            "keepa_estimate_roi": keepa_estimate_roi,
            "keepa_estimate_margin": keepa_estimate_margin,
            "keepa_estimate_profit_90d": keepa_estimate_profit_90d,
            "keepa_estimate_roi_90d": keepa_estimate_roi_90d,
            "keepa_estimate_profit_peak": keepa_estimate_profit_peak,
            "keepa_estimate_roi_peak": keepa_estimate_roi_peak,
            "fee_breakdown": fee_breakdown,
            "viable_days_90d": viable_days_90d,

            # Demand & competition
            "price_avg_30d": parser.price_avg(30),
            "price_avg_90d": parser.price_avg(90),
            "price_avg_180d": parser.price_avg(180),
            "buy_box_percentage": parser.buy_box_percentage(),
            "amazon_buy_box_percentage": parser.amazon_buy_box_percentage(),
            "offers_now": parser.offers_now(),
            "offers_90d_avg": parser.offers_90d(),
            "offers_fba_present": parser.offers_fba_present(),
            "offer_count_fba": parser.offer_count_fba(),
            "offer_trend": parser.offer_trend(),
            "monthly_sales": parser.monthly_sales(),
            "monthly_sales_as_of": monthly_sales_as_of.isoformat() if monthly_sales_as_of else None,
            "sales_drops_30d": parser.sales_drops_30d(),
            "rating": parser.rating(),
            "review_count": parser.review_count(),
            "is_amazon_on_listing": parser.is_amazon_on_listing(),
            # Real gap found live, 2026-09-07 (Tamara, re: B0CFV7Z7SJ:
            # "this only had amazon on the buy box in the past 30 days
            # which is a red flag I should see") -- amazon_buy_box_
            # percentage above only reads the CURRENT moment, so it
            # returns 0 the instant Amazon isn't the buy box holder
            # RIGHT NOW (e.g. Amazon momentarily between stock), silently
            # hiding a genuine recent pattern of Amazon totally
            # dominating the buy box. This reads the real 30-day
            # seller-ID history instead -- see KeepaParser.buy_box_
            # holder_breakdown's own docstring.
            "buy_box_holder_30d": parser.buy_box_holder_breakdown(30),

            # Price history / stability
            "buy_box_now": parser.buy_box_now(),
            "price_drop_count_30d": parser.price_drop_count(30),
            "price_drop_count_90d": parser.price_drop_count(90),
            "price_drop_count_180d": parser.price_drop_count(180),
            "price_min_ever": parser.price_min_ever(),
            "price_min_90d": parser.buy_box_min_90d(),
            "price_max": parser.price_max(),
            "price_max_90d": parser.buy_box_max_90d(),
            "is_out_of_stock": parser.is_out_of_stock(),
        }

    @staticmethod
    def add_deep_dive(metrics: dict, asin: str) -> dict:
        """
        Promotes an already-computed compute_metrics() dict to a deep
        dive IN PLACE, no second Keepa call -- pass-1's metrics already
        has everything a deep dive needs except the live SP-API price
        cross-check (2026-08-23, sourcing agent brief section 5;
        per-seller stock was the other deep-dive field, dropped
        2026-08-26, see compute_metrics' own docstring).

        Only sets deep_dive=True when the SP-API call actually returned
        something (see SPAPIClient.get_item_offers' own docstring for
        why a failed/unconfigured call comes back None, not a dict with
        price=None) -- if it didn't, there's no new evidence over the
        baseline pass, so the caller should skip re-running Claude
        rather than spend a second verdict call to say the same thing
        again with a "not available" line bolted on.
        """
        sp_client = get_sp_api_client()
        sp_api_live_check = sp_client.get_item_offers(asin, "UK") if sp_client is not None else None

        metrics["sp_api_live_check"] = sp_api_live_check
        metrics["deep_dive"] = sp_api_live_check is not None
        return metrics
