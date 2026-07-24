from sqlalchemy import Column, Integer, String, Float
from backend.app.database.base import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)

    asin = Column(String, unique=True)
    title = Column(String)
    brand = Column(String)

    buy_price = Column(Float)
    sell_price = Column(Float)

    profit = Column(Float)
    roi = Column(Float)