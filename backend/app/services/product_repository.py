from datetime import datetime, timedelta, timezone
import json

from app.database.database import SessionLocal
from app.database.models import ProductRecord, KnownProduct, WatchedProduct, ExcludedProduct


class ProductRepository:
    """
    Handles saving opportunity scan results to the database and
    reading them back. Manages its own DB session per call, since
    this is used from services (BrandScanService) rather than routes
    that have FastAPI's Depends(get_db) available.
    """

    @staticmethod
    def save_opportunity(product_dict: dict, report_dict: dict, brand_query: str):
        db = SessionLocal()

        try:
            record = ProductRecord(
                asin=product_dict.get("asin") or "",
                title=product_dict.get("title") or "",
                brand=product_dict.get("brand") or "",
                category=product_dict.get("category") or "",
                brand_query=brand_query,
                buy_box_now=product_dict.get("buy_box_now") or 0.0,
                buy_box_90d=product_dict.get("buy_box_90d") or 0.0,
                best_source_marketplace=product_dict.get("best_source_marketplace") or "",
                best_source_cost_gbp=product_dict.get("best_source_cost_gbp") or 0.0,
                fba_fee=product_dict.get("fba_fee") or 0.0,
                referral_fee=product_dict.get("referral_fee") or 0.0,
                profit=product_dict.get("profit") or 0.0,
                roi=product_dict.get("roi") or 0.0,
                profit_90d=product_dict.get("profit_90d") or 0.0,
                roi_90d=product_dict.get("roi_90d") or 0.0,
                score=report_dict.get("score") or 0,
                confidence=report_dict.get("confidence") or 0,
                recommendation=report_dict.get("recommendation") or "",
                monthly_sales=product_dict.get("monthly_sales") or 0,
                report_json=json.dumps(report_dict),
            )
            db.add(record)
            db.commit()

        except Exception as exc:
            db.rollback()
            # A failed save shouldn't break the scan itself -- just log it.
            print(f"Failed to save product record for {product_dict.get('asin')}: {exc}")

        finally:
            db.close()

    @staticmethod
    def list_latest(page: int = 1, page_size: int = 25, profitable_only: bool = None):
        """
        Returns (records_for_this_page, total_count) using the most
        recent scan record per ASIN (not every historical row).

        profitable_only: None shows everything, True shows only
        products profitable today or at their 90-day typical price,
        False shows only the ones that aren't either way.
        """
        db = SessionLocal()

        try:
            all_records = (
                db.query(ProductRecord)
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            seen = set()
            latest = []

            for record in all_records:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

            if profitable_only is True:
                latest = [r for r in latest if r.profit > 0 or r.profit_90d > 0]
            elif profitable_only is False:
                latest = [r for r in latest if r.profit <= 0 and r.profit_90d <= 0]

            total_count = len(latest)

            start = max(page - 1, 0) * page_size
            end = start + page_size

            return latest[start:end], total_count

        finally:
            db.close()

    @staticmethod
    def get_recently_scanned_asins(brand_query: str, since_hours: int) -> set:
        """
        Returns the set of ASINs already scanned for this brand within
        the last `since_hours` hours -- used to skip re-spending
        tokens on products we already have fresh data for, and lets
        repeated scans naturally reach deeper into the catalog instead
        of hitting the same top-ranked ASINs every time.
        """
        db = SessionLocal()

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

            rows = (
                db.query(ProductRecord.asin)
                .filter(ProductRecord.brand_query == brand_query)
                .filter(ProductRecord.scanned_at >= cutoff)
                .all()
            )

            return {row[0] for row in rows}

        finally:
            db.close()

    @staticmethod
    def get_summary_stats():
        """
        Counts across the latest scan record per ASIN (not raw row
        count) -- so re-scanning the same ASIN over time doesn't
        inflate the numbers.
        """
        db = SessionLocal()

        try:
            recent = (
                db.query(ProductRecord)
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            seen = set()
            latest = []

            for record in recent:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

            return {
                "total_scanned": len(latest),
                "total_profitable": sum(1 for r in latest if r.profit > 0),
                "total_buy": sum(1 for r in latest if r.recommendation == "BUY"),
                "total_review": sum(1 for r in latest if r.recommendation == "REVIEW"),
            }

        finally:
            db.close()

    @staticmethod
    def get_known_products(asins: list) -> dict:
        """
        Batch lookup against known_products (imported from a CSV
        export) -- returns {asin: KnownProduct} for whichever of the
        given ASINs have been imported. Used to check category/brand
        exclusions BEFORE spending any Keepa tokens at all.
        """
        if not asins:
            return {}

        db = SessionLocal()

        try:
            rows = (
                db.query(KnownProduct)
                .filter(KnownProduct.asin.in_(asins))
                .all()
            )

            return {row.asin: row for row in rows}

        finally:
            db.close()

    # ---- Watchlist ----

    @staticmethod
    def add_watch(asin: str, title: str = "", brand: str = "", note: str = ""):
        db = SessionLocal()

        try:
            existing = db.get(WatchedProduct, asin)

            if existing:
                if title:
                    existing.title = title
                if brand:
                    existing.brand = brand
                if note:
                    existing.note = note
            else:
                db.add(WatchedProduct(asin=asin, title=title, brand=brand, note=note))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_watch(asin: str):
        db = SessionLocal()

        try:
            existing = db.get(WatchedProduct, asin)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_watched():
        db = SessionLocal()

        try:
            return (
                db.query(WatchedProduct)
                .order_by(WatchedProduct.watched_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_watched_asins() -> set:
        db = SessionLocal()

        try:
            rows = db.query(WatchedProduct.asin).all()
            return {row[0] for row in rows}

        finally:
            db.close()

    # ---- User exclusions (separate from the static exclusions.py file --
    # this is DB-backed so it can be controlled from the page itself) ----

    @staticmethod
    def add_exclusion(asin: str, title: str = "", reason: str = ""):
        db = SessionLocal()

        try:
            existing = db.get(ExcludedProduct, asin)

            if existing:
                if title:
                    existing.title = title
                if reason:
                    existing.reason = reason
            else:
                db.add(ExcludedProduct(asin=asin, title=title, reason=reason))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_exclusion(asin: str):
        db = SessionLocal()

        try:
            existing = db.get(ExcludedProduct, asin)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_exclusions():
        db = SessionLocal()

        try:
            return (
                db.query(ExcludedProduct)
                .order_by(ExcludedProduct.excluded_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_excluded_asins() -> set:
        """
        Used as a zero-token pre-check before spending any tokens on a
        scan, same idea as the known_products category check.
        """
        db = SessionLocal()

        try:
            rows = db.query(ExcludedProduct.asin).all()
            return {row[0] for row in rows}

        finally:
            db.close()