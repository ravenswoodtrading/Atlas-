from datetime import datetime, timezone

from sqlalchemy import String, Float, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProductRecord(Base):
    """
    A saved snapshot of one opportunity from a scan -- one row per
    (ASIN, scan run). This is what lets the Products page show
    history across restarts without re-querying Keepa, and gives you
    a record of what a product looked like when you saw it, even if
    the numbers change later.

    NOTE: this only saves opportunities that made it into a scan's
    results list (i.e. had a real UK price and a real EU source) --
    excluded/filtered-out products are not persisted.
    """
    __tablename__ = "product_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    asin: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")

    # What brand search produced this record, e.g. "philips"
    brand_query: Mapped[str] = mapped_column(String, default="")

    buy_box_now: Mapped[float] = mapped_column(Float, default=0.0)
    buy_box_90d: Mapped[float] = mapped_column(Float, default=0.0)

    best_source_marketplace: Mapped[str] = mapped_column(String, default="")
    best_source_cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)

    fba_fee: Mapped[float] = mapped_column(Float, default=0.0)
    referral_fee: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)

    # Same as profit/roi but using the 90-day typical UK price instead
    # of today's -- catches opportunities where today's price is a
    # temporary discount. See ScoringEngine/FeeEngine for how these
    # are used together with profit/roi.
    profit_90d: Mapped[float] = mapped_column(Float, default=0.0)
    roi_90d: Mapped[float] = mapped_column(Float, default=0.0)

    score: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    recommendation: Mapped[str] = mapped_column(String, default="")

    # Full report (trend + score_breakdown + confidence_breakdown) as
    # JSON text -- lets the Products page show exactly why a score was
    # given, as it was AT SCAN TIME, without needing to re-run scoring
    # logic later (which could drift if the scoring rules change).
    report_json: Mapped[str] = mapped_column(String, default="")

    # Keepa's confirmed monthly sales count (real Amazon sales data,
    # not an estimate). 0 means Keepa has no confirmed figure, not
    # necessarily that the product doesn't sell.
    monthly_sales: Mapped[int] = mapped_column(Integer, default=0)

    scanned_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class KnownProduct(Base):
    """
    Static catalog metadata imported from a Keepa CSV export (e.g. a
    Product Finder export downloaded manually) -- ASIN, title, brand,
    category, hazmat/adult flags. This does NOT include price/rank
    data, which changes constantly and would go stale; only fields
    that stay true regardless of when you look.

    The point: this lets category/brand exclusion checks happen
    BEFORE spending a single Keepa token, for any ASIN that's been
    imported this way -- not just after the UK lookup like before.
    """
    __tablename__ = "known_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    manufacturer: Mapped[str] = mapped_column(String, default="")

    category_root: Mapped[str] = mapped_column(String, default="")
    category_sub: Mapped[str] = mapped_column(String, default="")
    category_tree: Mapped[str] = mapped_column(String, default="")

    model: Mapped[str] = mapped_column(String, default="")
    ean: Mapped[str] = mapped_column(String, default="")
    upc: Mapped[str] = mapped_column(String, default="")

    is_hazmat: Mapped[bool] = mapped_column(default=False)
    adult_product: Mapped[bool] = mapped_column(default=False)

    imported_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class WatchedProduct(Base):
    """
    A product the user has explicitly marked as interesting, to track
    over time. Independent of any particular scan/brand -- watched
    products can be re-checked anytime via the Watchlist page, which
    reuses the same ASIN-scan pipeline as file uploads.
    """
    __tablename__ = "watched_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(String, default="")

    watched_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ExcludedProduct(Base):
    """
    A specific ASIN the user has explicitly marked as not interesting,
    checked BEFORE spending any tokens on future scans -- same idea as
    the static exclusions.py file, but user-controlled from the page
    itself rather than requiring a code edit.
    """
    __tablename__ = "excluded_products"

    asin: Mapped[str] = mapped_column(String, primary_key=True)

    title: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(String, default="")

    excluded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )