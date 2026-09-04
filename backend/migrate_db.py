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
    "match_tier": "TEXT DEFAULT ''",
    "match_confidence_pct": "INTEGER DEFAULT 0",
    "source_confidence": "TEXT DEFAULT ''",
    "review_reason_category": "TEXT",
    "last_offer_checked_at": "DATETIME",
    "last_offer_price_gbp": "REAL",
    "last_offer_buyable": "BOOLEAN",
})

migrate_table("leads", {
    "decision_reason": "TEXT",
    "source_detail": "TEXT",
    "synced_to_sheet_at": "DATETIME",
    "source_marketplace": "TEXT",
    "decision_reason_category": "TEXT",
})

migrate_table("watched_products", {
    "viable_days_90d": "INTEGER DEFAULT 0",
})

migrate_table("automation_settings", {
    "star_buys_reset_at": "DATETIME",
    "last_scan_queue_item_id": "INTEGER",
})

migrate_table("seller_new_listings", {
    "review": "TEXT",
    "sourcing_reclassified_at": "DATETIME",
    "review_reason": "TEXT",
    "review_reason_category": "TEXT",
})

migrate_table("oa_source_candidates", {
    "target_price_today_gbp": "REAL DEFAULT 0.0",
    "buy_box_avg_30d_gbp": "REAL DEFAULT 0.0",
    "target_price_30d_avg_gbp": "REAL DEFAULT 0.0",
    "price_source": "TEXT DEFAULT ''",
    "shopping_candidates_json": "TEXT DEFAULT ''",
    "added_to_review_queue": "BOOLEAN DEFAULT 0",
    "shopping_provider": "TEXT DEFAULT ''",
})

migrate_table("out_of_stock_listings", {
    "sold_evidence": "TEXT DEFAULT ''",
    "sku_listing_date": "TEXT DEFAULT ''",
    "snoozed_until": "DATETIME",
})

migrate_table("oa_source_runs", {
    "serpapi_search_count": "INTEGER DEFAULT 0",
    "serpapi_searches_left": "INTEGER",
    "asins_auto_priced": "INTEGER DEFAULT 0",
    "serpapi_quota_stopped": "BOOLEAN DEFAULT 0",
    "is_test": "BOOLEAN DEFAULT 0",
    "serper_search_count": "INTEGER DEFAULT 0",
})

def create_index_if_missing(index_name: str, table_name: str, column: str):
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    if cur.fetchone():
        print(f"Index already present, skipping: {index_name}")
        return
    print(f"Creating index: {index_name} ON {table_name}({column})")
    cur.execute(f"CREATE INDEX {index_name} ON {table_name} ({column})")


# 2026-09-04 perf fix -- ProductRepository.list_latest() ORDER BYs the
# whole table on scanned_at on every call; without this index SQLite
# has to load and sort every row (17,000+ and growing) in memory each
# time. See that method's own docstring for the caching fix alongside
# this -- both were needed, the cache alone still pays this cost once
# per TTL window.
create_index_if_missing("ix_product_records_scanned_at", "product_records", "scanned_at")

conn.commit()
conn.close()

print("\nMigration complete.")
