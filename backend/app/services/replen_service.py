import re
from datetime import datetime, timezone

import pandas as pd
from dateutil import parser as date_parser

from app.database.database import SessionLocal
from app.database.models import ReplenItem
from app.services.brand_scan_service import BrandScanService
from app.services.product_service import ProductService
from app.services.product_repository import ProductRepository
from app.services.scan_coordinator import ScanCoordinator
from app.services.activity_log import ActivityLog

# Matches the ASIN wrapped inside an Excel HYPERLINK() formula, e.g.
# =HYPERLINK("http://Amazon.co.uk/dp/B07YKYP455","B07YKYP455") -- the
# Seller Toolkit export stores the ASIN column as this formula text,
# not a plain value.
ASIN_HYPERLINK_PATTERN = re.compile(r'"(B[A-Z0-9]{9})"')

# Only the first 16 columns of the buy sheet export are authoritative
# purchase data -- everything after that is a second, differently-
# shaped "prep centre" logistics block pasted into the same file, with
# column names (Date Ordered, ASIN, Store...) that repeat and confuse
# pandas's default header handling. Relabelling by position sidesteps
# that entirely.
BUY_SHEET_COLUMNS = [
    "date_ordered", "product_name", "asin", "cost_price", "sale_price",
    "potential_roi", "profit_per_unit", "quantity", "cost_total",
    "profit_total", "source_url", "amazon_url", "category", "brand",
    "store", "sku",
]


