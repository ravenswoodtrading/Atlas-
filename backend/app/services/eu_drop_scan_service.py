"""
EU price-drop scan (2026-09-20, experiment) -- NON-brand discovery: find
products whose Buy Box just dropped on an EU marketplace within a chosen
category, then run those ASINs through the normal BrandScanService
pipeline so anything worth buying lands in the Review Queue like every
other scan result (saved as ProductRecords, brand_query
"eu-drop:<marketplace>:<category>").

Why it exists: the brand-by-brand Scan Queue re-walks the same catalogues
and its yield has collapsed, while ~50% of the Keepa refill goes unused.
Product Finder costs ~0.11 tokens per ASIN vs ~7 for a full evaluation,
so filtering there is the cheap lever. Roughly half of good leads are
structural spreads with no price drop at all (253 of 503 BUY/CONSIDER
records in the 30 days to 2026-09-19 had today's ROI at or above their
90-day typical) -- this finds the OTHER half, it doesn't replace brand
scans.

Category choice (from 531 EU A2A purchases, 2026-09-20): the five
categories below had the best mix of purchase profit, brand diversity and
Atlas's own BUY/CONSIDER yield. Health & Personal Care was deliberately
left out (89% of its profit is Philips, already brand-scanned) and Beauty
(GBP 3.3 profit per unit). Keepa root category IDs are per marketplace;
only DE is filled in so far (FR/ES/IT roots were looked up but not yet
mapped to these categories).

Free pre-check: before spending any Keepa tokens, SP-API (free) prices each
candidate on the UK and the source marketplace and drops it ONLY when both
prices come back and UK/source is below MIN_PRICE_RATIO -- 90% of real
leads sit at 1.4x or better. If SP-API can't answer either side the
candidate goes through anyway: BrandScanService's own docs record SP-API
missing real offers ~22% of the time (never the reverse), so silence is
not evidence.

Mains-plug filter: candidates whose UK title names a mains-plug device
(chargers, routers, monitors, printers, kitchen appliances, power tools --
see sourcing_classifier.title_suggests_mains_plug) are dropped by the scan
right after the UK lookup, before any EU lookup is paid, and listed in each
category's report so the keyword list can be tuned. Added on Tamara's
"be careful with plugs for computers" (2026-09-20); errs towards dropping.

Manual/on-demand only -- nothing schedules this.
"""
import time

from app.services.brand_scan_service import BrandScanService, RESCAN_COOLDOWN_HOURS
from app.services.currency_service import CurrencyService
from app.services.product_finder import ProductFinder
from app.services.product_mapper import MARKETPLACE_CURRENCY
from app.services.product_repository import ProductRepository
from app.services.restriction_service import RestrictionService
from app.services.scan_coordinator import ScanCoordinator
from app.sp_api.client import get_sp_api_client

EU_DROP_CATEGORIES = {
    "computers": dict(label="Computers & Accessories", ids={"DE": "340843031"}),
    "home_kitchen": dict(label="Home & Kitchen", ids={"DE": "3167641"}),
    "games": dict(label="PC & Video Games", ids={"DE": "300992"}),
    # 104 purchases but only GBP 5.9 profit per unit -- needs a higher floor.
    "toys": dict(label="Toys & Games", ids={"DE": "12950651"}, min_price=25),
    "diy": dict(label="DIY & Tools", ids={"DE": "80084031"}),
}

# Category pickers for the Keepa Searches form -- Keepa root category IDs per
# marketplace, from category_lookup on 2026-09-20. Only DE has been RUN so far
# (see EU_DROP_CATEGORIES); FR/ES/IT are the matching roots by name and are
# untried. ES "Hogar y cocina" wasn't captured, so it isn't listed -- enter
# that ID by hand.
EU_DROP_CATEGORY_PRESETS = {
    "DE": [("Computer & Zubehoer (Computers)", "340843031"), ("Kueche, Haushalt & Wohnen (Home & Kitchen)", "3167641"),
           ("Games", "300992"), ("Spielzeug (Toys)", "12950651"), ("Baumarkt (DIY & Tools)", "80084031")],
    "FR": [("Informatique (Computers)", "340858031"), ("Cuisine et Maison (Home & Kitchen)", "57004031"),
           ("Jeux video (Games)", "530490"), ("Jeux et Jouets (Toys)", "322086011"), ("Bricolage (DIY & Tools)", "590748031")],
    "ES": [("Informatica (Computers)", "667049031"), ("Videojuegos (Games)", "599382031"),
           ("Juguetes y juegos (Toys)", "599385031"), ("Bricolaje y herramientas (DIY & Tools)", "2454133031")],
    "IT": [("Informatica (Computers)", "425916031"), ("Casa e cucina (Home & Kitchen)", "524015031"),
           ("Videogiochi (Games)", "412603031"), ("Giochi e giocattoli (Toys)", "523997031"),
           ("Fai da te (DIY & Tools)", "2454160031")],
}
MARKETPLACES = list(EU_DROP_CATEGORY_PRESETS)

