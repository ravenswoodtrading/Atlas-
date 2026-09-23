"""
Replen (EU A2A) -- the redone Replen page's engine (2026-09-20, Tamara:
"sales history is useful for reporting but is restricting us. An item that
wasn't profitable first time round may now be a winner").

Every ASIN ever bought EU A2A (Buy Sheet Store = Amazon.de/.fr/.es/.it) is
tracked in ReplenA2AItem. The old ReplenService only listed ASINs that had
already sold through with a filled-in cost of goods, which hid exactly the
ones you most want to see: a fresh buy that is selling right now, and an old
buy that wasn't worth repeating until the price moved.

Three inputs feed one status per ASIN (see classify):
  * the Buy Sheet          -- what we paid, what we planned to sell for (free)
  * SP-API                 -- stock on hand / inbound, units shipped in 30 days (free)
  * a rolling Keepa re-price -- today's EU cost, UK Buy Box, ROI. ~25% of the list
                              per day, so every ASIN is re-priced about every 4 days.

Nothing here is gated on sales history. ROI throughout is ROI, never margin.
"""
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from dateutil import parser as date_parser

from app.database.database import SessionLocal
from app.database.models import ReplenA2AItem
from app.services.activity_log import ActivityLog
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine

# --- statuses ---------------------------------------------------------------

UNCHECKED = "UNCHECKED"
BUY_MORE = "BUY_MORE"
STOCKED = "STOCKED"
MARGINAL = "MARGINAL"
WAIT_COST = "WAIT_COST"
WAIT_PRICE = "WAIT_PRICE"
WAIT_BOTH = "WAIT_BOTH"
WAIT = "WAIT"
NO_SOURCE = "NO_SOURCE"
GATED = "GATED"
DEAD = "DEAD"

STATUS_LABELS = {
    UNCHECKED: "Not priced yet",
    BUY_MORE: "Buy more",
    STOCKED: "Well stocked",
    MARGINAL: "Thin margin",
    WAIT_COST: "Wait: cost up",
    WAIT_PRICE: "Wait: price down",
    WAIT_BOTH: "Wait: cost up + price down",
    WAIT: "Wait",
    NO_SOURCE: "No EU source",
    GATED: "Gated",
    DEAD: "Dead listing",
}

# Filter chips on the page group the detailed statuses.
STATUS_GROUPS = {
    "buy": (BUY_MORE,),
    "stocked": (STOCKED,),
    "marginal": (MARGINAL,),
    "wait": (WAIT_COST, WAIT_PRICE, WAIT_BOTH, WAIT),
    "nosource": (NO_SOURCE,),
    "blocked": (GATED, DEAD),
    "unchecked": (UNCHECKED,),
}

# Sort order on the page -- most actionable first.
STATUS_ORDER = {s: i for i, s in enumerate(
    [BUY_MORE, STOCKED, MARGINAL, WAIT_COST, WAIT_PRICE, WAIT_BOTH, WAIT, NO_SOURCE, GATED, DEAD, UNCHECKED]
)}

# Filter-bar choices (value, label) -- the values are what appears in the query string and
# what ReplenA2AService._matches understands.
STOCK_FILTERS = [("instock", "In stock"), ("out", "Out of stock"), ("inbound", "Has stock inbound"),
                 ("unknown", "Stock unknown")]
SOLD_FILTERS = [("yes", "Sold in last 30 days"), ("no", "Nothing sold in 30 days")]
BOUGHT_FILTERS = [("30", "Bought in last 30 days"), ("90", "Bought in last 90 days"),
                  ("older", "Bought over 90 days ago"), ("repeat", "Bought 2+ times")]
ROI_FILTERS = [("25", "25%+ (buy bar)"), ("17", "17%+ (viable)"), ("pos", "Above 0%"),
               ("neg", "Negative"), ("none", "No ROI (unpriced / no source)")]
TREND_FILTERS = [("costup", "EU cost up 10%+ vs paid"), ("costdown", "EU cost down 10%+ vs paid"),
                 ("pricedown", "Buy Box down 10%+ vs planned"), ("priceup", "Buy Box up 10%+ vs planned")]
CHECKED_FILTERS = [("never", "Never priced"), ("24h", "Priced in last 24h"), ("7d", "Not priced for 7+ days")]
SORT_CHOICES = [("verdict", "Verdict (default)"), ("roi", "ROI"), ("profit", "Profit per unit"),
                ("bought", "Last bought"), ("sold", "Sold in 30 days"), ("stock", "Stock"),
                ("cover", "Days of cover"), ("costdelta", "EU cost vs paid"), ("pricedelta", "Buy Box vs planned"),
                ("checked", "Time since priced"), ("title", "Product name"), ("brand", "Brand")]
PAGE_SIZES = [25, 50, 100, 0]        # 0 = all
# Which way each sort key runs when you first pick it -- chosen so the row that most needs a
# look comes first: highest ROI, most recent buy, LEAST cover left, biggest Buy Box drop, stalest
# price. "verdict" is the composite most-actionable-first order and runs "ascending".
SORT_DEFAULT_DESC = {
    "verdict": False, "roi": True, "profit": True, "bought": True, "sold": True, "stock": True,
    "cover": False, "costdelta": True, "pricedelta": False, "checked": True, "title": False, "brand": False,
}

# --- tuning -----------------------------------------------------------------

# "Worth buying" bar -- the same 25% ROI every other page in Atlas treats as a real buy.
BUY_ROI_PCT = FeeEngine.OA_TARGET_ROI_PCT
# Below this it isn't viable at all (same floor the Opportunity Engine uses).
MIN_VIABLE_ROI_PCT = float(OpportunityEngine.MIN_VIABLE_ROI)

