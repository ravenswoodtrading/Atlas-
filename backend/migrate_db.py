"""
One-off migration: adds columns to tables that were added to their
models after the table already existed. Base.metadata.create_all()
only creates brand-new tables -- it never alters an existing one, so
these columns never actually landed in the real database file.

Safe to run more than once -- skips any column that's already there.
Existing data is untouched.
"""

import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "atlas.db"
print(f"Using database: {db_path}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()


def migrate_table(table_name: str, columns_to_add: dict):
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cur.fetchall()}
    print(f"\n{table_name} -- existing columns: {sorted(existing_columns)}")

    for column, column_type in columns_to_add.items():
        if column in existing_columns:
            print(f"Already present, skipping: {column}")
            continue

        print(f"Adding column: {column}")
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {column_type}")


migrate_table("product_records", {
    "profit_90d": "REAL DEFAULT 0.0",
    "roi_90d": "REAL DEFAULT 0.0",
    "report_json": "TEXT DEFAULT ''",
    "monthly_sales": "INTEGER DEFAULT 0",
    "monthly_sales_as_of": "DATETIME",
    "sales_drops_30d": "INTEGER DEFAULT 0",
    "review": "TEXT",
    "category_name": "TEXT DEFAULT ''",
    "referral_rate_used": "REAL DEFAULT 0.0",
    "uk_vat_rate_used": "REAL DEFAULT 0.0",
    "eu_vat_rate_used": "REAL DEFAULT 0.0",
    "ean": "TEXT DEFAULT ''",
    "review_reason": "TEXT",
})

migrate_table("leads", {
    "decision_reason": "TEXT",
    "source_detail": "TEXT",
})

migrate_table("automation_settings", {
    "star_buys_reset_at": "DATETIME",
    "last_scan_queue_item_id": "INTEGER",
})

migrate_table("seller_new_listings", {
    "review": "TEXT",
    "sourcing_reclassified_at": "DATETIME",
    "review_reason": "TEXT",
})

migrate_table("oa_source_candidates", {
    "target_price_today_gbp": "REAL DEFAULT 0.0",
    "buy_box_avg_30d_gbp": "REAL DEFAULT 0.0",
    "target_price_30d_avg_gbp": "REAL DEFAULT 0.0",
    "price_source": "TEXT DEFAULT ''",
    "shopping_candidates_json": "TEXT DEFAULT ''",
    "added_to_review_queue": "BOOLEAN DEFAULT 0",
})

migrate_table("oa_source_runs", {
    "serpapi_search_count": "INTEGER DEFAULT 0",
    "serpapi_searches_left": "INTEGER",
    "asins_auto_priced": "INTEGER DEFAULT 0",
    "serpapi_quota_stopped": "BOOLEAN DEFAULT 0",
})

conn.commit()
conn.close()

print("\nMigration complete.")
