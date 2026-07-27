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

    score: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    recommendation: Mapped[str] = mapped_column(String, default="")

    scanned_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