# A 10% swing either way is noise (FX, VAT rounding, a penny of Buy Box drift).
COST_UP_TOLERANCE = 0.10
PRICE_DOWN_TOLERANCE = 0.10

# Profitable but this many days of stock at the current sales rate -> "Well stocked".
COVER_DAYS_MAX = 45

# Rolling Keepa re-price: this share of the list per day => everything every ~4 days.
DAILY_FRACTION = 0.25
# An item bought within HOT_BOUGHT_DAYS, or that shipped units in the last 30 days, is
# "hot" -- re-priced as soon as it is HOT_STALE_HOURS old rather than waiting its turn.
HOT_BOUGHT_DAYS = 30
HOT_STALE_HOURS = 48
# Window over which automated runs share ONE quota (see run_daily).
DAILY_WINDOW_HOURS = 20
# Keepa scan batch size -- small so a low-token stop still keeps partial results
# (same reasoning as the old ReplenService.CHECK_BATCH_SIZE).
CHECK_BATCH_SIZE = 20

# Stock/sales refresh is free but slow (the shipments report takes ~2 minutes) -- a retry
# of a deferred run within this window reuses it instead of paying for it again.
STOCK_FRESH_HOURS = 6

# Re-alert cap for one ASIN.
ALERT_COOLDOWN_DAYS = 7

# Keepa tokens one re-priced ASIN costs (UK + the EU markets it needs). MEASURED 2026-09-20:
# 261 tokens for 30 ASINs = 8.7 -- rounded up. Used only to size the reserve below.
REPLEN_TOKENS_PER_ASIN = 10
# Most tokens the Scan Queue is ever asked to leave untouched for Replen (about a day's batch).
REPLEN_RESERVE_CAP = 700

# Scheduler: a run is due once the last success is at least this old. Deliberately NOT
# "run at every server start" -- see project_restart_triggers_daily_recheck.
RUN_DUE_HOURS = 22

SCHEDULER_NAME = "replen_a2a"

# Buy Sheet columns (the first 16 are authoritative -- see replen_service.BUY_SHEET_COLUMNS).
COL_DATE, COL_NAME, COL_ASIN, COL_COST, COL_SALE, COL_QTY = 0, 1, 2, 3, 4, 7
COL_CATEGORY, COL_BRAND, COL_STORE, COL_SKU = 12, 13, 14, 15

