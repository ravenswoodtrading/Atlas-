import sqlite3

db = sqlite3.connect("atlas.db")

rows = db.execute("""
    SELECT
        review,
        review_reason,
        recommendation,
        COUNT(*) AS count
    FROM product_records
    WHERE review IS NOT NULL
    GROUP BY review, review_reason, recommendation
    ORDER BY count DESC
""").fetchall()

for row in rows:
    print(row)

db.close()