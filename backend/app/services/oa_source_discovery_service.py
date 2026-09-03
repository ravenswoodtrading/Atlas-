import json
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import OaSourceRun, OaSourceCandidate, OaSourceExcludedAsin, ProductRecord
from app.models.product import Product
from app.services.seller_watch_service import SellerWatchService
from app.services.product_repository import ProductRepository
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.category_survey_service import get_category_names
from app.services.fee_engine import FeeEngine
from app.services import brave_search_client
from app.services import shopping_search
from app.services.oa_domain_classifier import classify_domain, classify_shopping_source
from app.services.opportunity_engine import OpportunityEngine
from app.services.activity_log import ActivityLog
from app.config.exclusions import is_gated
from app.keepa.parser import KeepaParser
from dataclasses import asdict, replace as dataclass_replace

# Words too generic to help narrow a search -- same list OaLookupService
# already uses for its own title-cleaning, reused here rather than
# duplicated so both places stay in sync if it's ever tuned.
from app.services.oa_lookup_service import STOPWORDS

import re


# Max discovery queries generated per ASIN (MPN UK / EAN / Brand+Title
# UK) -- per the user's own MVP spec: "Don't generate excessive
# searches initially." Naturally comes out lower than this whenever a
# tier's underlying data (MPN especially -- see Product.ean's own
# "reference/display only" precedent; nothing in Atlas has ever parsed
# an MPN/model field before this module) isn't available for a given
# ASIN.
MAX_DISCOVERY_QUERIES_PER_ASIN = 3

# How many distinct candidate retailer domains get a follow-up
# site-restricted verification search per ASIN (see the spec's
# "Retailer verification" step) -- capped so a product mentioned
# across many domains doesn't blow the search budget on one ASIN.
MAX_RETAILERS_VERIFIED_PER_ASIN = 3

# How many significant words of the title to keep for the Brand+Title
# query -- same constant/spirit as OaLookupService.
SUGGESTED_QUERY_WORD_COUNT = 10

# "Highly similar" bar for the title-similarity tiers (SequenceMatcher
# ratio, 0-1) -- see classify_match's docstring for why this can never
# promote a match past "brand_title"/"title_only" on its own.
TITLE_SIMILARITY_STRONG = 0.55

# Don't re-run an ASIN that already has a candidate row from within
# this many days -- keeps repeated test batches from just re-searching
# (and re-spending Brave/Keepa budget on) the same handful of ASINs
# every time the "Run batch" button is clicked.
SKIP_RECENTLY_CHECKED_DAYS = 7

# Default "detected in the last N days" window for the "next up to
# scan" preview AND for what a Run batch actually searches (2026-08-28).
#
# WHY this exists: the eligible pool is sorted by max_source_cost
# DESCENDING (see _sorted_eligible_rows -- best opportunity first, the
# user's own confirmed choice) and there are ~290 eligible ASINs at any
# time. A handful of expensive old detections (£500 laptops from three
# weeks ago) therefore permanently occupy the top of the list, so a
# default limit=20 batch would search those over and over and NEVER
# reach anything a competitor listed this week -- the exact complaint
# that prompted this ("it only looks to have old leads on here... I
# thought it would give me recent OA leads competitors have added").
#
# The window is applied BEFORE the value sort, not instead of it: you
# still get best-opportunity-first, just within recent detections.
# 0 means no window at all (the pre-2026-08-28 behaviour) and is a
# selectable option on the page, so nothing is permanently hidden --
# an old-but-valuable ASIN is always one dropdown change away.
DEFAULT_DETECTION_WINDOW_DAYS = 7

# Reserve this many SerpApi searches at the bottom of the monthly quota
# and stop spending them once remaining quota drops to/below this line
# -- per the user's own quota concern (2026-08-19): "I am concerned we
# are going to get through this fast". A large batch (get_candidate_asins
# now sorts best-opportunity-first, so a limited quota is already being
# spent on the highest-value ASINs -- see _sorted_eligible_rows) could
# otherwise burn through an entire month's free/paid SerpApi quota in
# one "Run batch" click with no warning. Once the buffer is hit,
# run_batch stops calling SerpApi for the rest of THIS run and every
# remaining ASIN falls back to the existing free-tier-friendly Brave
# pipeline instead (same one used before SerpApi automation existed) --
# never a hard failure, just a graceful downgrade, surfaced to the user
# via OaSourceRun.serpapi_quota_stopped so it's visible, not silent.
SERPAPI_QUOTA_SAFETY_BUFFER = 15

# FeeEngine.max_source_cost > 0 alone isn't enough of a floor -- a
# cheap item (e.g. a GBP6.99 listing) can technically clear it at
# well under GBP1, which is not a realistic purchase price for a real
# retail product regardless of the math working out on paper.
# Named constant, easy to recalibrate once real batches show whether
# this is too strict/loose -- same spirit as the recalibration notes
# on DIP_THRESHOLD/RECENT_VIABLE_ROI_PCT in sourcing_classifier.py.
MIN_VIABLE_SOURCE_COST_GBP = 3.0

# Match-tier -> (confidence_pct, source_confidence), ordered here from
# strongest to weakest -- this dict's KEY ORDER doubles as the
# priority ranking run_batch uses to pick the best tier found across
# multiple candidate domains for one ASIN (see the `list(...).index()`
# lookups below), so don't reorder without meaning it.
#
# NOTE this ranks "brand_mpn" ABOVE plain "mpn", one place where this
# deliberately reads the user's own 5-tier list (EAN > MPN > Brand+MPN
# > Brand+Title > Title only) as ranking DETECTION METHODS, not final
# confidence -- both "mpn" and "brand_mpn" here come from the exact
# same site-restricted MPN verification hit (see classify_match); the
# only difference is whether the brand name ALSO appears in that same
# hit. That can only add corroborating evidence, never subtract it, so
# it's scored higher rather than lower. Flagged here in case the
# literal spec order was intended instead -- easy one-line swap if so.
MATCH_TIER_SCORES = {
    "ean": (98, "High"),
    "brand_mpn": (90, "High"),
    "mpn": (85, "High"),
    "brand_title": (55, "Medium"),
    "title_only": (30, "Low"),
}

# Match tiers trusted enough for Atlas's OWN algorithm to auto-populate
# a price from a Google Shopping result AND auto-promote it into a real
# Review Queue lead with NO human check, if it also clears
# FeeEngine.OA_TARGET_ROI_PCT -- per the user's own explicit choice
# (2026-08-19): "title_only" alone is excluded, same "never a confirmed
# match" rule classify_match's docstring already established for the
# Brave pipeline. A weaker/untrusted match still shows up on the
# results page (see shopping_candidates_json) for a human to manually
# pick if they recognise it as correct.
#
# Only gates the FULLY AUTOMATED path (see
# OaSourceCandidate.price_source == "serpapi_auto") -- once a human has
# confirmed or overridden a price themselves (price_source == "manual"),
# THEY are the trust check, and this tier bar no longer applies -- see
# _promote_if_qualifying.
TRUSTED_MATCH_TIERS = ("ean", "brand_mpn", "mpn", "brand_title")

# Auto-PROMOTE trust bar (2026-08-29, tightened after a real false
# positive Tamara caught by eye: B0DV6CDXKT, a UbiQuiti
# USW-FLEX-2.5G-8-POE switch, auto-matched to Optdex's GBP114 listing
# for the base "USW-FLEX" model -- a different, cheaper product in the
# same family. The match scored "brand_title" (Medium, 55%
# confidence) and was the cheapest TRUSTED_MATCH_TIERS hit, so it won
# and went straight into the Review Queue with no human look at all.
#
# Checked the real numbers behind that run: several OTHER listings in
# the same result set genuinely WERE the right product (matching
# title, matching price range) but scored a LOWER SequenceMatcher
# similarity than the wrong one and were classified "title_only" --
# i.e. the title-similarity signal that "brand_title" rests on isn't
# reliable enough, on its own, to silently promote a lead with zero
# human check. See classify_shopping_match's own docstring: this tier
# has no actual product-identifier confirmation behind it, just brand
# + fuzzy title similarity -- exactly the signal that can't tell a
# variant/sibling product apart from the real one (same root cause as
# the K5/K2 pressure-washer false positive IMPLAUSIBLE_AUTO_PROMOTE_ROI_PCT
# was added for, 2026-08-28 -- that backstop doesn't catch THIS case
# because a wrong-but-real-looking price like GBP114 isn't
# implausible in isolation).
#
# Deliberately narrower than TRUSTED_MATCH_TIERS above, which still
# governs which shopping result auto-populates a candidate's
# SUGGESTED price for the OA results page (a brand_title match still
# shows up there, pre-priced, ready for a human to confirm or
# reject) -- this constant only gates the fully-automated path
# straight into the real Review Queue with no human look at all. See
# _promote_if_qualifying's match_trusted_enough.
TRUSTED_MATCH_TIERS_AUTO_PROMOTE = ("ean", "brand_mpn", "mpn")