def _parse_date(value):
    if not value or not isinstance(value, str):
        return None

    try:
        # dayfirst=True: the sheet mixes "16 Feb 26" and "2026-03-01" --
        # dateutil handles both, dayfirst only disambiguates the
        # ambiguous all-numeric case.
        return date_parser.parse(value, dayfirst=True)
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_money(value):
    if value is None:
        return None

    try:
        return float(str(value).replace("£", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


class ReplenService:
    """
    Manages the replen watchlist -- ASINs worth checking for a repeat
    buy, either imported from a join of the purchase history buy sheet
    (where/what it cost originally) and a Seller Toolkit actual-sales
    export (the real ROI it achieved, not just what was projected at
    purchase time), or added manually.

    The achieved ROI from import is only evidence it's a genuine past
    winner -- check_now() re-prices every item through the normal scan
    pipeline to get each one's CURRENT ROI, which is what actually
    drives the buy-again decision.
    """

    @staticmethod
    def import_uploaded_actuals():
        """Idempotently add proven Amazon-sourced winners from shared STK data."""
        import csv
        import io
        from collections import defaultdict
        from app.database.models import VaSalesLine, AmazonInventoryLedgerLine
        from app.services.google_sheets_client import open_sheet
        from app.services.va_performance_service import PURCHASING_SHEET_URL
        values = open_sheet(PURCHASING_SHEET_URL).worksheet('Buy Sheet').get_all_values()
        stream = io.StringIO()
        csv.writer(stream).writerows([r[:16] + [''] * max(0, 16-len(r)) for r in values])
        stream.seek(0)
        purchases = ReplenService.parse_buy_sheet(stream)
        # Mixed OA/A2A purchase histories cannot establish A2A performance.
        mixed = {r[2].strip().upper() for r in values[1:] if len(r)>14 and 'amazon' not in r[14].lower()}
        totals = defaultdict(lambda: dict(units=0, profit=0., cog=0., missing=False))
        with SessionLocal() as db:
            returned = {r.asin.strip().upper() for r in db.query(AmazonInventoryLedgerLine).filter(
                AmazonInventoryLedgerLine.customer_returns > 0).all()}
            for row in db.query(VaSalesLine).all():
                item = totals[row.asin.strip().upper()]
                item['units'] += row.units
                item['profit'] += row.profit
                item['cog'] += row.cog or 0
                item['missing'] |= row.cog is None
            removed = []
            for entry in db.query(ReplenItem).all():
                if entry.asin in returned and (entry.notes or '').startswith('Added from uploaded STK actuals:'):
                    removed.append(entry.asin)
                    db.delete(entry)
            db.flush()
            existing = {r.asin for r in db.query(ReplenItem).all()}
            added = []
            for asin, purchase in purchases.items():
                item = totals.get(asin)
                if asin in existing or asin in mixed or asin in returned or not item or item['missing'] or item['units'] <= 0 or item['cog'] <= 0:
                    continue
                roi = 100 * item['profit'] / item['cog']
                if roi < 20:
                    continue
                db.add(ReplenItem(asin=asin, title=purchase['title'], brand=purchase['brand'],
                    category=purchase['category'], source_store=purchase['store'],
                    achieved_roi=roi, achieved_units=item['units'],
                    notes='Added from uploaded STK actuals: achieved ROI >=20%; Amazon purchase history only; no recorded ledger customer returns.'))
                added.append(asin)
            db.commit()
        return {'added': len(added), 'asins': added, 'removed_returned': removed}

    @staticmethod
    def parse_buy_sheet(file_path: str) -> dict:
        """
        Returns {asin: {store, cost, date, title, brand, category}} for
        the MOST RECENT Amazon-sourced purchase row per ASIN (any
        Amazon marketplace, including Amazon.co.uk -- a UK-sourced
        purchase can still resurface as a genuine EU A2A opportunity
        later, so it isn't excluded here). Non-Amazon retail-arbitrage
        rows (Boots, Currys, etc.) are excluded -- this app can only
        live-monitor Amazon marketplace pricing via Keepa.
        """
        df = pd.read_csv(file_path, dtype=str, usecols=range(16))
        df.columns = BUY_SHEET_COLUMNS

        df = df[df["store"].str.contains("amazon", case=False, na=False)]
        df = df[df["asin"].notna()]

        result = {}

        for _, row in df.iterrows():
            asin = row["asin"].strip()
            parsed_date = _parse_date(row["date_ordered"])
            existing = result.get(asin)

            # Keep whichever purchase is more recent -- if neither has
            # a parseable date, keep the first one seen rather than
            # endlessly overwriting.
            if existing and existing["date"] and parsed_date and parsed_date <= existing["date"]:
                continue

            result[asin] = {
                "store": (row["store"] or "").strip(),
                "cost": _parse_money(row["cost_price"]),
                "date": parsed_date,
                "title": row["product_name"] or "",
                "brand": row["brand"] or "",
                "category": row["category"] or "",
            }

        return result

    @staticmethod
    def parse_stk_sales(file_path: str) -> dict:
        """
        Returns {asin: {roi, units, title, brand, category}} from a
        Seller Toolkit "Sales Summary - By ASIN" tab-separated export
        -- RoI/Units here are real achieved figures over the export's
        date range, not projections.
        """
        df = pd.read_csv(file_path, sep="\t")
        df["asin_clean"] = df["ASIN"].astype(str).str.extract(ASIN_HYPERLINK_PATTERN)
        df = df[df["asin_clean"].notna()]

        df["RoI"] = pd.to_numeric(df["RoI"].astype(str).str.replace(",", ""), errors="coerce")
        df["Units"] = pd.to_numeric(df["Units"].astype(str).str.replace(",", ""), errors="coerce")

        result = {}

        for _, row in df.iterrows():
            result[row["asin_clean"]] = {
                "roi": row["RoI"] if pd.notna(row["RoI"]) else None,
                "units": int(row["Units"]) if pd.notna(row["Units"]) else None,
                "title": row.get("Title") or "",
                "brand": row.get("Brand") or "",
                "category": row.get("Category") or "",
            }

        return result

    @staticmethod
    def import_from_files(buy_sheet_path: str, stk_path: str, min_achieved_roi: float = 15.0) -> dict:
        """
        Joins the two exports on ASIN. Adds a ReplenItem for every
        Amazon-sourced ASIN that Seller Toolkit shows actually sold at
        least one confirmed unit at an achieved ROI of at least
        min_achieved_roi -- skips anything already on the list rather
        than overwriting (remove and re-add via the page to refresh
        one). Returns a summary for the import confirmation banner.
        """
        purchases = ReplenService.parse_buy_sheet(buy_sheet_path)
        sales = ReplenService.parse_stk_sales(stk_path)

        db = SessionLocal()
        added = 0
        skipped_existing = 0
        skipped_below_threshold = 0

        try:
            existing_asins = {row[0] for row in db.query(ReplenItem.asin).all()}

            for asin, purchase in purchases.items():
                sale = sales.get(asin)

                if not sale or not sale["units"] or sale["roi"] is None or sale["roi"] < min_achieved_roi:
                    skipped_below_threshold += 1
                    continue

                if asin in existing_asins:
                    skipped_existing += 1
                    continue

                db.add(ReplenItem(
                    asin=asin,
                    title=sale["title"] or purchase["title"],
                    brand=sale["brand"] or purchase["brand"],
                    category=sale["category"] or purchase["category"],
                    source_store=purchase["store"],
                    achieved_roi=sale["roi"],
                    achieved_units=sale["units"],
                ))
                existing_asins.add(asin)
                added += 1

            db.commit()
        finally:
            db.close()

        return {
            "added": added,
            "skipped_existing": skipped_existing,
            "skipped_below_threshold": skipped_below_threshold,
            "amazon_sourced_asins_considered": len(purchases),
        }

    @staticmethod
    def list_items():
        db = SessionLocal()

        try:
            return (
                db.query(ReplenItem)
                .order_by(ReplenItem.achieved_roi.desc().nullslast())
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def add_manual(asin: str, notes: str = ""):
        asin = asin.strip().upper()
        db = SessionLocal()

        try:
            if db.query(ReplenItem).filter(ReplenItem.asin == asin).first():
                return

            title, brand, category = "", "", ""

            try:
                products = ProductService().get_products([asin], "UK", usage_category="replen")
                if products:
                    p = products[0]
                    title = p.get("title") or ""
                    brand = p.get("brand") or ""
                    category = str(p.get("rootCategory") or "")
            except Exception as exc:
                print(f"Replen manual add: Keepa lookup failed for {asin}: {exc}")

            db.add(ReplenItem(
                asin=asin, title=title, brand=brand, category=category,
                source_store="Manual", notes=notes,
            ))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def remove(item_id: int):
        db = SessionLocal()

        try:
            item = db.get(ReplenItem, item_id)

            if item:
                db.delete(item)
                db.commit()
        finally:
            db.close()

    # How many ASINs to re-price per scan() call within check_now().
    # The old approach fetched the WHOLE replen list in one call --
    # with a large list and a limited token budget, running out
    # partway through meant literally every item came back with no EU
    # source (never got that far) and the whole check produced ZERO
    # results, even for ASINs that would have easily fit in the tokens
    # actually available. Small batches mean a partial budget still
    # gets you partial, real results instead of nothing.
    CHECK_BATCH_SIZE = 20

    @staticmethod
    def _run_check(db, items: list) -> dict:
        """
        Core batched re-price + persist loop, shared by check_now() and
        check_stale(). `items` must be ReplenItem rows already bound to
        `db` (the caller's own session) -- this neither opens/closes a
        session nor touches ScanCoordinator; both are the caller's job,
        since check_now() and check_stale() need different coordination
        (blocking "always wins" vs non-blocking "skip if busy").

        Batches least-recently-checked items first (never-checked ones
        before that), so if a run stops early on a low balance, the
        NEXT run naturally continues with whatever didn't get checked
        yet instead of re-spending tokens on ones just refreshed.

        Items that don't come back as a viable opportunity (excluded,
        dead listing, or ceiling-unprofitable) have their current
        figures cleared rather than left stale, so the page never
        shows an old number as if it were still true. Progress is
        committed after every batch, so a run that stops partway
        through keeps whatever it already found.
        """
        if not items:
            return {"checked": 0, "attempted": 0, "total": 0, "stopped_early": False, "tokens_remaining": None}

        checked = 0
        attempted = 0
        stopped_early = False
        tokens_remaining = None

        for i in range(0, len(items), ReplenService.CHECK_BATCH_SIZE):
            batch = items[i:i + ReplenService.CHECK_BATCH_SIZE]
            batch_asins = [item.asin for item in batch]

            scanner = BrandScanService(usage_category="replen")
            result = scanner.scan(
                "replen", asins=batch_asins, limit=len(batch_asins), force_rescan=True,
            )

            tokens_remaining = result.get("tokens_remaining", tokens_remaining)

            if result.get("error"):
                stopped_early = True
                break

            by_asin = {o["product"]["asin"]: o["product"] for o in result.get("opportunities", [])}

            for item in batch:
                product = by_asin.get(item.asin)
                item.last_checked_at = datetime.now(timezone.utc)
                attempted += 1

                if product:
                    item.current_roi = max(product["roi"], product["roi_90d"])
                    item.current_profit = max(product["profit"], product["profit_90d"])
                    item.current_source_marketplace = product["best_source_marketplace"]
                    checked += 1
                else:
                    item.current_roi = None
                    item.current_profit = None
                    item.current_source_marketplace = ""

            db.commit()

        return {
            "checked": checked,
            "attempted": attempted,
            "total": len(items),
            "stopped_early": stopped_early,
            "tokens_remaining": tokens_remaining,
        }

    @staticmethod
    def check_now():
        """
        Re-prices EVERY replen ASIN through the normal scan pipeline --
        see _run_check for the batching/resume behavior. Manual,
        user-initiated scan -- takes priority over the automated
        scheduler for its whole duration (see ScanCoordinator), not
        just per-batch, so it can't sneak a tick in between batches and
        start competing for tokens mid-check.
        """
        db = SessionLocal()

        try:
            all_items = (
                db.query(ReplenItem)
                .order_by(ReplenItem.last_checked_at.asc().nullsfirst())
                .all()
            )

            if not all_items:
                return {"checked": 0, "attempted": 0, "total": 0}

            ScanCoordinator.acquire_for_manual_scan()

            try:
                result = ReplenService._run_check(db, all_items)
                ActivityLog.record(
                    "replen_check",
                    f"manual: {result.get('checked', 0)}/{result.get('total', 0)} checked",
                )
                return result
            finally:
                ScanCoordinator.release_after_manual_scan()

        finally:
            db.close()

    @staticmethod
    def check_stale(since_hours: int = 24 * 7):
        """
        Weekly safety net: rescans replen items that haven't been
        scanned by ANYONE (this page, another campaign, anything) in
        the last `since_hours` -- so a background tick doesn't
        re-spend tokens on ones the user already refreshed via a
        manual "check now". Caller (see main.py's scheduler) is
        responsible for ScanCoordinator -- this doesn't acquire it
        itself, same convention as WatchlistService.check_stale.
        """
        db = SessionLocal()

        try:
            all_items = (
                db.query(ReplenItem)
                .order_by(ReplenItem.last_checked_at.asc().nullsfirst())
                .all()
            )

            if not all_items:
                return {"checked": 0, "attempted": 0, "total": 0, "stale": 0}

            recently_scanned = ProductRepository.get_recently_scanned_asins(since_hours)
            stale_items = [item for item in all_items if item.asin not in recently_scanned]

            if not stale_items:
                return {"checked": 0, "attempted": 0, "total": len(all_items), "stale": 0}

            result = ReplenService._run_check(db, stale_items)
            result["stale"] = len(stale_items)
            ActivityLog.record(
                "replen_check",
                f"automated: {result.get('checked', 0)}/{len(stale_items)} stale items checked",
            )
            return result

        finally:
            db.close()
