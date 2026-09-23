"""
Keepa watch lists (2026-09-20, Tamara) -- ASIN lists to import into Keepa's own price tracking, so Keepa
watches for a product to become a lead instead of Atlas re-scanning it every ~9 days.

Why two lists per product: a product is a lead when ROI >= TARGET_ROI_PCT, and ROI depends on BOTH the UK
sale price and the EU cost. A single "EU price falls X%" alert only covers one side -- if the UK price rises,
a smaller EU fall is enough, and that alert never fires. Profit is a straight line in both prices:

    profit = UK/(1+vat) - fees(UK) - prep - EU/(1+eu_vat)        ROI >= T  <=>  profit >= T * EU

so with s_eu = the EU fall alone that would make it a lead, and s_uk = the UK rise alone that would, ANY mix of
a UK rise u and an EU fall d makes it a lead exactly when  u/s_uk + d/s_eu >= 1.  Alerting at HALF of each
(u >= s_uk/2  OR  d >= s_eu/2) therefore catches every possible combination: if both terms were under a half
they could not sum to 1. The price is some early alerts, which a re-price settles. (Exact while the referral
fee is a flat percentage; Amazon's tiered/minimum referral fees bend the line slightly.)

Keepa's bulk tracking takes ONE percentage per imported list, so products are grouped into a few buckets by
their half-threshold, rounded DOWN (a lower alert threshold can only fire earlier, never later).

Read-only: uses stored ProductRecords, the Watchlist and the cached listing restrictions -- no Keepa or SP-API calls.
"""
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased

from app.config.fees import DIGITAL_SERVICES_FEE_RATE, MINIMUM_REFERRAL_FEE_GBP, PREP_FEE_GBP, UK_VAT_STANDARD_RATE
from app.database.database import SessionLocal
from app.database.models import ListingRestriction, ProductRecord, WatchedProduct
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.services.product_repository import ProductRepository
from app.services.sourcing_classifier import title_suggests_mains_plug

# The "is a lead" bar -- the same 25% ROI used everywhere else in Atlas.
TARGET_ROI_PCT = FeeEngine.OA_TARGET_ROI_PCT
# Keepa alert percentages the lists come in. Kept few so importing is a handful of uploads.
ALERT_BUCKETS = (3, 5, 10, 15, 20)
# A side needing a bigger move than this alone isn't worth an alert (a 40%+ swing isn't a "dip").
MAX_NEEDED_MOVE_PCT = 40.0
# A product needing no more than this on either side is effectively a lead already (24.5% against a 25% bar). An
# alert that small would be noise and the smallest list (3%) would fire long after it crossed, so it is left off the
# lists and reported to be looked at now.
NEARLY_A_LEAD_PCT = 2.0
# Prices on a record older than this are too stale to set thresholds from.
MAX_RECORD_AGE_DAYS = 60
# If the best fee model still differs from Atlas's own stored ROI by more than this many points, the record's fee
# data is incomplete (typically a blank category AND no stored referral rate), so any threshold set from it would be
# a guess -- the product is left out rather than given an unreliable number.
ROI_MISMATCH_POINTS = 3.0

_PLUG_EXCLUDED_RECOMMENDATIONS = ("GATED", "FREQUENTLY_RETURNED")


# --- exact-math helpers ---------------------------------------------------------------------------
# Each helper takes `roi_fn(price, cost) -> ROI %` -- the fee model that reproduces THIS product's own stored ROI
# (see roi_fn_for) -- so the "needed move" maths always agrees with what Atlas showed for the product.

def eu_cost_for_roi(roi_fn, price, target=TARGET_ROI_PCT, cost_now=None):
    """
    The HIGHEST EU cost (GBP, gross) that still earns `target`% at UK `price`, found by bisection on roi_fn.
    None if even a near-zero cost can't reach it (Amazon's fees swallow the price).
    """
    lo = 0.01
    if roi_fn(price, lo) < target:
        return None
    hi = max(cost_now or price * 2, lo * 2)
    for _ in range(60):
        mid = (lo + hi) / 2
        if roi_fn(price, mid) >= target:
            lo = mid
        else:
            hi = mid
    return lo