# ROI above which a FULLY AUTOMATED "brand_title" match is treated as
# too good to be true and held back for a human instead of
# auto-promoted (2026-08-28).
#
# From a real false positive in run 3: a Karcher K 5 WCM Flex pressure
# washer matched to an Argos listing at GBP45, scoring 336% ROI, and
# went straight into the Review Queue. A K5 does not sell for GBP45 --
# the match was almost certainly a different, far cheaper item in the
# same brand family (an accessory, a K2, a spare lance).
#
# WHY only "brand_title": it's the one trusted tier with NO identifier
# confirmation behind it at all -- just brand name + fuzzy title
# similarity (see classify_shopping_match), which is exactly the
# signal that cannot tell a K5 from a K2. The ean/mpn tiers matched a
# specific product identifier, so a high ROI there is far more likely
# to be a real find than a variant mix-up, and gating them would throw
# away genuine leads. Not applied to price_source == "manual" either:
# a human who opened the page and typed the price has already done the
# check this bar is a substitute for.
#
# Deliberately generous -- this is a "that's not physically plausible"
# backstop, not a profitability opinion. A genuine 150% OA find should
# still sail through. Needs recalibrating once real batches show the
# actual ROI distribution, same spirit as DIP_THRESHOLD in
# sourcing_classifier.py. The candidate is NOT discarded: it still
# appears on the results page with its price and ROI for a human to
# confirm, which promotes it via the manual path.
IMPLAUSIBLE_AUTO_PROMOTE_ROI_PCT = 200.0

# WIDENED TO EVERY AUTOMATED TIER (2026-08-28). The reasoning above --
# that an ean/mpn tier "matched a specific product identifier, so a high
# ROI there is far more likely to be a real find" -- was tested by a real
# batch and did not hold. Run 5 auto-promoted B0BTZB7F88 at a claimed
# 1453% ROI: an eBay listing titled "Amd Ryzen 7 7800x3d Box Only No Cpu"
# -- an EMPTY BOX at GBP12.94 against a GBP265 Amazon price -- and it
# reached brand_mpn, not brand_title, because the MPN and brand both
# genuinely appear in the box's own title.
#
# That is the whole failure mode: an identifier match confirms the
# listing is ABOUT the right product, never that it IS the product. A
# box, a spare part, a replacement screen, an empty retail carton and a
# manual all legitimately carry the MPN. So the implausibility backstop
# has to apply to every automated tier, not just the weakest one. A real
# OA find does not return 1453%; a number like that is a mismatch
# detector, not an opportunity.
#
# Still never applied to price_source == "manual" -- a human who opened
# the page has done this check themselves.
TIERS_EXEMPT_FROM_ROI_CEILING = ()

# Phrases that mean the listing is NOT the product itself, even when the
# brand and MPN match perfectly (2026-08-28). Drawn from real false
# positives in run 5: "Box Only No Cpu" (an empty box) and "Asus
# Chromebook Plus CX3402CB 14.0\" Laptop Screen" (a replacement screen,
# not the laptop). Both cleared a trusted tier and auto-promoted.
#
# Matched case-insensitively against the RETAILER's own title, not
# Amazon's. Blocks auto-promotion only -- the candidate still appears on
# the results page, because occasionally one of these is a genuine
# accessory listing the user may want, and that judgement is theirs.
#
# Add to this list whenever a new shape of false positive shows up; it is
# cheap to extend and each entry is evidence of a real miss.
NOT_THE_PRODUCT_PHRASES = (
    "box only", "empty box", "no cpu", "cpu not included",
    "for parts", "spares or repair", "spares/repair", "faulty", "not working",
    "case only", "cover only", "screen only", "replacement screen",
    "lcd screen", "digitizer", "screen assembly",
    "manual only", "instructions only", "photo only", "picture only",
    "battery only", "charger only", "cable only", "stand only", "lid only",
    "pre-owned", "preowned", "second hand", "used ", "refurbished", "refurb",
    "damaged", "incomplete", "read description", "sold as seen",
    "replica", "compatible with", "fits ",
)


def looks_like_not_the_product(retailer_title: str) -> bool:
    """
    True when the retailer's own listing title says this isn't the whole
    product -- see NOT_THE_PRODUCT_PHRASES.

    Deliberately a dumb substring check rather than anything clever: the
    phrases are unambiguous, and a false block costs the user one manual
    confirmation on the results page, while a false pass costs them a
    purchase of an empty box.
    """
    haystack = (retailer_title or "").lower()
    return any(phrase in haystack for phrase in NOT_THE_PRODUCT_PHRASES)