_ASIN_RE = re.compile(r"^B[A-Z0-9]{9}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# --- Buy Sheet parsing --------------------------------------------------------

def is_eu_a2a_store(store) -> bool:
    """
    True for an Amazon EU marketplace Store value. Tolerant of the spellings that really
    appear in the sheet ("Amazon.de", "AMIT (Amazon.it)", "Amazon EU (Germany)",
    "Amazon.italy", "Amazon,de") -- the strict four-value match HistoricalBuyingService
    uses drops about a dozen real EU A2A rows. UK/US Amazon ("Amazon.co.uk", "Amazon.com")
    and a bare "Amazon" (no marketplace to source from) are not EU A2A.
    """
    s = str(store or "").strip().lower()
    if "amazon" not in s:
        return False
    if any(x in s for x in ("co.uk", ".uk", ".com")):
        return False
    return any(x in s for x in (".de", ",de", "germany", ".fr", "france", ".es", "spain", ".it", "italy", "amit"))


def parse_money(value):
    """First number in a cell; tolerant of the mangled "£" ("�6.33") and thousands commas."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def parse_qty(value):
    n = parse_money(value)
    return int(round(n)) if n is not None else None


def parse_date(value):
    """
    The sheet mixes "2026-08-26", "16 Feb 26" and "6/1/2026". ISO is parsed explicitly
    first -- dateutil's dayfirst=True flips month and day on some ISO strings.
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if _ISO_DATE_RE.match(s):
            return datetime.strptime(s[:10], "%Y-%m-%d")
        return date_parser.parse(s, dayfirst=True)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_buy_sheet_rows(values: list) -> tuple[dict, dict]:
    """
    values: the Buy Sheet as get_all_values() returns it (header row first).

    Returns ({asin: purchase}, stats). Only EU A2A rows are kept. Literal duplicate rows
    for one order (same date + SKU + qty + cost) are counted once -- the Buy Sheet does
    contain them. `purchase` describes the most recent buy plus lifetime totals.
    """
    stats = {"rows": 0, "eu_rows": 0, "skipped_bad_asin": 0, "skipped_bad_date": 0,
             "skipped_bad_qty": 0, "duplicate_rows": 0}
    events: dict[str, dict] = {}

    for raw in values[1:]:
        stats["rows"] += 1
        row = list(raw[:16]) + [""] * max(0, 16 - len(raw))

        if not is_eu_a2a_store(row[COL_STORE]):
            continue
        stats["eu_rows"] += 1

        asin = row[COL_ASIN].strip().upper()
        if not _ASIN_RE.match(asin):
            stats["skipped_bad_asin"] += 1
            continue

        bought = parse_date(row[COL_DATE])
        if bought is None:
            stats["skipped_bad_date"] += 1
            continue

        qty = parse_qty(row[COL_QTY])
        if qty is None or qty <= 0:
            stats["skipped_bad_qty"] += 1
            continue

        cost = parse_money(row[COL_COST])
        key = (bought.date(), row[COL_SKU].strip(), qty, cost)
        per_asin = events.setdefault(asin, {})
        if key in per_asin:
            stats["duplicate_rows"] += 1
            continue

        per_asin[key] = {
            "bought": bought, "qty": qty, "cost": cost, "sale": parse_money(row[COL_SALE]),
            "store": row[COL_STORE].strip(), "sku": row[COL_SKU].strip(),
            "title": row[COL_NAME].strip(), "brand": row[COL_BRAND].strip(),
            "category": row[COL_CATEGORY].strip(),
        }

    purchases = {}
    for asin, per_asin in events.items():
        ordered = sorted(per_asin.values(), key=lambda e: e["bought"])
        last = ordered[-1]
        purchases[asin] = {
            "first_bought_at": ordered[0]["bought"],
            "last_bought_at": last["bought"],
            "times_bought": len(ordered),
            "total_units_bought": sum(e["qty"] for e in ordered),
            "last_qty": last["qty"],
            "last_cost_gbp": last["cost"],
            "last_sale_price_gbp": last["sale"],
            "last_source_store": last["store"],
            "last_sku": last["sku"],
            "title": last["title"],
            "brand": last["brand"],
            "category": last["category"],
            "skus": sorted({e["sku"] for e in ordered if e["sku"]}),
        }

    return purchases, stats


# --- status ----------------------------------------------------------------

def _gbp(value):
    return f"£{value:.2f}"


def classify(it, now=None) -> tuple[str, str]:
    """
    (status, reason) for one item -- pure, reads attributes only, so it is tested against
    plain objects. The reason always says what the numbers are, so the page never shows a
    bare verdict.

    Order matters: a Keepa result of "dead"/"gated"/"no source" beats any ROI figure, and
    "Buy more" is only awarded when there is no reason to hold off on stock.
    """
    if it.last_checked_at is None:
        return UNCHECKED, "Not priced yet"

    if it.gated:
        return GATED, "Gated for us on Amazon now, so we can't list it"

    if it.no_keepa_data:
        return DEAD, "No Keepa record -- delisted, unknown ASIN or excluded"

    reason = it.filtered_reason or ""
    if reason == "dead_listing":
        return DEAD, "No sales and no offers on the UK listing"
    if reason == "excluded_category":
        return DEAD, "In a category we exclude"
    if reason == "unprofitable_ceiling":
        price = max(it.buy_box_now or 0, it.buy_box_90d or 0)
        return WAIT_PRICE, (
            f"UK price ({_gbp(price)}) can't cover Amazon's fees even before any source cost"
            if price else "UK price can't cover Amazon's fees even before any source cost"
        )

    cost_now = it.current_source_cost_gbp
    if not cost_now:
        return NO_SOURCE, "No EU marketplace has it at a usable price right now"

    roi = it.current_roi
    market = it.current_source_marketplace or "EU"

    stock_bits = []
    if it.stock_total is not None:
        stock_bits.append(f"{it.stock_total} in stock")
        if it.stock_inbound:
            stock_bits.append(f"{it.stock_inbound} inbound")
    if it.units_30d is not None:
        stock_bits.append(f"{it.units_30d} sold in 30d")
    stock_text = ", ".join(stock_bits) if stock_bits else "stock unknown"

    if roi is None:
        return WAIT, "ROI could not be calculated from today's prices"

    if roi >= BUY_ROI_PCT:
        if it.stock_total is not None and it.units_30d is not None and it.stock_total > 0:
            if it.units_30d == 0:
                return STOCKED, (
                    f"ROI {roi:.0f}% but {it.stock_total} already in stock and none shipped in 30 days"
                )
            cover_days = it.stock_total / (it.units_30d / 30.0)
            if cover_days > COVER_DAYS_MAX:
                return STOCKED, (
                    f"ROI {roi:.0f}% but ~{cover_days:.0f} days of stock at the current sales rate "
                    f"({it.stock_total} in stock, {it.units_30d} sold in 30d)"
                )
        return BUY_MORE, f"ROI {roi:.0f}% at {_gbp(cost_now)} ({market}); {stock_text}"

    if roi >= MIN_VIABLE_ROI_PCT:
        return MARGINAL, f"ROI {roi:.0f}% at {_gbp(cost_now)} ({market}) -- viable but under {BUY_ROI_PCT:.0f}%; {stock_text}"

    causes = []
    cost_up = bool(it.last_cost_gbp) and cost_now > it.last_cost_gbp * (1 + COST_UP_TOLERANCE)
    price_down = (
        bool(it.last_sale_price_gbp) and bool(it.buy_box_now)
        and it.buy_box_now < it.last_sale_price_gbp * (1 - PRICE_DOWN_TOLERANCE)
    )
    if cost_up:
        causes.append(f"EU cost up {_gbp(cost_now - it.last_cost_gbp)} (paid {_gbp(it.last_cost_gbp)}, now {_gbp(cost_now)} {market})")
    if price_down:
        drop = 100 * (1 - it.buy_box_now / it.last_sale_price_gbp)
        causes.append(f"Buy Box down {drop:.0f}% (planned {_gbp(it.last_sale_price_gbp)}, now {_gbp(it.buy_box_now)})")

    head = f"ROI {roi:.0f}%"
    if cost_up and price_down:
        return WAIT_BOTH, f"{head}: " + "; ".join(causes)
    if cost_up:
        return WAIT_COST, f"{head}: {causes[0]}"
    if price_down:
        return WAIT_PRICE, f"{head}: {causes[0]}"
    return WAIT, (
        f"{head}, no single cause: source {_gbp(cost_now)} ({market}) vs {_gbp(it.last_cost_gbp)} paid, "
        f"Buy Box {_gbp(it.buy_box_now) if it.buy_box_now else 'n/a'}"
        if it.last_cost_gbp else f"{head} at {_gbp(cost_now)} ({market})"
    )


# --- selection ---------------------------------------------------------------

def _naive_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def is_hot(it, now) -> bool:
    bought = _naive_utc(it.last_bought_at)
    recent_buy = bought is not None and (now - bought) <= timedelta(days=HOT_BOUGHT_DAYS)
    return recent_buy or bool(it.units_30d)


def daily_quota(eligible_count: int, fraction: float = DAILY_FRACTION) -> int:
    """How many ASINs one day's Keepa re-price covers: a quarter of the list, at least one."""
    return max(1, math.ceil(eligible_count * fraction)) if eligible_count > 0 else 0


def select_batch(items: list, now=None, fraction: float = DAILY_FRACTION) -> list:
    """
    Which items to Keepa-check today. Quota = fraction of the non-ignored list (at least
    one). Never-checked items go first; then "hot" items that are HOT_STALE_HOURS old
    (capped at half the remaining quota so hot items can't starve the rest); then whatever
    was checked longest ago -- so every ASIN is re-priced about every 1/fraction days.
    """
    now = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    eligible = [i for i in items if not i.ignored]
    if not eligible:
        return []

    quota = daily_quota(len(eligible), fraction)
    chosen, chosen_ids = [], set()

    def take(candidates, limit):
        for c in candidates:
            if len(chosen) >= limit:
                break
            if c.asin not in chosen_ids:
                chosen.append(c)
                chosen_ids.add(c.asin)

    never = sorted((i for i in eligible if i.last_checked_at is None),
                   key=lambda i: _naive_utc(i.last_bought_at) or datetime.min, reverse=True)
    take(never, quota)

    stale_cut = now - timedelta(hours=HOT_STALE_HOURS)
    hot = sorted(
        (i for i in eligible if i.last_checked_at is not None and is_hot(i, now)
         and _naive_utc(i.last_checked_at) <= stale_cut),
        key=lambda i: _naive_utc(i.last_checked_at),
    )
    take(hot, len(chosen) + max(0, (quota - len(chosen)) // 2))

    rest = sorted((i for i in eligible if i.last_checked_at is not None),
                  key=lambda i: _naive_utc(i.last_checked_at))
    take(rest, quota)
    return chosen


# --- service -----------------------------------------------------------------

class ReplenA2AService:
    _state_lock = threading.Lock()
    _running = None          # what a background run is doing right now, or None
    _last_message = ""       # outcome of the most recent background run
    _last_finished = None

    # ---- purchases ----

    @staticmethod
    def sync_purchases(values: list | None = None) -> dict:
        """
        Reads the Buy Sheet (the same tab and 16 columns the daily replen job already read
        before this redo) and upserts one row per EU A2A ASIN. Purchase columns are always
        overwritten from the sheet; nothing else on an existing row is touched. An ASIN
        that disappears from the sheet is left in place (a deleted sheet row shouldn't
        silently erase its history here).
        """
        if values is None:
            from app.services.google_sheets_client import open_sheet
            from app.services.va_performance_service import PURCHASING_SHEET_URL
            values = open_sheet(PURCHASING_SHEET_URL).worksheet("Buy Sheet").get_all_values()

        purchases, stats = parse_buy_sheet_rows(values)
        added = updated = 0

        db = SessionLocal()
        try:
            existing = {r.asin: r for r in db.query(ReplenA2AItem).all()}
            for asin, p in purchases.items():
                row = existing.get(asin)
                if row is None:
                    row = ReplenA2AItem(asin=asin)
                    db.add(row)
                    added += 1
                else:
                    updated += 1
                for field in ("first_bought_at", "last_bought_at", "times_bought", "total_units_bought",
                              "last_qty", "last_cost_gbp", "last_sale_price_gbp", "last_source_store",
                              "last_sku"):
                    setattr(row, field, p[field])
                # Sheet text only fills blanks; a Keepa title is better than a sheet one.
                for field in ("title", "brand", "category"):
                    if not getattr(row, field):
                        setattr(row, field, p[field])
            db.commit()
        finally:
            db.close()

        return {"added": added, "updated": updated, "asins": len(purchases), **stats}

    # ---- stock ----

    @staticmethod
    def refresh_stock(sp_client=None) -> dict:
        """
        Live FBA stock and 30-day units shipped, per ASIN, from SP-API (free, no Keepa).
        Stock needs every inventory page (all_pages=True -- see get_inventory_summaries) and
        an ASIN absent from a COMPLETE listing genuinely has zero stock. If either call
        fails, that half is left exactly as it was: None from the client means "call failed",
        never "no stock".
        """
        if sp_client is None:
            from app.sp_api.client import get_sp_api_client
            sp_client = get_sp_api_client()
        if not sp_client:
            return {"stock": False, "shipments": False, "error": "SP-API is not configured"}

        inventory = sp_client.get_inventory_summaries("UK", all_pages=True)
        shipments = sp_client.get_fba_fulfilled_shipments_units("UK", 30)

        result = {"stock": inventory is not None, "shipments": shipments is not None,
                  "fba_skus": len(inventory) if inventory else 0}
        if inventory is None and shipments is None:
            result["error"] = "SP-API returned nothing for stock or shipments"
            return result

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db = SessionLocal()
        try:
            items = db.query(ReplenA2AItem).all()
            tracked = {i.asin for i in items}

            sku_to_asin = {sku: info.get("asin") for sku, info in (inventory or {}).items() if info.get("asin")}
            for i in items:
                if i.last_sku:
                    sku_to_asin.setdefault(i.last_sku, i.asin)

            if inventory is not None:
                totals: dict[str, dict] = {}
                for info in inventory.values():
                    asin = info.get("asin")
                    if asin not in tracked:
                        continue
                    t = totals.setdefault(asin, {"total": 0, "fulfillable": 0, "inbound": 0})
                    t["total"] += info.get("total") or 0
                    t["fulfillable"] += info.get("fulfillable") or 0
                    t["inbound"] += ((info.get("inbound_working") or 0) + (info.get("inbound_shipped") or 0)
                                     + (info.get("inbound_receiving") or 0))
                for i in items:
                    t = totals.get(i.asin, {"total": 0, "fulfillable": 0, "inbound": 0})
                    i.stock_total, i.stock_fulfillable, i.stock_inbound = t["total"], t["fulfillable"], t["inbound"]
                    i.stock_checked_at = now

            if shipments is not None:
                units: dict[str, dict] = {}
                for sku, info in shipments.items():
                    asin = sku_to_asin.get(sku)
                    if asin not in tracked:
                        continue
                    u = units.setdefault(asin, {"units": 0, "last": ""})
                    u["units"] += info.get("units_shipped") or 0
                    u["last"] = max(u["last"], info.get("last_shipment_date") or "")
                for i in items:
                    u = units.get(i.asin, {"units": 0, "last": ""})
                    i.units_30d = u["units"]
                    i.last_sale_date = u["last"] or i.last_sale_date or ""

            db.commit()
            result["items_with_stock"] = sum(1 for i in items if (i.stock_total or 0) > 0)
        finally:
            db.close()

        return result

    @staticmethod
    def _stock_is_fresh() -> bool:
        db = SessionLocal()
        try:
            newest = max((r[0] for r in db.query(ReplenA2AItem.stock_checked_at).all() if r[0]), default=None)
        finally:
            db.close()
        if newest is None:
            return False
        return (datetime.now(timezone.utc).replace(tzinfo=None) - newest) < timedelta(hours=STOCK_FRESH_HOURS)

    # ---- Keepa ----

    @staticmethod
    def _apply_scan(item, opp, now):
        item.last_checked_at = now
        if opp is None:
            item.no_keepa_data = True
            item.filtered_reason = ""
            item.current_source_marketplace = ""
            item.current_source_cost_gbp = None
            item.current_roi = item.current_roi_90d = item.current_profit = None
            return

        product, report = opp["product"], opp["report"]
        item.no_keepa_data = False
        item.filtered_reason = report.get("filtered_reason") or ""
        cost = product.get("best_source_cost_gbp")
        item.current_source_cost_gbp = cost if cost else None
        item.current_source_marketplace = (product.get("best_source_marketplace") or "") if cost else ""
        item.buy_box_now = product.get("buy_box_now") or None
        item.buy_box_90d = product.get("buy_box_90d") or None
        item.offers_now = product.get("offers_now")
        item.monthly_sales = product.get("monthly_sales")
        item.gated = bool(product.get("gated"))
        if cost and not item.filtered_reason:
            item.current_roi = product.get("roi")
            item.current_roi_90d = product.get("roi_90d")
            item.current_profit = product.get("profit")
        else:
            item.current_roi = item.current_roi_90d = item.current_profit = None
        for field in ("title", "brand"):
            if not getattr(item, field) and product.get(field):
                setattr(item, field, product[field])

    @staticmethod
    def check_asins(asins: list) -> dict:
        """
        Keepa-prices `asins` through the normal scan pipeline (caller holds ScanCoordinator).
        include_no_eu_source=True keeps ASINs the scan would otherwise drop silently, each
        with its filtered_reason -- that is what lets the page say WHY something isn't a buy
        instead of showing a blank. An ASIN the scan never actually looked up (ran out of
        tokens part-way) is left untouched to be retried; one Keepa was asked about and
        returned nothing for is recorded as "no Keepa data".
        """
        from app.services.brand_scan_service import BrandScanService

        attempted = checked = 0
        stopped_early = False
        tokens_remaining = None

        for start in range(0, len(asins), CHECK_BATCH_SIZE):
            chunk = asins[start:start + CHECK_BATCH_SIZE]
            result = BrandScanService(usage_category="replen").scan(
                "replen-a2a", asins=chunk, limit=len(chunk), force_rescan=True, include_no_eu_source=True,
            )
            tokens_remaining = result.get("tokens_remaining", tokens_remaining)

            if result.get("error"):
                stopped_early = True
                break

            by_asin = {o["product"]["asin"]: o for o in result.get("opportunities", [])}
            looked_up = set(result.get("completed_asins") or [])
            ran_out = bool(result.get("uk_ran_out"))
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            db = SessionLocal()
            try:
                for item in db.query(ReplenA2AItem).filter(ReplenA2AItem.asin.in_(chunk)).all():
                    opp = by_asin.get(item.asin)
                    if opp is None and ran_out and item.asin not in looked_up:
                        continue  # never reached -- retry next time rather than call it dead
                    ReplenA2AService._apply_scan(item, opp, now)
                    attempted += 1
                    if opp is not None:
                        checked += 1
                db.commit()
            finally:
                db.close()

            if ran_out:
                stopped_early = True
                break

        return {"attempted": attempted, "checked": checked, "total": len(asins),
                "stopped_early": stopped_early, "tokens_remaining": tokens_remaining}

    # ---- verdicts + alerts ----

    @staticmethod
    def reclassify_all(alert: bool = True) -> dict:
        """
        Recomputes every item's status from what is stored (so a stock-only change still
        moves an item) and Discord-pings items that just flipped INTO "Buy more" from a real
        earlier status. The very first status an item ever gets never pings -- otherwise the
        initial backfill would fire one alert per profitable ASIN.
        """
        from app.services.discord_notifier import DiscordNotifier

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        counts: dict[str, int] = {}
        alerts = 0

        db = SessionLocal()
        try:
            for it in db.query(ReplenA2AItem).all():
                status, reason = classify(it, now)
                old = it.status
                if status != old:
                    it.status_changed_at = now
                it.status, it.status_reason = status, reason
                counts[status] = counts.get(status, 0) + 1

                if (alert and not it.ignored and status == BUY_MORE
                        and old not in (None, UNCHECKED, BUY_MORE)
                        and (it.alerted_at is None
                             or now - it.alerted_at > timedelta(days=ALERT_COOLDOWN_DAYS))):
                    sent = DiscordNotifier.notify_replen_buy_more({
                        "asin": it.asin, "title": it.title, "reason": reason, "roi": it.current_roi,
                        "source_marketplace": it.current_source_marketplace,
                        "source_cost": it.current_source_cost_gbp, "last_cost": it.last_cost_gbp,
                        "stock": it.stock_total if it.stock_total is not None else "?",
                        "units_30d": it.units_30d if it.units_30d is not None else "?",
                    })
                    if sent:
                        it.alerted_at = now
                        alerts += 1
            db.commit()
        finally:
            db.close()

        return {"counts": counts, "alerts": alerts}

    # ---- token priority ----

    @staticmethod
    def token_reserve_needed() -> int:
        """
        Keepa tokens the Scan Queue should leave untouched right now so today's Replen batch can
        actually run. The Scan Queue ticks every 60s and otherwise spends everything above a
        30-token reserve (under a minute of refill), so Replen -- 1x a day, at ~530 tokens for a
        full batch -- kept hitting an empty balance (2026-09-20: stopped at 30 of 60 ASINs with
        26 tokens left). This is 0 extra once the day's quota is priced, so it only holds tokens
        back while Replen still has work to do. Never raises; on any error it asks for nothing.
        """
        try:
            db = SessionLocal()
            try:
                rows = db.query(ReplenA2AItem.ignored, ReplenA2AItem.last_checked_at).all()
            finally:
                db.close()
            eligible = [checked for ignored, checked in rows if not ignored]
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=DAILY_WINDOW_HOURS)
            done = sum(1 for checked in eligible if checked and _naive_utc(checked) >= cutoff)
            remaining = max(0, daily_quota(len(eligible)) - done)
            return min(REPLEN_RESERVE_CAP, remaining * REPLEN_TOKENS_PER_ASIN)
        except Exception as exc:
            print(f"Replen token reserve lookup failed (asking for none): {exc}")
            return 0

    # ---- orchestration ----

    @staticmethod
    def run_daily(manual: bool = False, force_stock: bool = False) -> dict:
        """
        One full pass: sync purchases -> refresh stock/sales -> Keepa-check the day's batch
        -> recompute every verdict. The free steps run without the scan lock; only the Keepa
        step takes it. Automated runs take it non-blocking and report `deferred` if a scan
        is running (the scheduler simply tries again later, and the stock refresh is reused
        rather than repeated); manual runs wait their turn.
        """
        from app.services.scan_coordinator import ScanCoordinator

        out: dict = {}
        try:
            out["sync"] = ReplenA2AService.sync_purchases()
        except Exception as exc:
            print(f"Replen A2A purchase sync failed: {exc}")
            out["sync"] = {"error": str(exc)}

        if force_stock or not ReplenA2AService._stock_is_fresh():
            try:
                out["stock"] = ReplenA2AService.refresh_stock()
            except Exception as exc:
                print(f"Replen A2A stock refresh failed: {exc}")
                out["stock"] = {"error": str(exc)}
        else:
            out["stock"] = {"skipped": "refreshed recently"}

        db = SessionLocal()
        try:
            all_items = db.query(ReplenA2AItem).all()
        finally:
            db.close()
        batch = [i.asin for i in select_batch(all_items)]
        if not manual:
            # Automated runs are capped at ONE day's quota in total: whatever was priced in the
            # last DAILY_WINDOW_HOURS (an earlier attempt that failed part-way, a manual
            # re-price) counts against it. Without this a retry after a failure -- Sheets down,
            # say -- would take a fresh 25% batch every time and burn the whole list in a day.
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=DAILY_WINDOW_HOURS)
            done_today = sum(1 for i in all_items if not i.ignored and i.last_checked_at and i.last_checked_at >= cutoff)
            batch = batch[:max(0, len(batch) - done_today)]
            out["already_priced_today"] = done_today
        out["batch_size"] = len(batch)

        if batch:
            if manual:
                ScanCoordinator.acquire_for_manual_scan()
                acquired = True
            else:
                acquired = ScanCoordinator.try_acquire_for_automated_tick()
            if not acquired:
                # Verdicts still refresh from the free data just gathered. Alerts stay on:
                # they key off the status change, so suppressing them here would consume
                # the change and the next run would never announce it.
                out["deferred"] = True
                out["verdicts"] = ReplenA2AService.reclassify_all()
                return out
            try:
                out["check"] = ReplenA2AService.check_asins(batch)
            finally:
                if manual:
                    ScanCoordinator.release_after_manual_scan()
                else:
                    ScanCoordinator.release_after_automated_tick()

        out["verdicts"] = ReplenA2AService.reclassify_all()
        ActivityLog.record("replen_check", "Replen EU A2A: " + ReplenA2AService.summarise(out))
        return out

    @staticmethod
    def summarise(out: dict) -> str:
        sync, stock, check, verdicts = out.get("sync", {}), out.get("stock", {}), out.get("check"), out.get("verdicts")
        parts = [f"{sync.get('asins', '?')} EU A2A ASINs ({sync.get('added', 0)} new)"
                 if "error" not in sync else f"purchase sync FAILED ({sync['error'][:60]})"]
        if "error" in stock:
            parts.append(f"stock refresh FAILED ({stock['error'][:60]})")
        elif stock.get("skipped"):
            parts.append("stock reused")
        else:
            parts.append(f"stock {'ok' if stock.get('stock') else 'FAILED'}, "
                         f"sales {'ok' if stock.get('shipments') else 'FAILED'}")
        if out.get("deferred"):
            parts.append("Keepa check deferred (another scan running)")
        elif check:
            parts.append(f"{check['checked']}/{out.get('batch_size', 0)} re-priced"
                         + (" (stopped early: low tokens)" if check["stopped_early"] else ""))
        elif out.get("already_priced_today"):
            parts.append(f"Keepa skipped: {out['already_priced_today']} already priced today")
        if verdicts:
            parts.append(f"{verdicts['counts'].get(BUY_MORE, 0)} buy-more, {verdicts['alerts']} alerted")
        return ", ".join(parts)

    @staticmethod
    def check_one(asin: str) -> dict:
        """Manual single-ASIN re-price (Keepa) from the page's row button."""
        from app.services.scan_coordinator import ScanCoordinator
        asin = asin.strip().upper()
        ScanCoordinator.acquire_for_manual_scan()
        try:
            result = ReplenA2AService.check_asins([asin])
        finally:
            ScanCoordinator.release_after_manual_scan()
        ReplenA2AService.reclassify_all()
        return result

    # ---- background runs for the page's buttons (a stock refresh alone takes minutes) ----

    @staticmethod
    def start_background(kind: str, fn, *args, **kwargs) -> bool:
        with ReplenA2AService._state_lock:
            if ReplenA2AService._running:
                return False
            ReplenA2AService._running = kind

        def _work():
            try:
                fn(*args, **kwargs)
                message = "finished"
            except Exception as exc:
                print(f"Replen A2A background run '{kind}' failed: {exc}")
                message = f"failed: {exc}"
            with ReplenA2AService._state_lock:
                ReplenA2AService._running = None
                ReplenA2AService._last_message = f"{kind}: {message}"
                ReplenA2AService._last_finished = datetime.now(timezone.utc)

        threading.Thread(target=_work, daemon=True).start()
        return True

    @staticmethod
    def run_status() -> dict:
        with ReplenA2AService._state_lock:
            return {"running": ReplenA2AService._running, "last_message": ReplenA2AService._last_message}

    @staticmethod
    def refresh_free_data() -> dict:
        """Manual 'Refresh now': purchases + stock/sales + verdicts, no Keepa spend."""
        out = {"sync": ReplenA2AService.sync_purchases(), "stock": ReplenA2AService.refresh_stock()}
        out["verdicts"] = ReplenA2AService.reclassify_all()
        return out

    # ---- page data ----

    @staticmethod
    def set_ignored(asin: str, ignored: bool):
        db = SessionLocal()
        try:
            row = db.query(ReplenA2AItem).filter(ReplenA2AItem.asin == asin.strip().upper()).first()
            if row:
                row.ignored = ignored
                db.commit()
        finally:
            db.close()

    @staticmethod
    def _derive(i, now) -> dict:
        """One item plus every derived display/filter/sort value, computed once."""
        cover = None
        if i.stock_total and i.units_30d:
            cover = round(i.stock_total / (i.units_30d / 30.0))
        cost_delta = (i.current_source_cost_gbp - i.last_cost_gbp) if i.current_source_cost_gbp and i.last_cost_gbp else None
        return {
            "item": i,
            "label": STATUS_LABELS.get(i.status, i.status),
            "cover_days": cover,
            "cost_delta": cost_delta,
            "cost_delta_pct": round(100 * cost_delta / i.last_cost_gbp, 4) if cost_delta is not None else None,
            # rounded: 27/30 is not exactly 0.9, so an exact -10% would otherwise land either side
            # of the tolerance depending on float noise
            "price_delta_pct": round(100 * (i.buy_box_now / i.last_sale_price_gbp - 1), 4)
            if i.buy_box_now and i.last_sale_price_gbp else None,
            "days_since_bought": (now - _naive_utc(i.last_bought_at)).days if i.last_bought_at else None,
            "hours_since_check": (now - _naive_utc(i.last_checked_at)).total_seconds() / 3600
            if i.last_checked_at else None,
        }

    @staticmethod
    def _matches(r: dict, f: dict) -> bool:
        """True if derived row `r` passes every active filter in `f` (group is handled separately)."""
        i = r["item"]

        q = (f.get("query") or "").strip().lower()
        if q and not (q in (i.asin or "").lower() or q in (i.title or "").lower() or q in (i.brand or "").lower()):
            return False

        stock = f.get("stock")
        if stock == "instock" and not (i.stock_total or 0) > 0:
            return False
        if stock == "out" and i.stock_total != 0:
            return False
        if stock == "inbound" and not (i.stock_inbound or 0) > 0:
            return False
        if stock == "unknown" and i.stock_total is not None:
            return False

        sold = f.get("sold")
        if sold == "yes" and not (i.units_30d or 0) > 0:
            return False
        if sold == "no" and i.units_30d != 0:
            return False

        bought, days = f.get("bought"), r["days_since_bought"]
        if bought in ("30", "90"):
            if days is None or days > int(bought):
                return False
        if bought == "older" and (days is None or days <= 90):
            return False
        if bought == "repeat" and (i.times_bought or 0) < 2:
            return False

        market = f.get("market")
        if market == "none":
            if i.current_source_cost_gbp:
                return False
        elif market and (i.current_source_marketplace or "").upper() != market.upper():
            return False

        if f.get("brand") and (i.brand or "").strip().lower() != f["brand"].strip().lower():
            return False
        if f.get("category") and (i.category or "").strip().lower() != f["category"].strip().lower():
            return False

        roi, value = f.get("roi"), i.current_roi
        if roi == "25" and not (value is not None and value >= BUY_ROI_PCT):
            return False
        if roi == "17" and not (value is not None and value >= MIN_VIABLE_ROI_PCT):
            return False
        if roi == "pos" and not (value is not None and value > 0):
            return False
        if roi == "neg" and not (value is not None and value < 0):
            return False
        if roi == "none" and value is not None:
            return False

        trend = f.get("trend")
        if trend == "costup" and not (r["cost_delta_pct"] is not None and r["cost_delta_pct"] > 100 * COST_UP_TOLERANCE):
            return False
        if trend == "costdown" and not (r["cost_delta_pct"] is not None and r["cost_delta_pct"] < -100 * COST_UP_TOLERANCE):
            return False
        if trend == "pricedown" and not (r["price_delta_pct"] is not None and r["price_delta_pct"] < -100 * PRICE_DOWN_TOLERANCE):
            return False
        if trend == "priceup" and not (r["price_delta_pct"] is not None and r["price_delta_pct"] > 100 * PRICE_DOWN_TOLERANCE):
            return False

        checked, hours = f.get("checked"), r["hours_since_check"]
        if checked == "never" and hours is not None:
            return False
        if checked == "24h" and not (hours is not None and hours <= 24):
            return False
        if checked == "7d" and not (hours is not None and hours > 24 * 7):
            return False

        return True

    @staticmethod
    def list_rows(group: str = "", query: str = "", show_ignored: bool = False, *, stock: str = "",
                  sold: str = "", bought: str = "", market: str = "", brand: str = "", category: str = "",
                  roi: str = "", trend: str = "", checked: str = "", sort: str = "verdict",
                  order: str = "", page: int = 1, page_size: int = 0) -> dict:
        """
        Rows for the page plus everything the filter bar needs.

        Filters combine (AND). The chip counts are computed with every filter EXCEPT the status
        group applied, so a chip shows what clicking it would give you right now. `page_size` 0
        means all rows. Unknown filter/sort values are ignored rather than erroring, since they
        come straight from the query string.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db = SessionLocal()
        try:
            items = db.query(ReplenA2AItem).all()
        finally:
            db.close()

        visible = [i for i in items if show_ignored or not i.ignored]
        derived = [ReplenA2AService._derive(i, now) for i in visible]

        filters = {"query": query, "stock": stock, "sold": sold, "bought": bought, "market": market,
                   "brand": brand, "category": category, "roi": roi, "trend": trend, "checked": checked}
        matching = [r for r in derived if ReplenA2AService._matches(r, filters)]

        counts = {g: 0 for g in STATUS_GROUPS}
        for r in matching:
            for g, members in STATUS_GROUPS.items():
                if r["item"].status in members:
                    counts[g] += 1

        chosen = matching
        if group in STATUS_GROUPS:
            chosen = [r for r in chosen if r["item"].status in STATUS_GROUPS[group]]

        chosen = ReplenA2AService._sort(chosen, sort, order)

        filtered_total = len(chosen)
        pages = 1
        if page_size and page_size > 0:
            pages = max(1, math.ceil(filtered_total / page_size))
            page = min(max(1, page), pages)
            chosen = chosen[(page - 1) * page_size: page * page_size]
        else:
            page = 1

        def tally(values):
            counted: dict[str, int] = {}
            for v in values:
                v = (v or "").strip()
                if v:
                    counted[v] = counted.get(v, 0) + 1
            return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0].lower()))

        newest_stock = max((i.stock_checked_at for i in items if i.stock_checked_at), default=None)
        return {
            "rows": chosen, "counts": counts, "total": len(visible), "filtered": filtered_total,
            "matching": len(matching), "page": page, "pages": pages, "page_size": page_size,
            "options": {
                "brands": tally(i.brand for i in visible),
                "categories": tally(i.category for i in visible),
                "markets": tally(i.current_source_marketplace for i in visible),
            },
            "ignored_count": sum(1 for i in items if i.ignored), "stock_checked_at": newest_stock,
        }

    @staticmethod
    def _sort(rows: list, sort: str, order: str) -> list:
        """
        `verdict` (the default) is the most-actionable-first order the page has always had. Every
        other key sorts by one column; missing values always go LAST whichever way it runs, so
        sorting by "cover" never buries the rows that have a value under a wall of blanks.
        """
        def bought_ts(r):
            b = r["item"].last_bought_at
            return _naive_utc(b).timestamp() if b else None

        keys = {
            "roi": lambda r: r["item"].current_roi,
            "profit": lambda r: r["item"].current_profit,
            "bought": bought_ts,
            "sold": lambda r: r["item"].units_30d,
            "stock": lambda r: r["item"].stock_total,
            "cover": lambda r: r["cover_days"],
            "costdelta": lambda r: r["cost_delta_pct"],
            "pricedelta": lambda r: r["price_delta_pct"],
            "checked": lambda r: r["hours_since_check"],
            "title": lambda r: (r["item"].title or r["item"].asin or "").lower(),
            "brand": lambda r: (r["item"].brand or "").lower(),
        }

        if sort not in keys:
            rows = sorted(rows, key=lambda r: (
                STATUS_ORDER.get(r["item"].status, 99),
                -(r["item"].current_roi if r["item"].current_roi is not None else -1e9),
                -(bought_ts(r) or 0),
            ))
            return rows[::-1] if order == "desc" else rows

        value_of = keys[sort]
        desc = (order == "desc") if order in ("asc", "desc") else SORT_DEFAULT_DESC[sort]
        present = [r for r in rows if value_of(r) is not None]
        missing = [r for r in rows if value_of(r) is None]
        present.sort(key=value_of, reverse=desc)
        return present + missing
