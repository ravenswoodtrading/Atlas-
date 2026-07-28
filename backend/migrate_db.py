"""
One-off migration: adds columns to product_records that were added to
the model after the table already existed. Base.metadata.create_all()
only creates brand-new tables -- it never alters an existing one, so
these columns never actually landed in the real database file.

Safe to run more than once -- skips any column that's already there.
Existing scan history is untouched.
"""

import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "atlas.db"
print(f"Using database: {db_path}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("PRAGMA table_info(product_records)")
existing_columns = {row[1] for row in cur.fetchall()}
print(f"Existing columns: {sorted(existing_columns)}")

columns_to_add = {
    "profit_90d": "REAL DEFAULT 0.0",
    "roi_90d": "REAL DEFAULT 0.0",
    "report_json": "TEXT DEFAULT ''",
    "monthly_sales": "INTEGER DEFAULT 0",
}

for column, column_type in columns_to_add.items():
    if column in existing_columns:
        print(f"Already present, skipping: {column}")
        continue

    print(f"Adding column: {column}")
    cur.execute(f"ALTER TABLE product_records ADD COLUMN {column} {column_type}")

conn.commit()
conn.close()

print("\nMigration complete.")