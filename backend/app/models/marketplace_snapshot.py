from dataclasses import dataclass
from datetime import datetime


@dataclass
class MarketplaceSnapshot:
    """
    Represents one Amazon marketplace for a single ASIN.
    Example:
        ASIN B08XXXX
            - Germany
            - France
            - Italy
            - Spain
            - UK
    """

    asin: str
    marketplace: str

    price: float

    buy_box_price: float

    monthly_sold: int

    offer_count: int

    amazon_in_stock: bool

    last_updated: datetime

    currency: str = ""