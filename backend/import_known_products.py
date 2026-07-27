"""
Standalone importer for a Keepa CSV export (e.g. a Product Finder
export downloaded manually) -- no server needed.

Loads static catalog metadata (title, brand, category, hazmat/adult
flags) into the known_products table. This lets category/brand
exclusion checks happen BEFORE spending any Keepa tokens, for any
ASIN that's been imported this way.

Usage:
    python import_known_products.py path/to/export.csv
"""

import sys

from app.database.base import Base
from app.database.database import engine
from app.database import models  # noqa: F401 -- registers tables with Base.metadata
from app.services.known_product_importer import KnownProductImporter

if len(sys.argv) < 2:
    print("Usage: python import_known_products.py path/to/export.csv")
    sys.exit(1)

csv_path = sys.argv[1]

Base.metadata.create_all(bind=engine)

importer = KnownProductImporter()
result = importer.import_file(csv_path)

print(f"Rows in file: {result['total_rows']}")
print(f"New products imported: {result['imported']}")
print(f"Existing products updated: {result['updated']}")
