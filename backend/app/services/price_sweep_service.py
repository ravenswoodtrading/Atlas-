"""
Free price sweep (2026-09-21, Tamara) -- watches the products Atlas follows for "is this a lead at TODAY'S
prices?" without spending Keepa tokens.

For each ASIN it asks SP-API's getItemOffers (free, ~2s a call under Amazon's throttling) for the live UK Buy Box and
the DE/FR/ES/IT Buy Boxes, takes the cheapest EU price in GBP, and runs it through THAT product's own fee model
(the one that reproduces the ROI Atlas stored for it -- see keepa_watch_list_service.roi_fn_for). Unlike a Keepa
price alert this covers both directions at once: a UK rise, an EU fall, or any mix, all show up as one number.

Who is swept: the Watchlist, near misses and UK-recovery candidates (the same population as the Keepa watch
lists, minus gated/plug/no-sales products) plus every active Replen item. Rolling: each batch takes the ASINs
checked longest ago, so the whole population is covered about twice a day.

MONITORING ONLY. Results go to price_sweep_results and the /price-sweep page, next to what Keepa's last full scan
said (ref_roi) so the accuracy can be measured. Nothing here alerts, re-scans or changes a verdict. Known limits
to weigh against that accuracy:
  * getItemOffers gives a Buy Box PRICE, not who fulfils it -- an EU Buy Box won by a merchant-fulfilled seller is
    not a real A2A source, so a "lead" here is unconfirmed until a full scan agrees.
  * ~12-25% of lookups return no price (the answer is then "unknown", never "no source").
  * No history: it can't tell a temporary UK spike from the normal price.
A call that FAILED (None) never overwrites an earlier real answer, and is never read as "no offer".
"""
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import aliased

from app.database.database import SessionLocal
from app.database.models import ListingRestriction, PriceSweepResult, ProductRecord, ReplenA2AItem
from app.services.currency_service import CurrencyService
from app.services.keepa_watch_list_service import (
    ROI_MISMATCH_POINTS, TARGET_ROI_PCT, KeepaWatchListService, fee_models_for, roi_fn_for,
)
from app.services.opportunity_engine import OpportunityEngine
from app.services.product_mapper import MARKETPLACE_CURRENCY
from app.services.product_repository import ProductRepository
from app.services.sourcing_classifier import title_suggests_mains_plug
from app.sp_api.client import get_sp_api_client

EU_MARKETS = ("DE", "FR", "ES", "IT")
CLOSE_ROI_PCT = float(OpportunityEngine.MIN_VIABLE_ROI)

# One batch = this many ASINs (up to 5 calls each) or BATCH_BUDGET_SECONDS, whichever ends first. With a batch every
# 30 minutes that's ~40 ASINs an hour -- a ~450-product population is covered about every 11 hours, using roughly a
# fifth of getItemOffers' throughput so scans that share it are not starved.
BATCH_SIZE = 20
BATCH_BUDGET_SECONDS = 15 * 60

_PLUG_EXCLUDED_RECOMMENDATIONS = ("GATED", "FREQUENTLY_RETURNED")

# Keepa's last scan is only a fair yardstick for the live check if it is recent.
ACCURACY_MAX_REF_AGE_DAYS = 3
ACCURACY_CLOSE_POINTS = 5.0
# A live UK price this far from the one Keepa last saw is more often a different listing or bundle (or a spike) than a
# real move -- the page flags it so a "lead" built on it isn't taken at face value.
UK_JUMP_WARN_PCT = 50.0

STATUS_LEAD, STATUS_CLOSE, STATUS_BELOW = "LEAD", "CLOSE", "BELOW"
STATUS_NO_UK, STATUS_NO_EU, STATUS_NO_MODEL = "NO_UK_PRICE", "NO_EU_PRICE", "NO_MODEL"
STATUS_LABELS = {
    STATUS_LEAD: "Lead at today's prices",
    STATUS_CLOSE: "Close (viable, under the lead bar)",
    STATUS_BELOW: "Below viable",
    STATUS_NO_UK: "No UK price returned",
    STATUS_NO_EU: "No EU price returned",
    STATUS_NO_MODEL: "No usable fee data",
}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- pure logic (no database, no network) --------------------------------------------------------------

