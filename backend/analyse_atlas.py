import sqlite3

DB = "atlas.db"

db = sqlite3.connect(DB)

print("\n=== PRODUCT_RECORDS COLUMNS ===\n")

columns = db.execute("""
    PRAGMA table_info(product_records)
""").fetchall()

for col in columns:
    print(f"{col[1]} ({col[2]})")

print("\n=== REJECTED BUY RECORDS ===\n")

column_names = [col[1] for col in columns]

# Only use columns that actually exist
wanted = [
    "asin",
    "recommendation",
    "score",
    "confidence",
    "profit",
    "roi",
    "review",
    "review_reason",
    "created_at",
    "updated_at",
    "scanned_at",
    "scan_timestamp",
    "reviewed_at",
]

available = [c for c in wanted if c in column_names]

print("Using columns:")
print(", ".join(available))
print()

if "review" in column_names and "recommendation" in column_names:

    sql = f"""
        SELECT {", ".join(available)}
        FROM product_records
        WHERE review = 'down'
          AND recommendation = 'BUY'
    """

    rows = db.execute(sql).fetchall()

    print("Rejected BUY records:", len(rows))
    print()

    for row in rows:
        print(row)

else:
    print("Could not find review/recommendation columns.")

print("\n=== APPROVED BUY RECORDS ===\n")

if "review" in column_names and "recommendation" in column_names:

    sql = f"""
        SELECT {", ".join(available)}
        FROM product_records
        WHERE review = 'up'
          AND recommendation = 'BUY'
    """

    rows = db.execute(sql).fetchall()

    print("Approved BUY records:", len(rows))
    print()

    for row in rows:
        print(row)

print("\n=== DONE ===")

db.close()