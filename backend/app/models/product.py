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

    # Marketplace buy prices
    uk_cost: float = 0.0
    fr_cost: float = 0.0
    de_cost: float = 0.0
    it_cost: float = 0.0
    es_cost: float = 0.0

    # Amazon fees
    fba_fee: float = 0.0
    referral_fee: float = 0.0

    # Calculated values
    profit: float = 0.0
    roi: float = 0.0

    hazmat: bool = False
    adult: bool = False