def model_for(record):
    """
    The ROI(uk_price, eu_cost) function for a ProductRecord, or None when its fee data can't be trusted.
    A record with an EU cost must have a model that reproduces its own stored ROI; one without (nothing to check
    against) uses its category/stored rates as they are.
    """
    if record is None:
        return None
    price, cost = record.buy_box_now or 0, record.best_source_cost_gbp or 0
    if price > 0 and cost > 0:
        fn, gap = roi_fn_for(record)
        return fn if gap <= ROI_MISMATCH_POINTS else None
    if record.category_name or record.referral_rate_used:
        return fee_models_for(record)[0]
    return None


def _priced(offer):
    """The Buy Box price of an offer answer, or None when it has no genuinely buyable price."""
    if not offer or offer.get("status") != "Success" or not offer.get("price"):
        return None
    return offer["price"]


def assess(roi_fn, uk_offer, eu_offers, to_gbp=None) -> dict:
    """
    Turns raw getItemOffers answers into a verdict. `uk_offer` and each value of `eu_offers` is a real answer dict
    or None for a FAILED call. Returns {"failed": True} when nothing usable was learned (so the caller keeps
    whatever it knew before), else {"failed": False, "status", "uk_price", "eu_prices_gbp", "best_market",
    "best_cost_gbp", "est_roi"}. A market whose call failed is left out of the cheapest-price search, which can only
    understate the ROI, never invent a lead.
    """
    to_gbp = to_gbp or (lambda price, market: CurrencyService.to_gbp(price, MARKETPLACE_CURRENCY.get(market, "EUR")))
    if uk_offer is None:
        return {"failed": True}
    uk_price = _priced(uk_offer)
    if uk_price is None:
        return {"failed": False, "status": STATUS_NO_UK, "uk_price": None, "eu_prices_gbp": {},
                "best_market": "", "best_cost_gbp": None, "est_roi": None}

    prices = {}
    for market, offer in eu_offers.items():
        p = _priced(offer)
        if p:
            prices[market] = to_gbp(p, market)
    if not prices:
        if any(offer is None for offer in eu_offers.values()):
            return {"failed": True}
        return {"failed": False, "status": STATUS_NO_EU, "uk_price": uk_price, "eu_prices_gbp": {},
                "best_market": "", "best_cost_gbp": None, "est_roi": None}

    best_market = min(prices, key=prices.get)
    cost = prices[best_market]
    roi = roi_fn(uk_price, cost)
    status = STATUS_LEAD if roi >= TARGET_ROI_PCT else STATUS_CLOSE if roi >= CLOSE_ROI_PCT else STATUS_BELOW
    return {"failed": False, "status": status, "uk_price": uk_price, "eu_prices_gbp": prices,
            "best_market": best_market, "best_cost_gbp": cost, "est_roi": roi}


# --- the service ----------------------------------------------------------------------------------------