def uk_price_for_roi(roi_fn, cost, price_now, target=TARGET_ROI_PCT, max_multiple=3.0):
    """The LOWEST UK price that earns `target`% against a fixed EU `cost`; None if not within `max_multiple` x today's."""
    lo, hi = price_now, price_now * max_multiple
    if roi_fn(hi, cost) < target:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if roi_fn(mid, cost) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def needed_moves(roi_fn, price, cost, target=TARGET_ROI_PCT) -> dict:
    """
    What it would take, ONE side at a time, for this product to become a lead:
      eu_drop_pct  -- how far the EU cost must fall (UK price unchanged)
      uk_rise_pct  -- how far the UK price must rise (EU cost unchanged)
    Either is None if unreachable; both are 0 if it already is a lead.
    """
    roi_now = roi_fn(price, cost)
    if roi_now >= target:
        return {"roi_now": roi_now, "eu_drop_pct": 0.0, "uk_rise_pct": 0.0, "eu_target_cost": cost, "uk_target_price": price}

    eu_target = eu_cost_for_roi(roi_fn, price, target, cost_now=cost)
    uk_target = uk_price_for_roi(roi_fn, cost, price, target)
    return {
        "roi_now": roi_now,
        "eu_drop_pct": None if eu_target is None else max(0.0, 100 * (1 - eu_target / cost)),
        "uk_rise_pct": None if uk_target is None else max(0.0, 100 * (uk_target / price - 1)),
        "eu_target_cost": eu_target,
        "uk_target_price": uk_target,
    }


def _stored_rate_roi(uk_vat, referral_rate, fba_fee, eu_vat, include_dsf=True):
    """ROI(price, cost) from the fee rates Atlas STORED on a record when it scanned it (flat referral rate)."""
    def roi(price, cost):
        referral = max(price * referral_rate, MINIMUM_REFERRAL_FEE_GBP)
        dsf = round(DIGITAL_SERVICES_FEE_RATE * (referral + fba_fee), 2) if include_dsf else 0.0
        profit = price / (1 + uk_vat) - fba_fee - referral - PREP_FEE_GBP - dsf - cost / (1 + eu_vat)
        return 100 * profit / cost
    return roi


def fee_models_for(record, include_dsf=True, eu_vat=None):
    """
    Every fee model a ProductRecord's own data supports (ROI% as a function of UK price and gross EU cost):
    FeeEngine when it has a category, and the referral/VAT rates stored on the record itself. Never empty --
    with neither, FeeEngine's default rules. Nothing here checks them against the record's stored ROI (see
    roi_fn_for), so a caller with no EU cost to check against uses these unvalidated.

    The current rules are the defaults: the Digital Services Fee is charged (`include_dsf`) and EU costs are netted by
    UK VAT (`eu_vat` None = the UK rate -- Tamara reclaims UK VAT only on EU FBA purchases, 2026-09-21). Passing
    include_dsf=False / eu_vat=record.eu_vat_rate_used gives the rules records were priced under before those dates.
    """
    eu_vat = UK_VAT_STANDARD_RATE if eu_vat is None else eu_vat
    fba = record.fba_fee or FeeEngine.DEFAULT_FBA_FEE
    models = []
    if record.category_name:
        models.append(lambda price, cost: FeeEngine.roi_at_price(price, cost, record.category_name, fba, eu_vat,
                                                                 include_dsf=include_dsf))
    if record.referral_rate_used:
        uk_vat = UK_VAT_STANDARD_RATE if record.uk_vat_rate_used is None else record.uk_vat_rate_used
        models.append(_stored_rate_roi(uk_vat, record.referral_rate_used, fba, eu_vat, include_dsf=include_dsf))
    if not models:
        models.append(lambda price, cost: FeeEngine.roi_at_price(price, cost, "", fba, eu_vat, include_dsf=include_dsf))
    return models


def roi_fn_for(record):
    """
    Picks, for one ProductRecord, the fee model that best reproduces the ROI Atlas stored for it at today's prices:
    FeeEngine (category rules, tiered referral fees) when the record has a category, or the fee rates stored on the
    record itself (many records have a blank category name but the real rates). Returns (roi_fn, gap_in_points).

    A record scanned BEFORE 2026-09-21 was priced without the Digital Services Fee and with the local EU VAT rate it
    stored, so each model is checked under every combination of those old and new rules and the closest counts --
    otherwise every old record of a cheap or Italian product would look as if its fee data had gone wrong. The function
    RETURNED always uses the current rules.
    """
    current = fee_models_for(record)
    stored_vat = record.eu_vat_rate_used or UK_VAT_STANDARD_RATE
    variants = [fee_models_for(record, include_dsf=dsf, eu_vat=vat)
                for dsf in (True, False) for vat in (UK_VAT_STANDARD_RATE, stored_vat)]

    price, cost, stored = record.buy_box_now, record.best_source_cost_gbp, record.roi or 0.0
    gaps = [min(abs(v[i](price, cost) - stored) for v in variants) for i in range(len(current))]
    best = min(range(len(current)), key=lambda i: gaps[i])
    return current[best], gaps[best]