MIN_PRICE_RATIO = 1.4

# Extra headroom (on top of MIN_TOKEN_BUFFER) left for the Scan Queue and the
# daily Replen/Watchlist safety net -- this experiment must never drain the
# account they share.
TOKEN_RESERVE = 200

GOOD = ("BUY", "CONSIDER")

# The free SP-API price check is only an optimisation, so it gets a time budget:
# past it, the remaining candidates are left for the next run (not dropped).
PRECHECK_BUDGET_SECONDS = 240

# The paid scan needs the shared manual-scan lock, which the daily Replen/Watchlist
# safety net holds for 20+ minutes (and re-takes on every server restart). Wait for
# it in short polls so the run can say what it's waiting for, and give up cleanly.
LOCK_WAIT_SECONDS = 900
LOCK_POLL_SECONDS = 30

# Free Amazon listing-restriction check (see RestrictionService) that runs BEFORE the price check
# and any Keepa spend: ~0.5s per uncached candidate, ~100 candidates per search.
RESTRICTION_BUDGET_SECONDS = 120


class EuDropScanService:
    @staticmethod
    def ratio_precheck(asins: list, sp_client, marketplace: str, stop_after: int = None,
                       budget_seconds: float = None, progress=None):
        """Returns (kept_asins, stats). Never drops on missing data -- see
        the module docstring. Stops early (stats["budget_hit"]) once
        `budget_seconds` has elapsed; the unchecked rest are simply not
        examined, so the caller must not treat them as looked at."""
        currency = MARKETPLACE_CURRENCY.get(marketplace, "EUR")
        stats = dict(checked=0, dropped_low_ratio=0, kept_ratio_ok=0, kept_unknown=0)
        kept = []
        started = time.monotonic()

        for asin in asins:
            if stop_after is not None and len(kept) >= stop_after:
                break
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                stats["budget_hit"] = True
                break
            if progress and stats["checked"] % 5 == 0:
                progress(f"Free price check {stats['checked']}/{len(asins)}")

            stats["checked"] += 1
            uk = sp_client.get_item_offers(asin, "UK")
            uk_price = uk.get("price") if uk else None

            source_price = None
            if uk_price:
                source = sp_client.get_item_offers(asin, marketplace)
                source_price = source.get("price") if source else None

            if uk_price and source_price:
                source_gbp = CurrencyService.to_gbp(source_price, currency)
                if source_gbp and uk_price / source_gbp < MIN_PRICE_RATIO:
                    stats["dropped_low_ratio"] += 1
                    continue
                stats["kept_ratio_ok"] += 1
            else:
                stats["kept_unknown"] += 1

            kept.append(asin)

        return kept, stats

    @staticmethod
    def run_category(marketplace: str, category_id, label: str, key: str, per_category: int = 60,
                     min_price: int = 20, max_price: int = 150, drop30_pct: int = 20, drop90_pct: int = 15,
                     min_rank_drops30: int = 10, skip_asins=None, use_ratio_precheck: bool = True,
                     finder=None, sp_client=None, excluded=None, progress=None) -> dict:
        """
        One configured search: Finder -> free SP-API ratio pre-check -> the
        normal scan. `category_id` is one marketplace root ID or a list of
        them. `skip_asins` are ASINs a previous run of this same search
        already examined (a Signal Query's snapshot), so a repeat run only
        looks at NEW price drops.

        Returns a report row. `examined_asins` is what the caller should
        remember as "already looked at": everything checked plus everything
        never meant to be scanned -- but NOT candidates cut off by
        per_category, and NOT ones whose scan couldn't finish (low tokens),
        since those still deserve a look next time.
        """
        finder = finder or ProductFinder()
        if sp_client is None and use_ratio_precheck:
            sp_client = get_sp_api_client()
        if excluded is None:
            excluded = ProductRepository.get_excluded_asins()
        seen = set(skip_asins or ())
        say = progress or (lambda _stage: None)

        row = dict(key=key, label=label)
        say("Searching Keepa for price drops")
        tokens_before = finder.api.tokens_left
        found = finder.find_eu_price_drops(
            marketplace, category_id, min_price=min_price, max_price=max_price, drop30_pct=drop30_pct,
            drop90_pct=drop90_pct, min_rank_drops30=min_rank_drops30,
        )
        if found is None:
            row["error"] = "Product Finder request failed"
            return row

        # Re-read every call: an earlier category's scan just saved records.
        recent = ProductRepository.get_recently_scanned_asins(RESCAN_COOLDOWN_HOURS)
        never_scan = [a for a in found if a in excluded or a in recent or a in seen]
        fresh = [a for a in found if a not in excluded and a not in recent and a not in seen]
        row.update(finder_results=len(found), fresh_candidates=len(fresh))

        # Products Amazon says this account can't list are dropped before any price check or Keepa
        # spend -- and remembered as looked-at, since their answer won't change for days.
        row["restricted_dropped"] = 0
        if fresh:
            say("Checking Amazon listing restrictions (free)")
            fresh, restricted = RestrictionService.filter_unrestricted(
                fresh, budget_seconds=RESTRICTION_BUDGET_SECONDS, sp_client=sp_client,
            )
            row["restricted_dropped"] = len(restricted)
            never_scan = never_scan + restricted

        if sp_client:
            kept, row["precheck"] = EuDropScanService.ratio_precheck(
                fresh, sp_client, marketplace, stop_after=per_category,
                budget_seconds=PRECHECK_BUDGET_SECONDS, progress=say)
            examined = fresh[:row["precheck"]["checked"]]
        else:
            kept = fresh[:per_category]
            examined = list(kept)
        kept = kept[:per_category]
        row["sent_to_scan"] = len(kept)

        unfinished = False
        if kept:
            scan_label = f"eu-drop:{marketplace.lower()}:{key}"
            acquired = False
            waited = 0
            while waited < LOCK_WAIT_SECONDS:
                if ScanCoordinator.acquire_for_manual_scan(timeout=LOCK_POLL_SECONDS):
                    acquired = True
                    break
                waited += LOCK_POLL_SECONDS
                say(f"Waiting for another scan to finish ({ScanCoordinator.busy_reason()})")

            if not acquired:
                row["error"] = (f"Another scan ({ScanCoordinator.busy_reason()}) was still running after "
                                f"{LOCK_WAIT_SECONDS // 60} minutes, so nothing was scanned -- run this search again later.")
                unfinished = True
                result = None
            else:
                say(f"Scanning {len(kept)} products (uses Keepa tokens)")
                try:
                    scanner = BrandScanService(token_reserve=TOKEN_RESERVE, usage_category="eu_drop_scan")
                    result = scanner.scan(scan_label, limit=len(kept), asins=kept)
                finally:
                    ScanCoordinator.release_after_manual_scan()

        if kept and result is not None:
            opportunities = result.get("opportunities") or []
            by_rec = {}
            for o in opportunities:
                rec = o["report"]["recommendation"]
                by_rec[rec] = by_rec.get(rec, 0) + 1
            plug_dropped = result.get("plug_risk_dropped") or []
            unfinished = bool(result.get("error") or result.get("uk_ran_out"))
            row.update(
                plug_risk_dropped=len(plug_dropped),
                plug_risk_examples=[title for _asin, title in plug_dropped[:12]],
                asins_scanned=result.get("asins_scanned", 0), saved=len(opportunities),
                by_recommendation=by_rec, error=result.get("error"),
                good=[dict(asin=o["product"]["asin"], title=(o["product"].get("title") or "")[:60],
                           recommendation=o["report"]["recommendation"],
                           roi=round(o["product"].get("roi") or 0, 1),
                           profit=round(o["product"].get("profit") or 0, 2),
                           source=o["product"].get("best_source_marketplace"),
                           uk_price=o["product"].get("buy_box_now"))
                      for o in opportunities if o["report"]["recommendation"] in GOOD],
            )

        if unfinished:
            kept_set = set(kept)
            examined = [a for a in examined if a not in kept_set]
        row["examined_asins"] = never_scan + examined
        row["tokens_used_approx"] = (tokens_before or 0) - (finder.api.tokens_left or 0)
        return row

    @staticmethod
    def run(categories: list = None, marketplace: str = "DE", per_category: int = 60,
            use_ratio_precheck: bool = True) -> dict:
        keys = list(categories) if categories else list(EU_DROP_CATEGORIES)
        unknown = [k for k in keys if k not in EU_DROP_CATEGORIES]
        if unknown:
            raise ValueError(f"Unknown category key(s): {unknown} -- choose from {list(EU_DROP_CATEGORIES)}")

        finder = ProductFinder()
        sp_client = get_sp_api_client() if use_ratio_precheck else None
        excluded = ProductRepository.get_excluded_asins()
        report = dict(marketplace=marketplace, per_category=per_category,
                      ratio_precheck=bool(sp_client), tokens_start=finder.api.tokens_left, categories=[])

        for key in keys:
            cfg = EU_DROP_CATEGORIES[key]
            category_id = cfg["ids"].get(marketplace)
            if not category_id:
                report["categories"].append(dict(
                    key=key, label=cfg["label"], error=f"No {marketplace} category ID mapped for {cfg['label']}"))
                continue
            report["categories"].append(EuDropScanService.run_category(
                marketplace, category_id, cfg["label"], key, per_category=per_category,
                min_price=cfg.get("min_price", 20), use_ratio_precheck=use_ratio_precheck,
                finder=finder, sp_client=sp_client, excluded=excluded,
            ))

        report["tokens_end"] = finder.api.tokens_left
        return report
