from dataclasses import dataclass


@dataclass
class Product:
    asin: str
    title: str
    brand: str
    category: str

    # UK selling data
    buy_box_now: float = 0.0
    buy_box_90d: float = 0.0

    offers_now: int = 0
    offers_90d: int = 0

    sales_rank_now: int = 0
    sales_rank_90d: int = 0

    sales_drops_30d: int = 0

    # Keepa's confirmed monthly sales count (their "monthlySold" stat,
    # based on actual Amazon sales data, not an estimate). 0 means
    # Keepa has no confirmed sales data for this product -- not
    # necessarily that it doesn't sell, just that Amazon hasn't
    # published a figure for it.
    monthly_sales: int = 0

    # Marketplace buy prices
    uk_cost: float = 0.0
    fr_cost: float = 0.0
    de_cost: float = 0.0
    it_cost: float = 0.0
    es_cost: float = 0.0

    # Amazon fees
    fba_fee: float = 0.0
    referral_fee: float = 0.0

    # Calculated values (based on today's UK price)
    profit: float = 0.0
    roi: float = 0.0

    # Same calculation but using the 90-day average UK price instead of
    # today's -- catches opportunities where today's price is
    # temporarily discounted (e.g. by Amazon itself) but the product
    # is normally profitable. Never hidden/discounted just because
    # today's number looks worse -- see is_excluded ceiling check and
    # the Discovery page's profitable filter, both of which use
    # whichever of profit/profit_90d is better.
    profit_90d: float = 0.0
    roi_90d: float = 0.0

    # Best A2A source found across DE/FR/IT/ES, cost already converted to GBP
    best_source_marketplace: str = ""
    best_source_cost_gbp: float = 0.0

    hazmat: bool = False
    adult: bool = False