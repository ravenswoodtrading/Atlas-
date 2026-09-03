"""
READ-ONLY diagnostic for atlas-competitor-watch-classification-v1.md
validation. Opens atlas.db in SQLite read-only mode (mode=ro) -- a
write attempt would raise, not silently do nothing -- and never calls
.commit() or any write statement. Run with
`python diagnose_a2a_window_validation.py`.

WHY THIS DOESN'T CALL LIVE KEEPA (important): the live Keepa token
balance was checked before writing this and is currently only ~30
tokens -- nowhere near enough to re-fetch fresh UK+EU data for a
meaningful sample of the ~1045 ASINs currently tagged OA/Wholesale,
and spending what little is left here would compete with real
production scanning. So this does NOT run SourcingClassifier.classify()
against fresh data (which would be the fully faithful "what would
reclassify_all() do now" answer). Instead it approximates the same
question using ProductRecord's own historical scan rows already in
the database -- the exact same profitability numbers (already run
through FeeEngine) and the exact same thresholds SourcingClassifier
itself uses (RECENT_VIABLE_ROI_PCT, DIP_THRESHOLD), just sourced from
however many times each ASIN happened to get scanned in the last 30
days rather than a full day-by-day Keepa price reconstruction.

LIMITATION to weigh the results against: ProductRecord is scan-cadence
sparse (only has a data point on days something actually scanned that
ASIN -- a brand scan, replen check, watchlist visit, etc.), not a
value for every calendar day the way SourcingClassifier's own
KeepaParser.daily_buy_box_prices reconstruction is. An ASIN that
genuinely had a viable EU day 12 days ago but was never scanned again
until today will show NO evidence here even though the real
classifier (with a live Keepa call) might find it. So: everything
this reports IS real evidence sitting in the database already (not
guessed), but the ABSENCE of a flagged row here is not proof the real
classifier would agree -- it just means no scan happened to catch it.

Also flags the reverse (section 8 of the spec): listings CURRENTLY
tagged EU A2A / UK A2A whose stored sourcing_reasoning_json looks
marginal -- note this JSON reflects whatever the OLD 10-day-window
code last wrote (reclassify_all() has not been re-run against
production data with the new 30-day code yet, deliberately, per this
task's own instructions), so these are today's live tags, not a
prediction of what the new window would say.
"""
import json
import sqlite3
from datetime import datetime, timedelta

RECENT_VIABLE_ROI_PCT = 17.0  # SourcingClassifier.RECENT_VIABLE_ROI_PCT
DIP_THRESHOLD = 0.70          # SourcingClassifier.DIP_THRESHOLD
EU_MARKETPLACES = ("DE", "FR", "ES", "IT")
WINDOW_DAYS = 30