class OaSourceDiscoveryService:
    """
    OA Source Discovery pipeline: takes ASINs Atlas's existing
    SourcingClassifier has already tagged "OA / unclear" (i.e. no
    recent EU or UK A2A evidence -- see sourcing_classifier.py), and
    tries to automatically find a UK retailer selling the same
    product AND its real price.

    UPDATED 2026-08-19 (originally an MVP with no automated pricing at
    all -- see git history / claude/oa-source-discovery-mvp-2026-08-19.md
    for the original spec): per the user's explicit request after
    trying the original MVP ("I can't automate this if I have to
    manually search... find the best price from a UK retailer and if
    it is profitable this gets added to the review queue"), run_batch
    now tries Google Shopping via SerpApi FIRST for each ASIN --
    ONE call returns multiple real retailers with real prices, scored
    via classify_shopping_match, and the CHEAPEST result that clears
    TRUSTED_MATCH_TIERS auto-populates the candidate's price with no
    manual step. If it also clears FeeEngine.OA_TARGET_ROI_PCT, it's
    automatically promoted into a real Atlas lead (see
    _promote_if_qualifying) -- same Review Queue, Dashboard counters,
    Discord alerts as any other scan-found opportunity, no separate
    "OA leads" concept anywhere downstream.

    The ORIGINAL Brave-based discovery+verification pipeline is kept
    as a fallback -- it still runs (unchanged) whenever Google Shopping
    finds nothing at a trusted match tier, which still just finds the
    retailer/page for the user to manually confirm via
    update_candidate() (which ALSO now accepts a full retailer
    override -- name/URL/price -- not just a price, so the user can
    replace Atlas's auto-pick with a different, better retailer they
    found themselves; every Google Shopping candidate considered,
    trusted or not, is kept on the candidate row -- see
    shopping_candidates_json -- specifically so a human can still spot
    and pick a good match Atlas's own trust bar rejected).
    """

    @staticmethod
    def _has_oa_potential(record) -> bool:
        """
        Whether an "OA / unclear" detection is worth spending a search
        budget on, given there is NO known source cost yet -- finding
        one is the whole point (same situation the Competitors page's
        own "OA price guide" already solves for -- see
        FeeEngine.max_source_cost's docstring).

        Deliberately NOT ProductRepository.is_notable (the Review
        Queue's bar) -- that checks roi/roi_90d > 25%, but those
        fields are computed against whatever EU A2A source Keepa
        found, and an "OA / unclear" tag specifically means that
        source's margin ISN'T currently viable (see
        SourcingClassifier._check_eu_a2a, which requires a 10%+
        recent-window margin before it will even call something EU
        A2A -- so is_notable's stricter 25% bar would reject nearly
        every genuine OA/unclear ASIN for the wrong reason: it's
        measuring profitability against a source we already know
        doesn't work, not against the unknown retailer source this
        module exists to find).

        Correct reading of the user's own "sufficient sales potential
        / acceptable profit potential" spec language for a product
        with no known cost: real evidence of sales (same bar
        is_notable itself uses for that half), AND a genuine GBP
        ceiling above zero at which an OA purchase would clear
        FeeEngine.OA_TARGET_ROI_PCT after Amazon's own fees -- i.e.
        the Amazon price itself leaves room for a profitable purchase
        to exist, whatever it turns out to cost.
        """
        has_sales_evidence = (
            record.monthly_sales > 0
            or record.sales_drops_30d >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD
        )

        if not has_sales_evidence:
            return False

        max_source_cost = FeeEngine.max_source_cost(
            record.buy_box_now, record.category_name, record.fba_fee, FeeEngine.OA_TARGET_ROI_PCT,
        )

        return max_source_cost >= MIN_VIABLE_SOURCE_COST_GBP

    @staticmethod
    def _eligible_candidate_rows() -> list:
        """
        Every "OA / unclear" competitor detection that clears
        _has_oa_potential (see there for why that's the right gate,
        not is_notable) AND isn't gated (see is_gated below), one row
        per distinct ASIN, in the detection feed's own order -- no
        recency/exclusion filtering applied yet. An "OA / unclear"
        detection with no linked ProductRecord at all (never scored --
        see SellerWatchService) has nothing to judge potential against,
        so it's skipped here.

        Gated brands (checked via is_gated, same DB-backed GatedBrand
        table + static GATED_BRAND_CATEGORIES set as everywhere else
        in Atlas) are dropped OUTRIGHT here, not just tagged --
        deliberately different from BrandScanService's own Step 3
        behaviour (which tags an incidental gated find as
        product.gated=True but still tracks it, since it came from a
        brand-search scan the user chose to run). This module never
        chose to scan these brands -- it exists purely to spend a
        Brave Search budget hunting for a UK retailer to actually BUY
        from, and a gated brand can't be sold on Amazon regardless of
        whether a source is found, so there's nothing worth tracking
        by including it here. Same reasoning SignalService already
        applies to its own candidates for the same reason. The
        underlying ProductRecord/Competitor Watch listing itself is
        untouched -- still visible and still tagged gated=True over on
        the Competitors page, this only keeps it out of THIS module's
        search queue/budget.

        Shared base for both get_candidate_asins (what a "Run batch"
        click actually searches) and preview_candidates (the "next up
        to scan" table shown before that click) -- factored out so the
        two can never quietly drift out of sync on which ASINs count
        as eligible.
        """
        detections = SellerWatchService.list_detections(
            sourcing_tag="OA / unclear", limit=2000,
        )
        gated_brand_pairs = ProductRepository.get_gated_brand_pairs()

        rows = []
        seen = set()

        for row in detections:
            record = row["record"]
            asin = row["listing"].asin

            if record is None or asin in seen:
                continue

            if is_gated(record.brand, record.category, gated_brand_pairs):
                continue

            if not OaSourceDiscoveryService._has_oa_potential(record):
                continue

            seen.add(asin)
            rows.append(row)

        return rows

    @staticmethod
    def _within_window(rows: list, since_days: int) -> list:
        """
        Keeps only detections first seen in the last `since_days` days.
        since_days=0 (or None) means no window -- every eligible row.

        Filtered in Python against rows already fetched by
        _fresh_eligible_rows rather than pushed down into
        SellerWatchService.list_detections' own since_days SQL filter,
        specifically so ONE pass over the pool can serve both the
        windowed list and the unwindowed total the page shows beside it
        ("87 of 288") -- doing it in SQL would mean running the whole
        eligibility pipeline (gated-brand lookup, _has_oa_potential,
        max_source_cost per row) twice per page load just to produce a
        second count.

        detected_at comes back timezone-NAIVE from SQLite even though
        it's written via datetime.now(timezone.utc) -- same workaround
        SellerWatchService._apply_common_filters already documents for
        its own cutoff. A row with no detected_at at all is KEPT rather
        than dropped: it's a data gap, not evidence of age, and
        silently hiding an otherwise-eligible ASIN would be worse than
        showing one whose date column reads "-".
        """
        if not since_days:
            return rows

        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).replace(tzinfo=None)

        return [
            row for row in rows
            if row["listing"].detected_at is None or row["listing"].detected_at >= cutoff
        ]

    @staticmethod
    def _fresh_eligible_rows() -> list:
        """
        _eligible_candidate_rows(), minus recently-checked (see
        SKIP_RECENTLY_CHECKED_DAYS) and manually-excluded ASINs (see
        OaSourceExcludedAsin), sorted by FeeEngine.max_source_cost
        DESCENDING -- best opportunity (most headroom to pay for stock
        at FeeEngine.OA_TARGET_ROI_PCT) first, per the user's own
        confirmed choice over sorting by most-recently-detected.

        NO detection-age window applied here -- that's _within_window's
        job, layered on top by _sorted_eligible_rows. Split that way so
        preview_context can get the windowed list and the full-pool
        total out of a single pass (see _within_window).

        FIXED 2026-08-19: this ordering used to be computed ONLY inside
        preview_candidates -- get_candidate_asins (what a "Run batch"
        click actually searches) separately returned rows in whatever
        order SellerWatchService.list_detections happened to return
        them (most-recently-DETECTED first), completely ignoring
        opportunity size, despite preview_candidates' own docstring
        claiming the preview table "always reflects what a Run batch
        click would actually pick next." Caught while addressing the
        user's concern about SerpApi search quota running out fast --
        a quota-limited batch needs to spend its searches on the
        BEST candidates first, not just the newest ones. Factored out
        here so get_candidate_asins and preview_candidates can never
        drift apart on this again -- both now genuinely share one
        ordering, computed once.

        Each row gets `row["max_source_cost"]` attached (not just used
        for sorting) so preview_candidates doesn't need to recompute it.
        """
        rows = OaSourceDiscoveryService._eligible_candidate_rows()

        recently_checked = OaSourceDiscoveryService._recently_checked_asins(SKIP_RECENTLY_CHECKED_DAYS)
        excluded = OaSourceDiscoveryService.get_excluded_asins()

        fresh_rows = []

        for row in rows:
            asin = row["listing"].asin
            if asin in recently_checked or asin in excluded:
                continue

            record = row["record"]
            row["max_source_cost"] = FeeEngine.max_source_cost(
                record.buy_box_now, record.category_name, record.fba_fee, FeeEngine.OA_TARGET_ROI_PCT,
            )
            fresh_rows.append(row)

        fresh_rows.sort(key=lambda r: r["max_source_cost"], reverse=True)

        return fresh_rows

    @staticmethod
    def _sorted_eligible_rows(since_days: int = DEFAULT_DETECTION_WINDOW_DAYS) -> list:
        """
        _fresh_eligible_rows() narrowed to detections from the last
        `since_days` days (see DEFAULT_DETECTION_WINDOW_DAYS for why
        that window exists and why it defaults on), still in
        best-opportunity-first order within that window.

        since_days=0 restores the original all-time behaviour.
        """
        return OaSourceDiscoveryService._within_window(
            OaSourceDiscoveryService._fresh_eligible_rows(), since_days,
        )

    @staticmethod
    def _display_row(row: dict) -> dict:
        """
        One eligible row -> the flat dict the "next up to scan" table
        renders. Shared by preview_candidates and preview_context so
        the two can't drift on which fields the template gets.
        """
        record = row["record"]
        listing = row["listing"]

        return {
            "asin": listing.asin,
            "title": record.title,
            "brand": record.brand,
            "category_name": record.category_name,
            "buy_box_now": record.buy_box_now,
            "monthly_sales": record.monthly_sales,
            "ean": record.ean,
            "max_source_cost": row["max_source_cost"],
            "detected_at": listing.detected_at,
        }

    @staticmethod
    def get_candidate_asins(limit: int, since_days: int = DEFAULT_DETECTION_WINDOW_DAYS) -> list:
        """
        Up to `limit` ASINs from _sorted_eligible_rows -- i.e. the
        BEST opportunities first (see that method's docstring for why
        this matters, especially with a finite SerpApi search budget),
        within the last `since_days` days of detections. Distinct
        ASINs, with recently-checked and manually-excluded ones already
        filtered out, so repeated batches don't just keep re-searching
        the same handful of ASINs, and a run automatically respects
        whatever the user pruned from the "next up to scan" preview.

        `since_days` MUST be whatever the page was displaying when Run
        batch was clicked (the form posts it back -- see
        oa_discovery_run) -- otherwise the preview would once again be
        claiming to show what a run would search while the run quietly
        picked a different set, the exact drift _sorted_eligible_rows
        was factored out to prevent in the first place.
        """
        rows = OaSourceDiscoveryService._sorted_eligible_rows(since_days)
        return [row["listing"].asin for row in rows[:limit]]

    @staticmethod
    def preview_candidates(limit: int = 100,
                            since_days: int = DEFAULT_DETECTION_WINDOW_DAYS) -> list:
        """
        Full display rows for the "next up to scan" table -- the exact
        same sorted, eligible/not-recently-checked/not-excluded,
        within-window pool get_candidate_asins draws its ASINs from
        (this table genuinely reflects what a "Run batch" click would
        search next, in the order it would search them), but returned
        as dicts with the fields the UI needs to show and to judge
        before excluding one, rather than bare ASIN strings.

        Kept as a thin wrapper over preview_context for any caller that
        only wants the rows and not the pool counts.
        """
        return OaSourceDiscoveryService.preview_context(limit, since_days)["candidates"]

    @staticmethod
    def preview_context(limit: int = 100,
                         since_days: int = DEFAULT_DETECTION_WINDOW_DAYS) -> dict:
        """
        preview_candidates' rows PLUS the two counts the page needs to
        make the window filter honest:

          window_matched -- eligible ASINs inside the current window
                            (may exceed len(candidates), which is
                            capped at `limit`)
          pool_total     -- eligible ASINs ignoring the window entirely

        Showing both is the whole point: "87 of 288" tells the user at
        a glance that a narrow window is hiding 201 older-but-still-
        eligible ASINs, rather than leaving them to assume the pool
        itself has dried up -- which is precisely how the old
        unwindowed-but-value-sorted list misled in the other direction.

        Both counts come from ONE pass over the eligibility pipeline --
        see _within_window for why the window isn't pushed into SQL.
        """
        all_rows = OaSourceDiscoveryService._fresh_eligible_rows()
        windowed = OaSourceDiscoveryService._within_window(all_rows, since_days)

        return {
            "candidates": [OaSourceDiscoveryService._display_row(row) for row in windowed[:limit]],
            "window_matched": len(windowed),
            "pool_total": len(all_rows),
        }

    @staticmethod
    def get_excluded_asins() -> set:
        db = SessionLocal()

        try:
            rows = db.query(OaSourceExcludedAsin.asin).all()
            return {row[0] for row in rows}
        finally:
            db.close()

    @staticmethod
    def list_excluded() -> list:
        db = SessionLocal()

        try:
            return (
                db.query(OaSourceExcludedAsin)
                .order_by(OaSourceExcludedAsin.excluded_at.desc())
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def exclude_asin(asin: str, title: str = "", reason: str = "") -> None:
        """
        Adds/updates an OA-Discovery-only skip -- same upsert-by-asin
        behaviour as ProductRepository.add_exclusion, but writing to
        OaSourceExcludedAsin instead of the app-wide ExcludedProduct
        table (see that model's docstring for why they're kept
        separate).
        """
        db = SessionLocal()

        try:
            existing = db.get(OaSourceExcludedAsin, asin)

            if existing:
                if title:
                    existing.title = title
                if reason:
                    existing.reason = reason
            else:
                db.add(OaSourceExcludedAsin(asin=asin, title=title, reason=reason))

            db.commit()
        finally:
            db.close()

    @staticmethod
    def unexclude_asin(asin: str) -> None:
        db = SessionLocal()

        try:
            existing = db.get(OaSourceExcludedAsin, asin)

            if existing:
                db.delete(existing)
                db.commit()
        finally:
            db.close()

    @staticmethod
    def _recently_checked_asins(within_days: int) -> set:
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).replace(tzinfo=None)
            rows = (
                db.query(OaSourceCandidate.asin)
                .join(OaSourceRun, OaSourceCandidate.run_id == OaSourceRun.id)
                # Excludes test-harness runs (see OaSourceRun.is_test's
                # docstring) -- a step-3 comparison run must never make a
                # real ASIN look "recently checked" and suppress it from
                # the LIVE next-up-to-scan pool for SKIP_RECENTLY_CHECKED_DAYS.
                .filter(OaSourceRun.is_test == False)  # noqa: E712
                .filter(OaSourceCandidate.created_at >= cutoff)
                .distinct()
                .all()
            )
            return {row[0] for row in rows}
        finally:
            db.close()

    @staticmethod
    def _suggest_title_query(title: str) -> str:
        words = re.findall(r"[A-Za-z0-9&']+", title or "")
        significant = [w for w in words if w.lower() not in STOPWORDS]
        return " ".join(significant[:SUGGESTED_QUERY_WORD_COUNT])

    @staticmethod
    def build_queries(brand: str, title: str, ean: str, mpn: str) -> list:
        """
        Up to MAX_DISCOVERY_QUERIES_PER_ASIN queries, exactly the 3
        forms from the user's own spec: MPN UK, bare EAN, Brand+Title
        UK -- any tier whose underlying data is missing is simply
        skipped, which is also how this naturally respects "don't
        generate excessive searches" without a separate cap check.
        Returns [(tier, query), ...] so the caller knows which tier
        each result came from.
        """
        queries = []

        if mpn:
            queries.append(("mpn", f'"{mpn}" UK'))

        if ean:
            queries.append(("ean", f'"{ean}"'))

        title_query = OaSourceDiscoveryService._suggest_title_query(title)

        # Amazon titles routinely start with the brand name already
        # (e.g. "Philips Airfryer XL...") -- avoid sending Brave a
        # query with the brand word doubled up front.
        if brand and title_query.lower().startswith(brand.lower()):
            combined = title_query
        else:
            combined = f"{brand} {title_query}".strip()

        if combined:
            queries.append(("brand_title", f'"{combined}" UK'))

        return queries[:MAX_DISCOVERY_QUERIES_PER_ASIN]

    @staticmethod
    def _canonical_product_string(brand: str, title: str) -> str:
        """
        "brand + title" for the title-similarity tiers below -- but
        Amazon titles routinely already start with the brand name
        (e.g. "UbiQuiti USW-FLEX-2.5G-8-POE", "Philips Airfryer XL"),
        same observation _build_discovery_queries already made for its
        own Brave query text (see its own comment just above). Naively
        prepending the brand again doubles it up ("Ubiquiti Ubiquiti
        USW-Flex-2.5G-8-PoE"), which shortens/skews the string enough
        to move a SequenceMatcher ratio in either direction for no
        real reason -- confirmed 2026-08-29 on a real false positive
        (B0DV6CDXKT, see TRUSTED_MATCH_TIERS_AUTO_PROMOTE's docstring)
        where this was one contributing factor in a wrong listing
        narrowly clearing the similarity bar.
        """
        if brand and title.lower().startswith(brand.lower()):
            return title.lower()
        return f"{brand} {title}".lower()

    @staticmethod
    def classify_match(ean: str, mpn: str, brand: str, title: str,
                        verified_hits: dict, discovery_hit: dict) -> tuple:
        """
        The user's own 5-tier hierarchy, in order, never skipped past
        on a weaker signal alone:
          1. ean -- an exact-phrase site-restricted search for the EAN
             found a real hit on the candidate domain.
          2/3. mpn / brand_mpn -- same, for the MPN; brand_mpn if the
             brand name also appears in that hit's own title/description
             (stronger than the MPN string alone, which can coincide
             across unrelated listings for generic/short part numbers).
          4. brand_title -- no exact identifier confirmed at all, but
             the ORIGINAL discovery-query hit's title is a strong
             SequenceMatcher match against "brand + title" AND the
             brand name itself appears in it.
          5. title_only -- some title-similarity signal, but weaker
             than tier 4 (missing the brand confirmation, or below the
             strong-similarity bar). Per the user's explicit
             instruction, this is NEVER treated as a confirmed OA
             source -- callers must keep showing it as low-confidence,
             not silently drop or auto-promote it.

        verified_hits: {"ean": hit_or_None, "mpn": hit_or_None} from
        the site-restricted verification searches (see run_batch).
        discovery_hit: the original discovery-query search result the
        candidate domain came from (used for the title-similarity
        tiers, and as the retailer_title/url fallback when no
        verification hit exists).

        Returns (tier, hit_to_use) -- hit_to_use is whichever search
        result should populate retailer_title/retailer_url, or None if
        nothing at all matched (caller should not create a candidate
        row in that case).
        """
        ean_hit = verified_hits.get("ean")
        if ean_hit:
            return "ean", ean_hit

        mpn_hit = verified_hits.get("mpn")
        if mpn_hit:
            haystack = f"{mpn_hit.get('title', '')} {mpn_hit.get('description', '')}".lower()
            if brand and brand.lower() in haystack:
                return "brand_mpn", mpn_hit
            return "mpn", mpn_hit

        if not discovery_hit:
            return "", None

        haystack = f"{discovery_hit.get('title', '')} {discovery_hit.get('description', '')}"
        canonical = OaSourceDiscoveryService._canonical_product_string(brand, title)
        similarity = SequenceMatcher(None, canonical, haystack.lower()).ratio()
        brand_present = bool(brand) and brand.lower() in haystack.lower()

        if brand_present and similarity >= TITLE_SIMILARITY_STRONG:
            return "brand_title", discovery_hit

        return "title_only", discovery_hit

    @staticmethod
    def classify_shopping_match(ean: str, mpn: str, brand: str, title: str, result: dict) -> str:
        """
        The same 5-tier hierarchy as classify_match, adapted for a
        single Google Shopping result (see shopping_search.
        search_uk_shopping) rather than a Brave discovery hit + a
        separate site-restricted verification search. There IS no
        site-restricted EAN/MPN search available here -- SerpApi's
        shopping engine takes a free-text product query, not a
        site-scoped operator query the way Brave's Web Search does --
        so "ean"/"mpn" here means the literal EAN/MPN string happens to
        appear in the shopping listing's own title (retailers sometimes
        include it, e.g. for spare-part-sensitive categories), NOT a
        separately-verified hit. Still meaningfully stronger evidence
        than a bare title-similarity match, so it's scored the same as
        classify_match's own ean/mpn tiers.

        title_only is reachable here too, and per the same "never a
        confirmed match on its own" rule, is deliberately excluded from
        TRUSTED_MATCH_TIERS -- see that constant.
        """
        haystack = f"{result.get('title', '')} {result.get('source', '')}".lower()

        if ean and ean.lower() in haystack:
            return "ean"

        if mpn and mpn.lower() in haystack:
            if brand and brand.lower() in haystack:
                return "brand_mpn"
            return "mpn"

        canonical = OaSourceDiscoveryService._canonical_product_string(brand, title)
        similarity = SequenceMatcher(None, canonical, haystack).ratio()
        brand_present = bool(brand) and brand.lower() in haystack

        if brand_present and similarity >= TITLE_SIMILARITY_STRONG:
            return "brand_title"

        return "title_only"

    @staticmethod
    def _promote_if_qualifying(db, candidate, product, category_name: str, retailer_price_gbp: float,
                                dry_run: bool = False) -> bool:
        """
        Shared by run_batch (SerpApi auto-found prices) and
        update_candidate (a human confirming/overriding a price) --
        one place deciding whether a priced OA candidate is trustworthy
        AND profitable enough to become a REAL Atlas lead.

        The match-tier bar (TRUSTED_MATCH_TIERS_AUTO_PROMOTE -- EAN/
        MPN/brand+MPN only, tightened 2026-08-29 to exclude brand_title;
        see that constant's docstring) ONLY applies when
        candidate.price_source == "serpapi_auto" -- i.e. nobody has
        looked at this yet, so the algorithm's own match confidence is
        the only check there is. It does NOT apply when
        price_source == "manual": a human who opened the retailer link
        (or picked one from "other retailers found") and typed in what
        they verified themselves IS the trust check -- requiring an
        ALSO-trusted match_tier on top of that would block the user's
        own explicit ask ("if I find a better retailer... this gets
        added to the review queue"), and mirrors the original
        confirm_price's bar (ROI only, no match-tier gate) for exactly
        this reason. A brand_title match (Medium confidence -- no real
        identifier confirmed) still shows up on the OA results page,
        pre-priced, for a human to confirm via that manual path.

        Either way, ROI must clear FeeEngine.OA_TARGET_ROI_PCT (same
        25% bar as everywhere else in Atlas), AND margin/profit must
        clear OpportunityEngine.MIN_VIABLE_MARGIN_PCT/
        MIN_VIABLE_PROFIT_GBP (13%/£2, added 2026-08-23) -- deliberately NOT gated
        on OpportunityEngine's own BUY/CONSIDER recommendation, since a
        genuinely good OA find can legitimately land at CONSIDER (e.g.
        lower confidence from thin sales evidence) and still be exactly
        what ProductRepository.is_notable would treat as worth a look
        -- gating on ROI here (which _has_oa_potential/is_notable
        already require sales evidence for, upstream) is the correct
        bar, not BUY only.

        `product` must be a FULL Product (real trend/sales history from
        a fresh Keepa UK lookup -- see run_batch/update_candidate for
        where each builds one), not the minimal hypothetical the old
        confirm_price used to build -- OpportunityEngine.analyse needs
        real trend data to score meaningfully; a stripped-down Product
        would silently under/over-score.

        Always updates candidate.estimated_profit_gbp/estimated_roi_pct
        (even when not promoted, e.g. a trusted-tier match that's just
        not profitable enough) so the results page always reflects the
        real number for whatever price is currently confirmed.

        Idempotent -- does nothing on a second call for a candidate
        already promoted (candidate.added_to_review_queue), so
        re-confirming an unchanged price, or a Reclassify-style re-run,
        can't create duplicate ProductRecord rows for the same find.
        Returns True if this call newly promoted the candidate.

        dry_run (2026-08-29, added for the step-3 SerpApi-vs-Serper test
        harness -- see test_harness_search_providers.py): still runs
        every check above and still returns the real True/False
        "would this have qualified" answer, and still records
        candidate.estimated_profit_gbp/estimated_roi_pct either way
        (those are informational fields on the candidate row, harmless
        either way) -- but skips the two writes that make a promotion
        REAL: ProductRepository.save_opportunity and
        candidate.added_to_review_queue. A dry_run call can therefore
        measure "how many would have promoted" without creating a
        single real lead, Review Queue entry, or Discord alert.
        """
        priced_product = dataclass_replace(
            product,
            best_source_marketplace="UK-OA",
            best_source_cost_gbp=retailer_price_gbp,
            category_name=category_name,
        )
        fees = FeeEngine.calculate(priced_product, category_name=category_name)

        candidate.estimated_profit_gbp = fees.profit
        candidate.estimated_roi_pct = fees.roi

        already_promoted = candidate.added_to_review_queue
        match_trusted_enough = (
            candidate.price_source == "manual"
            or candidate.match_tier in TRUSTED_MATCH_TIERS_AUTO_PROMOTE
        )

        # Too-good-to-be-true backstop -- see
        # IMPLAUSIBLE_AUTO_PROMOTE_ROI_PCT and
        # TIERS_EXEMPT_FROM_ROI_CEILING. Applies to EVERY automated tier
        # since 2026-08-28, not just brand_title: an identifier match
        # proves the listing is about the right product, never that it is
        # the product. Only ever blocks the automated path; the row stays
        # on the results page for a human to confirm.
        implausible_auto_match = (
            candidate.price_source != "manual"
            and candidate.match_tier not in TIERS_EXEMPT_FROM_ROI_CEILING
            and fees.roi > IMPLAUSIBLE_AUTO_PROMOTE_ROI_PCT
        )

        # The retailer's own title says this is a box/spare/screen/used
        # unit rather than the product -- see NOT_THE_PRODUCT_PHRASES.
        # Same "block the robot, not the human" rule as above.
        not_the_product = (
            candidate.price_source != "manual"
            and looks_like_not_the_product(candidate.retailer_title)
        )

        qualifies = (
            not already_promoted
            and match_trusted_enough
            and not implausible_auto_match
            and not not_the_product
            and fees.roi > FeeEngine.OA_TARGET_ROI_PCT
            # Section 8.2 viability floor (2026-08-23): even at a
            # strong 25%+ ROI, also require the margin/absolute-profit
            # gate everywhere else in Atlas now enforces -- see
            # OpportunityEngine.MIN_VIABLE_MARGIN_PCT/
            # MIN_VIABLE_PROFIT_GBP's own docstring for why ROI alone
            # isn't enough.
            and fees.margin >= OpportunityEngine.MIN_VIABLE_MARGIN_PCT
            and fees.profit >= OpportunityEngine.MIN_VIABLE_PROFIT_GBP
        )

        if not qualifies:
            return False

        if dry_run:
            return True

        priced_product.profit = fees.profit
        priced_product.roi = fees.roi
        priced_product.margin = fees.margin
        priced_product.profit_90d = fees.profit_90d
        priced_product.roi_90d = fees.roi_90d
        priced_product.margin_90d = fees.margin_90d
        priced_product.profit_peak = fees.profit_peak
        priced_product.roi_peak = fees.roi_peak
        priced_product.margin_peak = fees.margin_peak
        priced_product.fba_fee = fees.fba_fee
        priced_product.referral_fee = fees.referral_fee
        priced_product.referral_rate_used = fees.referral_rate_used
        priced_product.uk_vat_rate_used = fees.uk_vat_rate_used
        priced_product.eu_vat_rate_used = fees.eu_vat_rate_used

        report = OpportunityEngine.analyse(priced_product)
        product_dict = asdict(priced_product)
        report_dict = asdict(report)
        ProductRepository.save_opportunity(product_dict, report_dict, "OA Source Discovery")

        # Match confidence doesn't travel through product_dict/
        # save_opportunity -- Product has no match_tier field, and
        # save_opportunity is shared by every other caller in Atlas
        # (the regular scan pipeline included), so adding OA-only
        # columns to its general signature isn't worth it for three
        # fields. Stamped directly onto the ProductRecord it just
        # created instead, so the Review Queue can show "how sure
        # Atlas actually was" on this specific lead -- see
        # review_queue_service._scan_lead_dict and the B0DV6CDXKT
        # false positive that prompted this (2026-08-29,
        # TRUSTED_MATCH_TIERS_AUTO_PROMOTE's docstring).
        new_record = (
            db.query(ProductRecord)
            .filter(ProductRecord.asin == priced_product.asin)
            .order_by(ProductRecord.scanned_at.desc())
            .first()
        )
        if new_record:
            new_record.match_tier = candidate.match_tier
            new_record.match_confidence_pct = candidate.match_confidence_pct
            new_record.source_confidence = candidate.source_confidence
            db.add(new_record)

        candidate.added_to_review_queue = True

        run = db.get(OaSourceRun, candidate.run_id)
        if run:
            run.asins_profitable += 1

        return True

    @staticmethod
    def run_batch(limit: int = 20, since_days: int = DEFAULT_DETECTION_WINDOW_DAYS,
                  test_mode: bool = False, test_asins: list = None, raw_products: list = None) -> dict:
        """
        test_mode/test_asins/raw_products (2026-08-29, added for the
        step-3 SerpApi-vs-Serper test harness -- see
        test_harness_search_providers.py, NOT used by the live
        "Run batch" button or the scheduler, both of which omit all
        three and get EXACTLY the pre-existing behaviour):

        - test_mode=True marks the created OaSourceRun as
          is_test=True (see that column's docstring in
          app/database/models.py for every place that then excludes
          it) and passes dry_run=True into _promote_if_qualifying, so
          a test run can never write a real ProductRecord, Review
          Queue entry, or Discord alert -- only OaSourceRun/
          OaSourceCandidate rows the harness itself reads back.
        - test_asins, when given, is used VERBATIM instead of calling
          get_candidate_asins -- so the harness can run the SAME fixed
          ASIN list through two separate test_mode calls (one per
          shopping_search provider) and get a genuine apples-to-apples
          comparison, rather than each call independently (and
          possibly differently) selecting "the next 25 eligible ASINs".
        - raw_products, when given, is used instead of this method
          fetching its own Keepa data -- the provider comparison only
          differs in the shopping-search step below; the Keepa-sourced
          product data (title/brand/ean/mpn/category/price) is
          identical either way, so a caller comparing two providers on
          the same ASINs can fetch it ONCE and pass it to both calls,
          rather than this method spending a second real Keepa lookup
          per ASIN purely to re-fetch data it already has.

        Runs the full pipeline against up to `limit` "OA / unclear"
        ASINs detected in the last `since_days` days (0 = no window --
        see DEFAULT_DETECTION_WINDOW_DAYS; the page posts back whatever
        window it was displaying, so a run always searches exactly the
        rows the preview promised). Spends real Keepa tokens (one fresh full UK lookup per
        ASIN, batched into a single Keepa call -- needed because
        nothing in Atlas has ever persisted an MPN/model field, so
        this is the one place that actually looks for it).

        Per ASIN, tries TWO search paths, in order (2026-08-19):

        1. Google Shopping via shopping_search.search_uk_shopping --
           ONE call, returns MULTIPLE real retailers with their own
           real prices for one query, unlike Brave's plain web search.
           Which provider actually serves that call (SerpApi or Serper)
           is config, see shopping_search.OA_SHOPPING_PROVIDER.
           Every result is scored via classify_shopping_match; the
           CHEAPEST result whose tier is in TRUSTED_MATCH_TIERS wins
           (never title_only alone -- per the user's own explicit
           choice) -- this is what actually answers "find me the best
           (cheapest) genuine price", not just the first/strongest text
           match. A winning result auto-populates the candidate's price
           immediately (no manual confirm needed) and is passed to
           _promote_if_qualifying, which adds it straight to the real
           Review Queue if it also clears FeeEngine.OA_TARGET_ROI_PCT.
           ALL shopping results (trusted or not) are kept as
           shopping_candidates_json so a human can still see/pick a
           good-but-untrusted match (e.g. the exact "Argos was
           auto-picked, but Halfords was actually cheaper and is the
           real match" scenario the user described) via update_candidate.

        2. The existing Brave discovery+verification pipeline -- ONLY
           run when Google Shopping found nothing at a trusted tier
           (no key configured, no results, or every result was
           title-only) -- preserves the original "find the retailer
           page, human confirms the price" behaviour for the harder
           cases SerpApi's free-text shopping search can't resolve.

        Both APIs degrade gracefully with no key configured -- SerpApi
        and Brave both return [] immediately rather than raising (see
        each client's own docstring) -- so a run with neither key set
        still completes and clearly shows 0 retailers found rather than
        erroring. search_count/estimated_cost_usd (Brave) and
        serpapi_search_count/serpapi_searches_left (SerpApi) are
        tracked separately since they bill completely differently
        (Brave: $/1000 searches; SerpApi: fixed monthly quota).

        Quota safety buffer (2026-08-19, extended 2026-08-29): remaining
        SerpApi quota is checked once up front via
        shopping_search.get_account_status() (confirmed not to cost a
        search credit) and tracked locally as each search is spent.
        Once it drops to/below SERPAPI_QUOTA_SAFETY_BUFFER, this run
        stops calling SerpApi for its remaining ASINs -- but instead of
        giving up on a Google-Shopping-style search entirely (the
        original 2026-08-19 behaviour), it now reaches for Serper
        specifically (shopping_search.search_uk_shopping_via("serper",
        ...)) for the rest of the batch, per Tamara's own instruction:
        "use both, switch to the other when we're out of free credits
        on one." Serper has no monthly cliff (prepaid credits) so it's
        never itself subject to this buffer. run.serpapi_quota_stopped
        still records that the switch happened, and
        run.serper_search_count / OaSourceCandidate.shopping_provider
        record how much of the run actually ran on Serper as a result
        -- see those fields' own docstrings, added specifically so a
        month-from-now evaluation of this switch can tell the two
        providers' real leads apart. Brave remains the fallback ONLY
        when Serper (or SerpApi, before the buffer was hit) also finds
        nothing at a trusted match tier for a given ASIN -- unchanged.
        If the SerpApi account-status lookup itself fails (no key, or
        SerpApi's own account endpoint errors), remaining quota is
        treated as unknown and the buffer is never enforced -- SerpApi
        calls proceed as normal and simply fail/degrade per-call as
        they already did before this feature existed.
        """
        db = SessionLocal()

        run = OaSourceRun(status="running", is_test=test_mode)
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        try:
            asins = list(test_asins) if test_asins is not None else OaSourceDiscoveryService.get_candidate_asins(limit, since_days)
            run.asins_targeted = len(asins)
            db.commit()

            if not asins:
                run.status = "done"
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
                # Name the window in the message -- with one on by
                # default, "nothing available" is far more often "the
                # last 7 days are exhausted" than "the pool is empty",
                # and the two need very different responses from the
                # user (widen the window vs wait for new detections).
                window_note = f" detected in the last {since_days} days" if since_days else ""
                return {
                    "run_id": run_id,
                    "asins_targeted": 0,
                    "message": f"No notable 'OA / unclear' ASINs{window_note} available right now.",
                }

            service = ProductService()
            if raw_products is None:
                raw_products = service.get_products(asins, "UK", full=True, usage_category="oa_discovery")
            category_names = get_category_names(service.api)

            search_count = 0
            serpapi_search_count = 0
            serper_search_count = 0
            with_retailer = 0
            with_exact_match = 0
            auto_priced = 0
            # Only actually used in test_mode -- see the run.asins_profitable
            # assignment near the end of this method. In real (non-test)
            # runs, _promote_if_qualifying already increments
            # run.asins_profitable itself; in test_mode it can't (dry_run
            # skips that write on purpose), so this local counter is how
            # the harness finds out "how many WOULD have promoted".
            would_promote = 0

            # Live remaining quota, checked once up front (doesn't cost
            # a credit -- see get_account_status's own docstring) and
            # decremented locally as this run spends searches. None
            # means "unknown" (no key, or the lookup itself failed) --
            # in that case the buffer below is simply never triggered,
            # same as if this feature didn't exist.
            serpapi_quota_remaining = shopping_search.get_account_status().get("plan_searches_left")
            serpapi_quota_stopped = False

            for raw in raw_products:
                asin = raw.get("asin") or ""
                if not asin:
                    continue

                product = ProductMapper.from_keepa(raw)
                category_name = category_names.get(product.category, "")
                mpn = raw.get("model") or raw.get("partNumber") or ""

                # Target buy price -- highest price payable for stock
                # and still clear FeeEngine.OA_TARGET_ROI_PCT, shown on
                # the results table so the user has a guardrail before
                # going to verify a retailer's real price. Computed
                # twice per the user's own request (2026-08-19): once
                # against TODAY's Amazon price, and once against the
                # 30-day average (KeepaParser.price_avg(30), already
                # available for free -- stats=90 was requested for this
                # same `raw` product, and Keepa's avg30 is populated
                # whenever the requested stats window is >= 30 days) --
                # a competitor's purchase decision may have been made
                # against a recent price that was higher than today's,
                # so the 30d figure can be a more realistic/conservative
                # target than today's alone.
                target_price_today = FeeEngine.max_source_cost(
                    product.buy_box_now, category_name, product.fba_fee, FeeEngine.OA_TARGET_ROI_PCT,
                )
                buy_box_avg_30d = KeepaParser(raw).price_avg(30)
                target_price_30d = (
                    FeeEngine.max_source_cost(
                        buy_box_avg_30d, category_name, product.fba_fee, FeeEngine.OA_TARGET_ROI_PCT,
                    )
                    if buy_box_avg_30d else 0.0
                )

                candidate = OaSourceCandidate(
                    run_id=run_id,
                    asin=asin,
                    title=product.title,
                    brand=product.brand,
                    ean=product.ean,
                    mpn=mpn,
                    amazon_price_gbp=product.buy_box_now,
                    category_name=category_name,
                    target_price_today_gbp=target_price_today,
                    buy_box_avg_30d_gbp=buy_box_avg_30d,
                    target_price_30d_avg_gbp=target_price_30d,
                )

                # --- Path 1: Google Shopping (SerpApi) ---
                shopping_query = OaSourceDiscoveryService._suggest_title_query(product.title)
                if product.brand and not shopping_query.lower().startswith(product.brand.lower()):
                    shopping_query = f"{product.brand} {shopping_query}".strip()

                quota_buffer_hit = (
                    serpapi_quota_remaining is not None
                    and serpapi_quota_remaining <= SERPAPI_QUOTA_SAFETY_BUFFER
                )

                # Which client actually served this ASIN's shopping
                # search, if any -- "" until set below. Recorded onto
                # the candidate (see OaSourceCandidate.shopping_provider)
                # only if a trusted match is actually found further
                # down; kept as a local var here since that's the one
                # place both branches below agree on what really ran.
                shopping_provider_used = ""

                if not shopping_query:
                    # No usable query text -- no call made against
                    # either provider, nothing to count against anyone's
                    # quota.
                    shopping_results = []
                elif quota_buffer_hit:
                    # SerpApi's monthly reserve is hit -- reach for
                    # Serper instead of giving up on a Google-Shopping-
                    # style search entirely for the rest of this batch
                    # (2026-08-29, Tamara's own instruction -- see this
                    # method's docstring and
                    # shopping_search.search_uk_shopping_via). Serper is
                    # prepaid credits with no monthly cliff, so no
                    # equivalent buffer applies to it here.
                    serpapi_quota_stopped = True
                    shopping_results = shopping_search.search_uk_shopping_via("serper", shopping_query)
                    serper_search_count += 1
                    shopping_provider_used = "serper"
                else:
                    shopping_results = shopping_search.search_uk_shopping(shopping_query)
                    serpapi_search_count += 1
                    shopping_provider_used = shopping_search.active_provider_name()
                    if serpapi_quota_remaining is not None:
                        serpapi_quota_remaining -= 1

                best_shopping = None
                best_shopping_tier = ""

                # Drop marketplaces, used-goods resellers, import
                # concierges and foreign storefronts BEFORE anything is
                # scored or stored (2026-08-28) -- see
                # classify_shopping_source. Filtered here rather than
                # only at auto-pick time so an excluded source can't
                # reach the "other retailers found" picker either: it
                # isn't a viable OA source whether Atlas chooses it or
                # a human does, so offering it as a one-click promote
                # would just reintroduce the same bad lead by the
                # manual route.
                shopping_results = [
                    r for r in shopping_results
                    if classify_shopping_source(r.get("source", ""))[1] == "candidate"
                ]

                if shopping_results:
                    candidate.shopping_candidates_json = json.dumps(shopping_results)

                    scored = [
                        (r, OaSourceDiscoveryService.classify_shopping_match(
                            product.ean, mpn, product.brand, product.title, r,
                        ))
                        for r in shopping_results
                    ]
                    trusted = [(r, t) for r, t in scored if t in TRUSTED_MATCH_TIERS]

                    if trusted:
                        # Cheapest wins, per the user's own "find the
                        # best price" instruction -- ties broken by
                        # strongest match tier.
                        trusted.sort(
                            key=lambda rt: (rt[0]["extracted_price"], list(MATCH_TIER_SCORES).index(rt[1]))
                        )
                        best_shopping, best_shopping_tier = trusted[0]

                if best_shopping:
                    confidence_pct, source_confidence = MATCH_TIER_SCORES[best_shopping_tier]
                    candidate.outcome = "candidate_found"
                    candidate.retailer_domain = best_shopping.get("source", "")
                    candidate.retailer_url = best_shopping.get("product_link", "")
                    candidate.retailer_title = best_shopping.get("title", "")
                    candidate.retailer_price_gbp = best_shopping["extracted_price"]
                    candidate.match_tier = best_shopping_tier
                    candidate.match_confidence_pct = confidence_pct
                    candidate.source_confidence = source_confidence
                    candidate.price_source = "serpapi_auto"
                    candidate.shopping_provider = shopping_provider_used
                    candidate.queries_used_json = json.dumps([shopping_query])

                    with_retailer += 1
                    auto_priced += 1
                    if best_shopping_tier in ("ean", "mpn", "brand_mpn"):
                        with_exact_match += 1

                    db.add(candidate)
                    db.commit()

                    if OaSourceDiscoveryService._promote_if_qualifying(
                        db, candidate, product, category_name, candidate.retailer_price_gbp,
                        dry_run=test_mode,
                    ):
                        would_promote += 1
                    db.commit()

                    continue

                # --- Path 2: Brave discovery + verification (fallback -- unchanged pipeline) ---
                queries = OaSourceDiscoveryService.build_queries(
                    product.brand, product.title, product.ean, mpn
                )

                queries_used = []
                discovery_results_by_tier = {}

                for tier, query in queries:
                    results = brave_search_client.search(query)
                    search_count += 1
                    queries_used.append(query)
                    discovery_results_by_tier[tier] = results

                # Distinct candidate domains across every discovery
                # query's results, keeping the first hit seen per
                # domain (and which tier's query surfaced it, so an
                # EAN/MPN-query domain is verified before a
                # brand_title-only one -- see the sort below).
                domain_hits = {}
                domain_tier_rank = {"ean": 0, "mpn": 1, "brand_title": 2}

                for tier, results in discovery_results_by_tier.items():
                    for hit in results:
                        domain, category = classify_domain(hit.get("url", ""))
                        if category != "candidate" or not domain:
                            continue
                        if domain not in domain_hits:
                            domain_hits[domain] = {"hit": hit, "tier": tier}

                ranked_domains = sorted(
                    domain_hits.items(),
                    key=lambda kv: domain_tier_rank.get(kv[1]["tier"], 9),
                )[:MAX_RETAILERS_VERIFIED_PER_ASIN]

                best_tier = ""
                best_hit = None
                best_domain = ""

                for domain, info in ranked_domains:
                    verified_hits = {}

                    if product.ean:
                        vq = f'site:{domain} "{product.ean}"'
                        vresults = brave_search_client.search(vq, count=5)
                        search_count += 1
                        queries_used.append(vq)
                        verified_hits["ean"] = next(
                            (h for h in vresults if classify_domain(h.get("url", ""))[0] == domain), None
                        )

                    if mpn:
                        vq = f'site:{domain} "{mpn}"'
                        vresults = brave_search_client.search(vq, count=5)
                        search_count += 1
                        queries_used.append(vq)
                        verified_hits["mpn"] = next(
                            (h for h in vresults if classify_domain(h.get("url", ""))[0] == domain), None
                        )

                    tier, hit = OaSourceDiscoveryService.classify_match(
                        product.ean, mpn, product.brand, product.title,
                        verified_hits, info["hit"],
                    )

                    if not tier:
                        continue

                    # Keep the strongest tier seen across every
                    # verified domain for this ASIN -- MATCH_TIER_SCORES
                    # order below doubles as the priority order.
                    if best_tier == "" or list(MATCH_TIER_SCORES).index(tier) < list(MATCH_TIER_SCORES).index(best_tier):
                        best_tier = tier
                        best_hit = hit
                        best_domain = domain

                candidate.queries_used_json = json.dumps(queries_used)

                if best_tier and best_hit:
                    confidence_pct, source_confidence = MATCH_TIER_SCORES[best_tier]
                    candidate.outcome = "candidate_found"
                    candidate.retailer_domain = best_domain
                    candidate.retailer_url = best_hit.get("url", "")
                    candidate.retailer_title = best_hit.get("title", "")
                    candidate.match_tier = best_tier
                    candidate.match_confidence_pct = confidence_pct
                    candidate.source_confidence = source_confidence

                    with_retailer += 1
                    if best_tier in ("ean", "mpn", "brand_mpn"):
                        with_exact_match += 1

                db.add(candidate)
                db.commit()

            run.status = "done"
            run.asins_searched = len(raw_products)
            run.asins_with_retailer = with_retailer
            run.asins_with_exact_match = with_exact_match
            run.asins_auto_priced = auto_priced
            if test_mode:
                # The real (non-test) path already accumulated this
                # inside _promote_if_qualifying itself; dry_run mode
                # skips that write on purpose, so this is the only
                # place test_mode's count gets recorded.
                run.asins_profitable = would_promote
            run.search_count = search_count
            run.estimated_cost_usd = round(search_count / 1000 * brave_search_client.COST_PER_1000_USD, 2)
            run.serpapi_search_count = serpapi_search_count
            run.serper_search_count = serper_search_count
            run.serpapi_searches_left = shopping_search.get_account_status().get("plan_searches_left")
            run.serpapi_quota_stopped = serpapi_quota_stopped
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(run)

            if not test_mode:
                ActivityLog.record(
                    "oa_discovery_run",
                    f"{run.asins_searched} ASIN(s) searched, {auto_priced} auto-priced",
                )

            return {
                "run_id": run_id,
                "asins_targeted": run.asins_targeted,
                "asins_searched": run.asins_searched,
                "asins_with_retailer": with_retailer,
                "asins_with_exact_match": with_exact_match,
                "asins_auto_priced": auto_priced,
                "asins_profitable": run.asins_profitable,
                "search_count": search_count,
                "estimated_cost_usd": run.estimated_cost_usd,
                "serpapi_search_count": serpapi_search_count,
                "serper_search_count": serper_search_count,
                "serpapi_searches_left": run.serpapi_searches_left,
                "serpapi_quota_stopped": serpapi_quota_stopped,
            }

        except Exception as exc:
            db.rollback()
            run.status = "error"
            run.error = str(exc)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            print(f"OA Source Discovery run {run_id} failed: {exc}")
            return {"run_id": run_id, "error": str(exc)}

        finally:
            db.close()

    @staticmethod
    def count_awaiting_review() -> int:
        """
        Candidates that have a real price attached (auto-found by
        Google Shopping OR manually confirmed -- price_source != "")
        but weren't strong enough to auto-promote into the Review
        Queue (added_to_review_queue is still False). These are
        exactly the ones sitting there needing a human judgment call:
        keep looking, swap in a different retailer from
        shopping_candidates_json, or move on. Across ALL runs, not
        just the latest one -- an older run's un-promoted candidate is
        just as much "awaiting review" as a fresh one, and this is a
        cheap COUNT-only query for a Dashboard tile, not a page render.
        """
        db = SessionLocal()

        try:
            return (
                db.query(func.count(OaSourceCandidate.id))
                .join(OaSourceRun, OaSourceCandidate.run_id == OaSourceRun.id)
                # Excludes test-harness runs -- see OaSourceRun.is_test's
                # docstring. A dry_run candidate can have price_source
                # set and added_to_review_queue still False (that's the
                # whole point of dry_run), which would otherwise inflate
                # this Dashboard tile with rows nobody actually needs to
                # review.
                .filter(OaSourceRun.is_test == False)  # noqa: E712
                .filter(OaSourceCandidate.price_source != "")
                .filter(OaSourceCandidate.added_to_review_queue == False)  # noqa: E712
                .scalar()
            ) or 0
        finally:
            db.close()

    @staticmethod
    def latest_run_quota_status() -> dict | None:
        """
        Quick snapshot of the most recent OA Source Discovery run's
        SerpApi state -- for a Dashboard warning if the last run had
        to fall back to the free Brave pipeline early to protect quota
        (see SERPAPI_QUOTA_SAFETY_BUFFER), without needing a full page
        visit to notice. None if no run has ever happened.
        """
        db = SessionLocal()

        try:
            # Excludes test-harness runs -- see OaSourceRun.is_test's
            # docstring. Without this, a step-3 comparison run would
            # become "the latest run" and the Dashboard's SerpApi
            # quota-warning tile would reflect the harness's own
            # quota usage instead of the live pipeline's.
            run = (
                db.query(OaSourceRun)
                .filter(OaSourceRun.is_test == False)  # noqa: E712
                .order_by(OaSourceRun.started_at.desc())
                .first()
            )
            if not run:
                return None

            return {
                "quota_stopped": run.serpapi_quota_stopped,
                "searches_left": run.serpapi_searches_left,
            }
        finally:
            db.close()

    @staticmethod
    def update_candidate(candidate_id: int, retailer_domain: str, retailer_url: str,
                          retailer_price_gbp: float, retailer_title: str = "",
                          retailer_stock_text: str = "") -> dict:
        """
        Records a human-confirmed (or human-overridden) retailer for a
        candidate -- replaces the old confirm_price (2026-08-19): takes
        the full retailer identity (name/URL), not just a price, so the
        user can swap in a DIFFERENT, better retailer they found
        themselves, not only confirm the one Atlas auto-picked -- the
        exact scenario the user described (Atlas auto-picked Argos for
        a Garmin product; the user found a genuinely cheaper price at
        Halfords and is confident that's really where the competitor
        bought it).

        Fetches a FRESH full Keepa UK lookup for the candidate's ASIN
        (one Keepa token -- acceptable here since this is a deliberate
        one-off human action, not a batch loop) so _promote_if_qualifying
        has real trend/sales data to score with, rather than trusting a
        possibly-incomplete lightweight ProductRecord (see
        FeeEngine.max_source_cost's own docstring on why those records'
        non-price fields aren't trustworthy). Falls back to the OLD
        FeeEngine-only-no-scoring behaviour if Keepa returns nothing for
        the ASIN (e.g. a transient API issue) -- profit/ROI still get
        computed and shown, it just can't be auto-promoted to the
        Review Queue without real trend data to score against.

        marks price_source="manual" regardless of whether this matches
        what Google Shopping already auto-found, since the human is now
        the one vouching for it.
        """
        db = SessionLocal()

        try:
            candidate = db.get(OaSourceCandidate, candidate_id)
            if not candidate:
                return {"error": "Candidate not found"}

            candidate.retailer_domain = retailer_domain or candidate.retailer_domain
            candidate.retailer_url = retailer_url or candidate.retailer_url
            if retailer_title:
                candidate.retailer_title = retailer_title
            candidate.retailer_price_gbp = retailer_price_gbp
            if retailer_stock_text:
                candidate.retailer_stock_text = retailer_stock_text
            candidate.price_source = "manual"
            candidate.outcome = "candidate_found"

            service = ProductService()
            raw_products = service.get_products([candidate.asin], "UK", full=True, usage_category="oa_discovery")

            promoted = False

            if raw_products:
                product = ProductMapper.from_keepa(raw_products[0])
                category_names = get_category_names(service.api)
                category_name = category_names.get(product.category, "") or candidate.category_name

                promoted = OaSourceDiscoveryService._promote_if_qualifying(
                    db, candidate, product, category_name, retailer_price_gbp,
                )
            else:
                # Degraded fallback -- no real trend data available,
                # so this can only compute profit/ROI, never promote.
                record = ProductRepository.get_last_eu_check(candidate.asin)
                fba_fee = record.fba_fee if record else 0.0

                hypothetical = Product(
                    asin=candidate.asin, title=candidate.title, brand=candidate.brand,
                    category="", ean=candidate.ean,
                    buy_box_now=candidate.amazon_price_gbp,
                    fba_fee=fba_fee,
                    best_source_marketplace="UK-OA",
                    best_source_cost_gbp=retailer_price_gbp,
                )
                fees = FeeEngine.calculate(hypothetical, category_name=candidate.category_name)
                candidate.estimated_profit_gbp = fees.profit
                candidate.estimated_roi_pct = fees.roi

            db.commit()

            return {
                "profit": candidate.estimated_profit_gbp,
                "roi": candidate.estimated_roi_pct,
                "promoted": promoted,
            }

        finally:
            db.close()
