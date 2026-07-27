from pathlib import Path

import pandas as pd

from app.database.database import SessionLocal
from app.database.models import KnownProduct


class KnownProductImporter:
    """
    Imports a Keepa Product Finder CSV export into the known_products
    table -- static catalog metadata only (title, brand, category,
    hazmat/adult flags), NOT price/rank data, which goes stale.

    This is what lets category/brand exclusion checks happen before
    spending any Keepa tokens at all, for any ASIN that's been
    imported this way.
    """

    REQUIRED_COLUMNS = ["ASIN", "Title", "Categories: Root", "Brand"]

    def import_file(self, filename: str | Path):
        df = pd.read_csv(filename)

        self._validate_columns(df)

        db = SessionLocal()
        imported = 0
        updated = 0

        try:
            for _, row in df.iterrows():
                asin = self._text(row.get("ASIN"))

                if not asin:
                    continue

                existing = db.get(KnownProduct, asin)

                fields = dict(
                    title=self._text(row.get("Title")),
                    brand=self._text(row.get("Brand")),
                    manufacturer=self._text(row.get("Manufacturer")),
                    category_root=self._text(row.get("Categories: Root")),
                    category_sub=self._text(row.get("Categories: Sub")),
                    category_tree=self._text(row.get("Categories: Tree")),
                    model=self._text(row.get("Model")),
                    ean=self._text(row.get("Product Codes: EAN")),
                    upc=self._text(row.get("Product Codes: UPC")),
                    is_hazmat=self._yes_no(row.get("Is HazMat")),
                    adult_product=self._yes_no(row.get("Adult Product")),
                )

                if existing:
                    for key, value in fields.items():
                        setattr(existing, key, value)
                    updated += 1
                else:
                    db.add(KnownProduct(asin=asin, **fields))
                    imported += 1

            db.commit()

        finally:
            db.close()

        return {"imported": imported, "updated": updated, "total_rows": len(df)}

    def _validate_columns(self, df):
        missing = [c for c in self.REQUIRED_COLUMNS if c not in df.columns]

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
    def _yes_no(value):
        if pd.isna(value):
            return False
        return str(value).strip().lower() == "yes"