conn = sqlite3.connect("file:atlas.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

now = datetime.utcnow()
window_start = (now - timedelta(days=WINDOW_DAYS)).isoformat(sep=" ")

# ---- candidate listings: currently OA/unclear or Wholesale (likely) ----
candidates = cur.execute("""
    SELECT snl.id AS listing_id, snl.asin, snl.sourcing_tag, snl.detected_at,
           snl.sourcing_reasoning_json, pr.title, pr.brand
    FROM seller_new_listings snl
    LEFT JOIN product_records pr ON pr.id = snl.product_record_id
    WHERE snl.dismissed = 0
      AND snl.sourcing_tag IN ('OA / unclear', 'Wholesale (likely)')
""").fetchall()

print(f"Candidate listings (OA/unclear or Wholesale, not dismissed): {len(candidates)}")
print(f"Distinct ASINs among them: {len({c['asin'] for c in candidates})}")
print(f"Window: last {WINDOW_DAYS} days (since {window_start})\n")

flagged = []

for c in candidates:
    asin = c["asin"]

    # Best EU A2A evidence for this ASIN in the last 30 days.
    eu_row = cur.execute("""
        SELECT best_source_marketplace, best_source_cost_gbp, buy_box_now, roi, scanned_at
        FROM product_records
        WHERE asin = ? AND scanned_at >= ?
          AND best_source_marketplace IN (?, ?, ?, ?)
          AND roi >= ?
        ORDER BY roi DESC
        LIMIT 1
    """, (asin, window_start, *EU_MARKETPLACES, RECENT_VIABLE_ROI_PCT)).fetchone()

    # Best UK dip evidence for this ASIN in the last 30 days.
    uk_row = cur.execute("""
        SELECT buy_box_now, buy_box_90d, scanned_at
        FROM product_records
        WHERE asin = ? AND scanned_at >= ?
          AND buy_box_90d > 0
          AND buy_box_now <= buy_box_90d * ?
        ORDER BY (buy_box_now * 1.0 / buy_box_90d) ASC
        LIMIT 1
    """, (asin, window_start, DIP_THRESHOLD)).fetchone()

    if not eu_row and not uk_row:
        continue

    current_row = cur.execute("""
        SELECT best_source_marketplace, best_source_cost_gbp, buy_box_now, roi, scanned_at
        FROM product_records
        WHERE asin = ?
        ORDER BY scanned_at DESC
        LIMIT 1
    """, (asin,)).fetchone()

    flagged.append({
        "current_tag": c["sourcing_tag"],
        "asin": asin,
        "title": (c["title"] or "")[:60],
        "eu_row": dict(eu_row) if eu_row else None,
        "uk_row": dict(uk_row) if uk_row else None,
        "current_row": dict(current_row) if current_row else None,
    })

print(f"Flagged: currently OA/Wholesale but with real 30-day historical A2A evidence in product_records: {len(flagged)}\n")

flagged.sort(key=lambda f: (f["eu_row"] or {}).get("roi", 0) or 0, reverse=True)

print("=" * 160)
print(f"{'Current tag':<18} | {'Historical classification':<26} | {'ASIN':<12} | {'Marketplace/evidence':<22} | "
      f"{'Best hist. buy £':<16} | {'Historical date':<16} | {'Current buy £':<14} | {'Current UK £':<12}")
print("=" * 160)

for f in flagged[:40]:
    if f["eu_row"]:
        hist_class = "EU A2A"
        evidence = f"{f['eu_row']['best_source_marketplace']} (roi {f['eu_row']['roi']:.1f}%)"
        best_hist_buy = f"£{f['eu_row']['best_source_cost_gbp']:.2f}"
        hist_date = f["eu_row"]["scanned_at"][:10]
    else:
        hist_class = "UK A2A"
        evidence = f"UK dip ({f['uk_row']['buy_box_now'] / f['uk_row']['buy_box_90d'] * 100:.0f}% of 90d avg)"
        best_hist_buy = f"£{f['uk_row']['buy_box_now']:.2f}"
        hist_date = f["uk_row"]["scanned_at"][:10]

    cur_row = f["current_row"]
    current_buy = f"£{cur_row['best_source_cost_gbp']:.2f}" if cur_row and cur_row["best_source_cost_gbp"] else "n/a"
    current_uk = f"£{cur_row['buy_box_now']:.2f}" if cur_row and cur_row["buy_box_now"] else "n/a"

    print(f"{f['current_tag']:<18} | {hist_class:<26} | {f['asin']:<12} | {evidence:<22} | "
          f"{best_hist_buy:<16} | {hist_date:<16} | {current_buy:<14} | {current_uk:<12}")
    print(f"    title: {f['title']}")

print()

# ---- section 7: strongest cases -- EU price meaningfully lower in the ----
# past 30 days AND UK price meaningfully higher at that point, i.e. a
# real spread a competitor could plausibly have bought into.
strong_cases = [
    f for f in flagged
    if f["eu_row"] and f["current_row"]
    and f["eu_row"]["roi"] >= 25.0  # comfortably above the bare 17% floor
]
print(f"Strongest candidates (historical EU roi >= 25%, i.e. a real, not marginal, spread): {len(strong_cases)}\n")
for f in strong_cases[:15]:
    er = f["eu_row"]
    cr = f["current_row"]
    print(
        f"  {f['asin']}  {f['title']}\n"
        f"    tagged '{f['current_tag']}' now, but {er['scanned_at'][:10]}: "
        f"{er['best_source_marketplace']} £{er['best_source_cost_gbp']:.2f} -> "
        f"UK £{er['buy_box_now']:.2f} (roi {er['roi']:.1f}%)\n"
        f"    current: {cr['best_source_marketplace'] or 'n/a'} £{cr['best_source_cost_gbp']:.2f} -> "
        f"UK £{cr['buy_box_now']:.2f} (roi {cr['roi']:.1f}%) as of {cr['scanned_at'][:10]}\n"
    )

# ---- section 8: currently EU/UK A2A but stored evidence looks weak ----
weak = cur.execute("""
    SELECT id AS listing_id, asin, sourcing_tag, sourcing_reasoning_json, detected_at
    FROM seller_new_listings
    WHERE dismissed = 0 AND sourcing_tag IN ('EU A2A', 'UK A2A')
      AND sourcing_reasoning_json IS NOT NULL AND sourcing_reasoning_json != ''
""").fetchall()

weak_flagged = []
for w in weak:
    try:
        reasoning = json.loads(w["sourcing_reasoning_json"])
    except Exception:
        continue

    viable_days = reasoning.get("viable_days_recent") or reasoning.get("uk_dip_days_recent")
    best_roi = reasoning.get("best_roi_recent_pct")

    if viable_days is not None and viable_days <= 1:
        weak_flagged.append({
            "asin": w["asin"], "tag": w["sourcing_tag"],
            "viable_days": viable_days, "best_roi": best_roi,
            "detected_at": w["detected_at"],
        })

print(f"\nCurrently EU A2A / UK A2A with only 1 (or 0-recorded) viable/dip day in their STORED reasoning "
      f"(reflects the OLD 10-day-window code -- see script docstring): {len(weak_flagged)}\n")
for w in weak_flagged[:15]:
    print(f"  {w['asin']}  tag={w['tag']}  viable_days_recorded={w['viable_days']}  "
          f"best_roi_recorded={w['best_roi']}  detected_at={w['detected_at']}")

conn.close()
print("\nDone. Database opened read-only -- nothing was written.")
