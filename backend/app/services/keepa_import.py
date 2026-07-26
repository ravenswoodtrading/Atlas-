from pathlib import Path
from typing import List

import pandas as pd

from app.models.product import Product


class KeepaImporter:
    """
    Imports a Keepa CSV export and converts it into Atlas Product objects.
    """

    REQUIRED_COLUMNS = [
        "ASIN",
        "Title",
        "Brand",
        "Category",
    ]

    def import_file(self, filename: str | Path) -> List[Product]:

        df = pd.read_csv(filename)

        self._validate_columns(df)

        products: List[Product] = []

        for _, row in df.iterrows():

            products.append(
                Product(
                    asin=self._text(row.get("ASIN")),
                    title=self._text(row.get("Title")),
                    brand=self._text(row.get("Brand")),
                    category=self._text(row.get("Category")),

                    buy_box_now=self._money(row.get("Buy Box: Current")),
                    buy_box_90d=self._money(row.get("Buy Box: 90 days avg.")),

                    offers_now=self._integer(row.get("Total Offer Count")),
                    offers_90d=self._integer(row.get("Total Offer Count: 90 days avg.")),

                    sales_rank_now=self._integer(row.get("Sales Rank: Current")),
                    sales_rank_90d=self._integer(row.get("Sales Rank: 90 days avg.")),

                    sales_drops_30d=self._integer(
                        row.get("Sales Rank: Drops last 90 days")
                    ),

                    fba_fee=self._money(row.get("FBA Pick&Pack Fee")),
                    referral_fee=self._money(
                        row.get("Referral Fee based on current Buy Box price")
                    ),

                    hazmat=False,
                    adult=False,
                )
            )

        return products

    def _validate_columns(self, df):

        missing = [
            column
            for column in self.REQUIRED_COLUMNS
            if column not in df.columns
        ]

        if missing:
            raise ValueError(
                f"Keepa export missing required columns: {', '.join(missing)}"
            )

    @staticmethod
    def _text(value):

        if pd.isna(value):
            return ""

        return str(value).strip()

    @staticmethod
    def _integer(value):

        if pd.isna(value):
            return 0

        try:
            return int(float(value))
        except Exception:
            return 0

    @staticmethod
    def _money(value):

        if pd.isna(value):
            return 0.0

        try:
            return float(value)
        except Exception:
            return 0.0