from app.models.product import Product
from app.models.marketplace import Marketplace

product = Product(
    asin="B08TEST",
    title="Test Product",
    brand="Philips",
    category="Health"
)

product.marketplaces.append(
    Marketplace(
        marketplace="France",
        currency="EUR",
        price=18.40,
        buy_box=18.40,
        monthly_sold=240,
        offer_count=6
    )
)

print(product)