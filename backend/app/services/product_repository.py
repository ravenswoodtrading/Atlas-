from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord


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
                score=report_dict.get("score") or 0,
                confidence=report_dict.get("confidence") or 0,
                recommendation=report_dict.get("recommendation") or "",
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
    def list_latest(limit: int = 200):
        """
        Returns the most recently scanned record per ASIN (not every
        historical row) -- most recently scanned first.
        """
        db = SessionLocal()

        try:
            recent = (
                db.query(ProductRecord)
                .order_by(ProductRecord.scanned_at.desc())
                .limit(limit * 5)  # generous overfetch before de-duplicating
                .all()
            )

            seen = set()
            latest = []

            for record in recent:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

                if len(latest) >= limit:
                    break

            return latest

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