def alert_bucket(needed_pct):
    """
    The Keepa alert % for a side that needs `needed_pct` alone: HALF of it (see the module docstring),
    rounded DOWN to a bucket so it can only fire early. None if there's no sensible alert (unreachable or > MAX).
    """
    if needed_pct is None or needed_pct <= 0 or needed_pct > MAX_NEEDED_MOVE_PCT:
        return None
    half = needed_pct / 2
    eligible = [b for b in ALERT_BUCKETS if b <= half]
    return max(eligible) if eligible else ALERT_BUCKETS[0]


# --- the list ---------------------------------------------------------------------------------------

class KeepaWatchListService:

    @staticmethod
    def _candidate_records(db):
        """Latest ProductRecord per ASIN that is on the Watchlist, a near miss, or a UK-recovery candidate."""
        newer = aliased(ProductRecord)
        latest_id = (
            db.query(newer.id).filter(newer.asin == ProductRecord.asin)
            .order_by(newer.scanned_at.desc(), newer.id.desc()).limit(1).correlate(ProductRecord).scalar_subquery()
        )
        watched = db.query(WatchedProduct.asin).scalar_subquery()
        viable = float(OpportunityEngine.MIN_VIABLE_ROI)
        return (
            db.query(ProductRecord)
            .filter(ProductRecord.id == latest_id)
            .filter(or_(
                ProductRecord.asin.in_(watched),
                # near miss: a live EU source, real sales, 10-25% ROI today
                and_(ProductRecord.best_source_cost_gbp > 0, ProductRecord.monthly_sales > 0,
                     ProductRecord.roi >= 10.0, ProductRecord.roi < TARGET_ROI_PCT),
                # UK recovery: poor today only because the UK fell; fine at its normal price
                and_(ProductRecord.best_source_cost_gbp > 0, ProductRecord.roi < viable,
                     ProductRecord.roi_90d >= TARGET_ROI_PCT, ProductRecord.roi_90d <= 200.0,
                     ProductRecord.buy_box_now > 0, ProductRecord.buy_box_90d > ProductRecord.buy_box_now),
            ))
            .all()
        )

    @staticmethod
    def build(now=None, max_age_days: int = MAX_RECORD_AGE_DAYS) -> dict:
        """
        {"rows": [...], "excluded": [...], "summary": {...}}. Every product Atlas is already following that has a
        live EU source and real sales but is NOT a lead today, with what it would take to become one.
        """
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        stale_before = now - timedelta(days=max_age_days)

        db = SessionLocal()
        try:
            records = KeepaWatchListService._candidate_records(db)
            watched = {a for (a,) in db.query(WatchedProduct.asin).all()}
            restricted = {
                a for (a,) in db.query(ListingRestriction.asin)
                .filter(ListingRestriction.restricted == True, ListingRestriction.marketplace == "UK").all()
            }
        finally:
            db.close()

        rows, excluded = [], []
        viable = float(OpportunityEngine.MIN_VIABLE_ROI)

        def skip(record, reason, roi_now=None):
            excluded.append({"asin": record.asin, "title": record.title or "", "reason": reason,
                             "roi_now": None if roi_now is None else round(roi_now, 1)})

        for r in records:
            scopes = []
            if r.asin in watched:
                scopes.append("Watchlist")
            if (r.best_source_cost_gbp or 0) > 0 and (r.monthly_sales or 0) > 0 and 10.0 <= (r.roi or 0) < TARGET_ROI_PCT:
                scopes.append("Near miss")
            if ((r.best_source_cost_gbp or 0) > 0 and (r.roi or 0) < viable
                    and TARGET_ROI_PCT <= (r.roi_90d or 0) <= 200.0 and (r.buy_box_now or 0) > 0
                    and (r.buy_box_90d or 0) > (r.buy_box_now or 0)):
                scopes.append("UK recovery")

            if not (r.best_source_cost_gbp or 0) > 0:
                skip(r, "No live EU source"); continue
            if not (r.buy_box_now or 0) > 0:
                skip(r, "No UK price"); continue
            if r.recommendation in _PLUG_EXCLUDED_RECOMMENDATIONS:
                skip(r, f"Can't be bought ({r.recommendation})"); continue
            if r.asin in restricted:
                skip(r, "Gated for us on Amazon"); continue
            if title_suggests_mains_plug(r.title):
                skip(r, "Plug/electrical item (never EU A2A)"); continue
            if r.scanned_at and r.scanned_at < stale_before:
                skip(r, f"Prices older than {max_age_days} days"); continue
            if not ((r.monthly_sales or 0) > 0 or (r.sales_drops_30d or 0) >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD):
                skip(r, "No sales evidence"); continue

            roi_fn, model_gap = roi_fn_for(r)
            if model_gap > ROI_MISMATCH_POINTS:
                skip(r, "Stored fee data can't reproduce its ROI (thresholds would be a guess)"); continue
            moves = needed_moves(roi_fn, r.buy_box_now, r.best_source_cost_gbp)
            if moves["roi_now"] >= TARGET_ROI_PCT:
                skip(r, "Already a lead today"); continue

            smallest_need = min(v for v in (moves["eu_drop_pct"], moves["uk_rise_pct"]) if v is not None)
            if smallest_need <= NEARLY_A_LEAD_PCT:
                skip(r, f"Within {NEARLY_A_LEAD_PCT:.0f}% of being a lead now -- review it now instead", moves["roi_now"]); continue

            eu_alert = alert_bucket(moves["eu_drop_pct"])
            uk_alert = alert_bucket(moves["uk_rise_pct"])
            if eu_alert is None and uk_alert is None:
                skip(r, f"Out of reach (needs more than a {MAX_NEEDED_MOVE_PCT:.0f}% move either way)"); continue

            rows.append({
                "asin": r.asin, "title": r.title or "", "brand": r.brand or "", "category": r.category_name or "",
                "scopes": ", ".join(scopes) or "Watchlist",
                "uk_now": r.buy_box_now, "uk_typical": r.buy_box_90d or None,
                "eu_market": r.best_source_marketplace or "", "eu_cost_gbp": r.best_source_cost_gbp,
                "roi_now": round(moves["roi_now"], 1),
                "eu_drop_needed_pct": None if moves["eu_drop_pct"] is None else round(moves["eu_drop_pct"], 1),
                "eu_target_cost_gbp": None if moves["eu_target_cost"] is None else round(moves["eu_target_cost"], 2),
                "eu_alert_pct": eu_alert,
                "uk_rise_needed_pct": None if moves["uk_rise_pct"] is None else round(moves["uk_rise_pct"], 1),
                "uk_target_price_gbp": None if moves["uk_target_price"] is None else round(moves["uk_target_price"], 2),
                "uk_alert_pct": uk_alert,
                "monthly_sales": r.monthly_sales or 0,
                "scanned": r.scanned_at.strftime("%Y-%m-%d") if r.scanned_at else "",
            })

        rows.sort(key=lambda x: (min(v for v in (x["eu_drop_needed_pct"], x["uk_rise_needed_pct"]) if v is not None), x["asin"]))
        by_reason: dict[str, int] = {}
        for e in excluded:
            by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        return {
            "rows": rows, "excluded": excluded,
            "summary": {
                "candidates": len(records), "included": len(rows), "excluded": len(excluded),
                "excluded_by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
                "eu_lists": {b: sum(1 for x in rows if x["eu_alert_pct"] == b) for b in ALERT_BUCKETS},
                "uk_lists": {b: sum(1 for x in rows if x["uk_alert_pct"] == b) for b in ALERT_BUCKETS},
                "generated": now.strftime("%Y-%m-%d %H:%M"),
            },
        }

    @staticmethod
    def write_files(result: dict, out_dir: str) -> dict:
        """
        Writes the import files and a spreadsheet with everything behind them. Returns {label: path}.
        Text files are one ASIN per line and nothing else, so they can be pasted or uploaded as they are.
        """
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        os.makedirs(out_dir, exist_ok=True)
        rows, summary = result["rows"], result["summary"]
        paths = {}

        def write_list(name, asins):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(asins) + "\n")
            paths[name] = path

        for bucket in ALERT_BUCKETS:
            eu = [x["asin"] for x in rows if x["eu_alert_pct"] == bucket]
            uk = [x["asin"] for x in rows if x["uk_alert_pct"] == bucket]
            if eu:
                write_list(f"EU_drop_{bucket:02d}pct.txt", eu)
            if uk:
                write_list(f"UK_rise_{bucket:02d}pct.txt", uk)
        if rows:
            write_list("ALL_ASINS_for_a_first_test_upload.txt", sorted({x["asin"] for x in rows}))

        wb = Workbook()
        head_fill = PatternFill("solid", fgColor="1F2937")
        head_font = Font(bold=True, color="FFFFFF")

        ws = wb.active
        ws.title = "Read me"
        lines = [
            ("Keepa watch lists", True),
            (f"Generated {summary['generated']} from Atlas's stored prices. {summary['included']} products.", False),
            ("", False),
            ("What each product needs to become a lead (ROI 25%+), one side at a time:", True),
            ("  EU drop needed = how far the EU price must fall if the UK price stays put.", False),
            ("  UK rise needed = how far the UK price must rise if the EU price stays put.", False),
            ("", False),
            ("Why there are two sets of lists:", True),
            ("  Both prices move. If the UK rises, a smaller EU fall is enough -- so watching only the EU price misses it.", False),
            ("  Any mix of a UK rise (u) and an EU fall (d) makes it a lead exactly when u/needed_uk + d/needed_eu >= 1.", False),
            ("  So alert at HALF of each side: if both moves were under half they could not add up to 1.", False),
            ("  Track a product on BOTH its EU list and its UK list and you cannot miss it (you may get some early alerts).", False),
            ("", False),
            ("Files:", True),
            ("  EU_drop_NNpct.txt  -- track these for an EU price FALL of NN% (all four EU marketplaces).", False),
            ("  UK_rise_NNpct.txt  -- track these for a UK price RISE of NN%.", False),
            ("  ALL_ASINS_for_a_first_test_upload.txt -- every ASIN once, to see whether the import works.", False),
            ("  Keepa's percentage is measured from the price at the moment you start tracking, so re-make the lists now and then.", False),
            ("", False),
            ("Counts per list", True),
        ]
        for bucket in ALERT_BUCKETS:
            lines.append((f"  EU drop {bucket}%: {summary['eu_lists'][bucket]} products      UK rise {bucket}%: {summary['uk_lists'][bucket]} products", False))
        lines += [("", False), ("Left out, and why:", True)]
        lines += [(f"  {reason}: {n}", False) for reason, n in summary["excluded_by_reason"].items()]
        for i, (text, bold) in enumerate(lines, 1):
            ws.cell(row=i, column=1, value=text).font = Font(bold=bold)
        ws.column_dimensions["A"].width = 120

        items = wb.create_sheet("Products")
        columns = [
            ("asin", "ASIN", 13), ("title", "Product", 46), ("brand", "Brand", 16), ("scopes", "Why it's here", 22),
            ("uk_now", "UK price now", 11), ("eu_market", "Cheapest EU", 9), ("eu_cost_gbp", "EU cost now (GBP)", 12),
            ("roi_now", "ROI now %", 9), ("eu_drop_needed_pct", "EU drop needed %", 12), ("eu_target_cost_gbp", "EU cost that gives 25%", 13),
            ("eu_alert_pct", "EU alert at %", 10), ("uk_rise_needed_pct", "UK rise needed %", 12),
            ("uk_target_price_gbp", "UK price that gives 25%", 13), ("uk_alert_pct", "UK alert at %", 10),
            ("uk_typical", "UK 90-day typical", 11), ("monthly_sales", "Sales / month", 9), ("scanned", "Prices from", 11),
        ]
        for c, (_, title, width) in enumerate(columns, 1):
            cell = items.cell(row=1, column=c, value=title)
            cell.font, cell.fill, cell.alignment = head_font, head_fill, Alignment(wrap_text=True, vertical="center")
            items.column_dimensions[get_column_letter(c)].width = width
        for r, row in enumerate(rows, 2):
            for c, (key, _, _) in enumerate(columns, 1):
                items.cell(row=r, column=c, value=row[key])
        items.freeze_panes = "C2"
        items.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 2)}"
        items.row_dimensions[1].height = 42

        left = wb.create_sheet("Left out")
        for c, (title, width) in enumerate((("ASIN", 13), ("Product", 60), ("Reason", 60), ("ROI now %", 10)), 1):
            cell = left.cell(row=1, column=c, value=title)
            cell.font, cell.fill = head_font, head_fill
            left.column_dimensions[get_column_letter(c)].width = width
        for r, e in enumerate(result["excluded"], 2):
            left.cell(row=r, column=1, value=e["asin"])
            left.cell(row=r, column=2, value=e["title"])
            left.cell(row=r, column=3, value=e["reason"])
            left.cell(row=r, column=4, value=e.get("roi_now"))
        left.freeze_panes = "A2"

        xlsx = os.path.join(out_dir, "keepa_watch_lists.xlsx")
        wb.save(xlsx)
        paths["keepa_watch_lists.xlsx"] = xlsx
        return paths