class PriceSweepService:

    @staticmethod
    def _latest_records(db, asins) -> dict:
        """asin -> newest ProductRecord, for the given asins."""
        newer = aliased(ProductRecord)
        latest_id = (
            db.query(newer.id).filter(newer.asin == ProductRecord.asin)
            .order_by(newer.scanned_at.desc(), newer.id.desc()).limit(1).correlate(ProductRecord).scalar_subquery()
        )
        out = {}
        asins = list(asins)
        for start in range(0, len(asins), 500):
            for r in db.query(ProductRecord).filter(ProductRecord.id == latest_id,
                                                    ProductRecord.asin.in_(asins[start:start + 500])).all():
                out[r.asin] = r
        return out

    @staticmethod
    def population(db) -> list:
        """
        [{"asin", "title", "scopes": [..], "record": ProductRecord | None}] -- everything worth sweeping. A record
        of None (a Replen item Atlas has never scanned) has no fee data, so it is reported as NO_MODEL rather than
        guessed at.
        """
        restricted = {
            a for (a,) in db.query(ListingRestriction.asin)
            .filter(ListingRestriction.restricted == True, ListingRestriction.marketplace == "UK").all()  # noqa: E712
        }
        entries: dict = {}

        for r in KeepaWatchListService._candidate_records(db):
            if r.recommendation in _PLUG_EXCLUDED_RECOMMENDATIONS or r.asin in restricted:
                continue
            if title_suggests_mains_plug(r.title):
                continue
            if not ((r.monthly_sales or 0) > 0
                    or (r.sales_drops_30d or 0) >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD):
                continue
            entries[r.asin] = {"asin": r.asin, "title": r.title or "", "scopes": ["Watch"], "record": r}

        replen = db.query(ReplenA2AItem).filter(ReplenA2AItem.ignored == False, ReplenA2AItem.gated == False).all()  # noqa: E712
        replen = [i for i in replen if i.asin not in restricted]
        records = PriceSweepService._latest_records(db, [i.asin for i in replen])
        for i in replen:
            e = entries.setdefault(i.asin, {"asin": i.asin, "title": i.title or "", "scopes": [],
                                            "record": records.get(i.asin)})
            e["scopes"].append("Replen")
            e["title"] = e["title"] or i.title or ""
        return list(entries.values())

    @staticmethod
    def _fetch(client, asin, deadline):
        """(uk_offer, {market: offer}) -- UK first; no EU calls at all when the UK has no price to compare against."""
        uk = client.get_item_offers(asin, "UK")
        if _priced(uk) is None:
            return uk, {}
        eu = {}
        for market in EU_MARKETS:
            if deadline is not None and time.monotonic() >= deadline:
                eu[market] = None
                continue
            eu[market] = client.get_item_offers(asin, market)
        return uk, eu

    @staticmethod
    def _save(db, entry, outcome, now):
        """Upserts the row for one ASIN. `outcome` is assess()'s dict (never a failed one)."""
        record = entry["record"]
        row = db.get(PriceSweepResult, entry["asin"])
        if row is None:
            row = PriceSweepResult(asin=entry["asin"])
            db.add(row)
        was_lead = row.status == STATUS_LEAD and row.hit_since is not None

        row.title = (entry["title"] or "")[:300]
        row.scopes = ", ".join(entry["scopes"])
        row.checked_at = now
        row.status = outcome["status"]
        row.uk_price = outcome.get("uk_price")
        row.eu_prices_gbp = json.dumps({m: round(v, 2) for m, v in (outcome.get("eu_prices_gbp") or {}).items()})
        row.best_market = outcome.get("best_market") or ""
        row.best_cost_gbp = outcome.get("best_cost_gbp")
        row.est_roi = outcome.get("est_roi")
        has_eu_ref = bool(record is not None and (record.best_source_cost_gbp or 0) > 0)
        row.ref_roi = record.roi if has_eu_ref else None
        row.ref_cost_gbp = record.best_source_cost_gbp if has_eu_ref else None
        row.ref_uk_price = record.buy_box_now if record is not None else None
        row.ref_at = record.scanned_at if record is not None else None

        news = row.status == STATUS_LEAD and (row.ref_roi is None or row.ref_roi < TARGET_ROI_PCT)
        row.hit_since = (row.hit_since if was_lead and row.hit_since else now) if news else None
        return row

    @staticmethod
    def run_batch(limit: int = BATCH_SIZE, budget_seconds: float = BATCH_BUDGET_SECONDS, client=None, now=None) -> dict:
        """
        Checks the `limit` least recently checked ASINs (never-checked first) within `budget_seconds`, prunes
        results for ASINs that dropped out of the population, and returns a summary dict for the scheduler.
        """
        client = client or get_sp_api_client()
        if client is None:
            return {"skipped": "SP-API not configured"}
        now = now or _now()
        deadline = time.monotonic() + budget_seconds

        db = SessionLocal()
        try:
            entries = PriceSweepService.population(db)
            in_pop = {e["asin"] for e in entries}
            for stale in db.query(PriceSweepResult).filter(~PriceSweepResult.asin.in_(list(in_pop) or [""])).all():
                db.delete(stale)
            db.commit()

            last = dict(db.query(PriceSweepResult.asin, PriceSweepResult.checked_at).all())
            entries.sort(key=lambda e: (last.get(e["asin"]) is not None, last.get(e["asin"]) or datetime.min))
            out = {"population": len(entries), "checked": 0, "failed": 0, "no_model": 0, "leads": 0,
                   "new_hits": 0, "out_of_time": False}
            spent = 0  # ASINs that cost SP-API calls; products with no usable fee data cost none, so don't use up slots
            for entry in entries:
                if spent >= limit:
                    break
                if time.monotonic() >= deadline:
                    out["out_of_time"] = True
                    break
                fn = model_for(entry["record"])
                if fn is None:
                    PriceSweepService._save(db, entry, {"status": STATUS_NO_MODEL}, now)
                    out["no_model"] += 1
                    continue
                spent += 1
                uk, eu = PriceSweepService._fetch(client, entry["asin"], deadline)
                outcome = assess(fn, uk, eu)
                if outcome["failed"]:
                    out["failed"] += 1
                    continue
                row = PriceSweepService._save(db, entry, outcome, now)
                out["checked"] += 1
                out["leads"] += row.status == STATUS_LEAD
                out["new_hits"] += row.hit_since is not None
            db.commit()
            return out
        finally:
            db.close()

    @staticmethod
    def summarise(result: dict) -> str:
        if result.get("skipped"):
            return f"Skipped: {result['skipped']}"
        return (f"{result['checked']} checked of {result['population']} in the sweep ({result['leads']} lead at "
                f"today's prices, {result['new_hits']} new to Keepa's last scan)"
                + (f", {result['failed']} calls failed" if result["failed"] else "")
                + (f", {result['no_model']} without usable fee data" if result["no_model"] else "")
                + (", stopped at the time budget" if result.get("out_of_time") else ""))

    @staticmethod
    def overview(now=None) -> dict:
        """Everything the /price-sweep page shows: coverage, accuracy against Keepa's last scan, hits, disagreements."""
        now = now or _now()
        db = SessionLocal()
        try:
            in_pop = {e["asin"] for e in PriceSweepService.population(db)}
            rows = [r for r in db.query(PriceSweepResult).all() if r.asin in in_pop]
            db.expunge_all()
        finally:
            db.close()

        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1

        modelled = [r for r in rows if r.status != STATUS_NO_MODEL]
        answered = [r for r in modelled if r.status in (STATUS_LEAD, STATUS_CLOSE, STATUS_BELOW)]

        def comparable(r):
            return (r.est_roi is not None and (r.ref_cost_gbp or 0) > 0 and r.ref_roi is not None and r.ref_at
                    and (r.checked_at - r.ref_at) <= timedelta(days=ACCURACY_MAX_REF_AGE_DAYS))

        cmp_rows = [r for r in rows if comparable(r)]
        deltas = [r.est_roi - r.ref_roi for r in cmp_rows]
        agree = sum(1 for r in cmp_rows if (r.est_roi >= TARGET_ROI_PCT) == (r.ref_roi >= TARGET_ROI_PCT))
        accuracy = {
            "n": len(cmp_rows),
            "mean_abs_delta": round(sum(abs(d) for d in deltas) / len(deltas), 1) if deltas else None,
            "within_close": sum(1 for d in deltas if abs(d) <= ACCURACY_CLOSE_POINTS),
            "lead_agreement": agree,
            "max_age_days": ACCURACY_MAX_REF_AGE_DAYS,
        }

        def view(r):
            uk_move = (100 * (r.uk_price / r.ref_uk_price - 1)
                       if r.uk_price and r.ref_uk_price and r.ref_uk_price > 0 else None)
            return {
                "uk_move_pct": uk_move,
                "asin": r.asin, "title": r.title, "scopes": r.scopes, "status": r.status,
                "uk_price": r.uk_price, "best_market": r.best_market, "best_cost_gbp": r.best_cost_gbp,
                "est_roi": r.est_roi, "ref_roi": r.ref_roi, "ref_cost_gbp": r.ref_cost_gbp,
                "ref_uk_price": r.ref_uk_price, "ref_at": r.ref_at, "checked_at": r.checked_at, "hit_since": r.hit_since,
                "eu_prices": json.loads(r.eu_prices_gbp) if r.eu_prices_gbp else {},
            }

        def is_jump(h):
            return h["uk_move_pct"] is not None and abs(h["uk_move_pct"]) >= UK_JUMP_WARN_PCT

        hits = [view(r) for r in sorted((r for r in rows if r.hit_since), key=lambda r: -(r.est_roi or 0))]

        return {
            "population": len(in_pop),
            "checked_ever": len(rows),
            "checked_24h": sum(1 for r in rows if r.checked_at >= now - timedelta(hours=24)),
            "never_checked": len(in_pop) - len(rows),
            "by_status": by_status,
            "coverage": {"answered": len(answered), "modelled": len(modelled)},
            "accuracy": accuracy,
            "hits": hits,
            "hits_worth_a_look": [h for h in hits if not is_jump(h)],
            "hits_probably_spikes": [h for h in hits if is_jump(h)],
            "disagreements": [view(r) for r in sorted(cmp_rows, key=lambda r: -abs(r.est_roi - r.ref_roi))[:15]],
            "status_labels": STATUS_LABELS,
            "uk_jump_warn_pct": UK_JUMP_WARN_PCT,
        }
