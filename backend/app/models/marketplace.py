from dataclasses import dataclass


@dataclass
class Marketplace:

    marketplace: str

    currency: str

    price: float

    buy_box: float

    monthly_sold: int

    offer_count: int

    amazon_in_stock: bool = False