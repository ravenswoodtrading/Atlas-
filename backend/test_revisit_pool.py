"""
Opportunity Engine 2.0, Phase 3A -- Revisit Pool tests.

Run with `python test_revisit_pool.py` (same plain-script style as the
other test_*.py files here, no pytest).

Two stages, per the Phase 3A implementation plan:

1. SYNTHETIC FIXTURES (points 13) -- pure-logic tests against
   RevisitPoolService.select_and_rank / build_log_entry, using plain
   constructed objects. No DB touched, no Keepa/SP-API call possible
   (asserted explicitly below by monkeypatching BrandScanService.scan
   to raise if it's ever called during this stage).

2. CONTROLLED LIVE-DATA SELECTION TEST (point 14) -- calls
   RevisitPoolService.get_candidates() against the REAL atlas.db.
   Read-only: get_candidates() never calls BrandScanService.scan and
   never writes to the DB, so this is safe to run against production
   data. Prints the real candidate list for manual inspection.

NOT tested here (deliberately): RevisitPoolService.run_batch(dry_run=
False) against the real DB -- that spends real Keepa tokens via
BrandScanService.scan, which this test file must never trigger. The
first real (non-dry-run) batch happens only when the daily scheduler
tick actually fires, or when a human explicitly triggers one.
"""
from datetime import datetime, timedelta, timezone

from app.database.models import ProductRecord
from app.services import revisit_pool_service as rps_module
from app.services.revisit_pool_service import RevisitPoolService
from app.services.brand_scan_service import BrandScanService

NOW = datetime(2026, 9, 4, 12, 0, 0)


def _record(id=1, asin="B0TEST0001", title="Test Product", profit=0.0, profit_90d=0.0,
            roi=0.0, roi_90d=0.0, recommendation="IGNORE", monthly_sales=0, sales_drops_30d=0,
            scanned_at=NOW):
    r = ProductRecord(
        asin=asin, title=title, profit=profit, profit_90d=profit_90d, roi=roi, roi_90d=roi_90d,
        recommendation=recommendation, monthly_sales=monthly_sales, sales_drops_30d=sales_drops_30d,
        scanned_at=scanned_at,
    )
    r.id = id
    return r


passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


print("=" * 90)
print("STAGE 1 -- SYNTHETIC FIXTURES (pure logic, no DB, no Keepa)")
print("=" * 90)

# ---- token-safety self-check: dry_run must NEVER call BrandScanService.scan ----
def _forbidden_scan(*args, **kwargs):
    raise AssertionError("BrandScanService.scan was called during a dry_run / selection-only path -- MUST NOT happen")

_original_scan = BrandScanService.scan
BrandScanService.scan = _forbidden_scan

# get_candidates() touches the real DB (read-only) -- fine to call here,
# it must not raise, proving no scan() call happens on this path either.
try:
    _ = RevisitPoolService.select_and_rank([], limit=25, stale_days=30, now=NOW)
    check("select_and_rank on an empty list never calls BrandScanService.scan", True)
except AssertionError:
    check("select_and_rank on an empty list never calls BrandScanService.scan", False)

dry_result = None
try:
    # run_batch's own get_candidates() call hits the real DB (read-only,
    # harmless) -- what matters is dry_run=True must short-circuit
    # BEFORE ever reaching the scan() call below.
    dry_result = RevisitPoolService.run_batch(limit=5, dry_run=True)
    check("run_batch(dry_run=True) completes without calling BrandScanService.scan", True)
except AssertionError:
    check("run_batch(dry_run=True) completes without calling BrandScanService.scan", False)

BrandScanService.scan = _original_scan

if dry_result is not None:
    check("dry_run result carries dry_run=True", dry_result["dry_run"] is True)
    check("dry_run result spent zero (scanned=0)", dry_result["scanned"] == 0)

# ---- select_and_rank: filtering ----
raw = [
    {"asin": "A_PROFITABLE_OLD", "title": "old profitable", "previous_record": _record(profit=50),
     "best_ever_profit": 50.0, "last_scanned_at": NOW - timedelta(days=45)},
    {"asin": "B_PROFITABLE_RECENT", "title": "recent profitable", "previous_record": _record(profit=200),
     "best_ever_profit": 200.0, "last_scanned_at": NOW - timedelta(days=5)},   # too recent -- excluded
    {"asin": "C_UNPROFITABLE_OLD", "title": "old unprofitable", "previous_record": _record(profit=-10),
     "best_ever_profit": 0.0, "last_scanned_at": NOW - timedelta(days=90)},     # never profitable -- excluded
    {"asin": "D_HIGH_PROFIT_OLD", "title": "big old profit", "previous_record": _record(profit=900),
     "best_ever_profit": 900.0, "last_scanned_at": NOW - timedelta(days=40)},
    {"asin": "E_NO_SCAN_DATE", "title": "no date", "previous_record": _record(),
     "best_ever_profit": 30.0, "last_scanned_at": None},                        # no date -- excluded
    {"asin": "F_TIE_OLDER", "title": "tie, older", "previous_record": _record(profit=50),
     "best_ever_profit": 50.0, "last_scanned_at": NOW - timedelta(days=60)},
]

ranked = RevisitPoolService.select_and_rank(raw, limit=25, stale_days=30, now=NOW)
ranked_asins = [c["asin"] for c in ranked]

check("excludes an ASIN scanned <30 days ago", "B_PROFITABLE_RECENT" not in ranked_asins)
check("excludes an ASIN with best_ever_profit <= 0", "C_UNPROFITABLE_OLD" not in ranked_asins)
check("excludes an ASIN with no last_scanned_at", "E_NO_SCAN_DATE" not in ranked_asins)
check("includes both 30+ day, ever-profitable ASINs", set(["A_PROFITABLE_OLD", "D_HIGH_PROFIT_OLD", "F_TIE_OLDER"]) <= set(ranked_asins))
check("ranks profit-primary, highest first", ranked_asins[0] == "D_HIGH_PROFIT_OLD")
check(
    "ties on profit break toward the ASIN stale LONGER (F, 60d, before A, 45d)",
    ranked_asins.index("F_TIE_OLDER") < ranked_asins.index("A_PROFITABLE_OLD"),
)
check("days_since_last_scan is computed correctly", next(c for c in ranked if c["asin"] == "D_HIGH_PROFIT_OLD")["days_since_last_scan"] == 40)

# ---- select_and_rank: cap at limit ----
many_raw = [
    {"asin": f"ASIN{i}", "title": "t", "previous_record": _record(profit=float(i)),
     "best_ever_profit": float(i), "last_scanned_at": NOW - timedelta(days=31)}
    for i in range(1, 51)
]
capped = RevisitPoolService.select_and_rank(many_raw, limit=25, stale_days=30, now=NOW)
check("caps at the requested limit (25 of 50 eligible)", len(capped) == 25)
check("cap keeps the highest-profit ones (ASIN50 first)", capped[0]["asin"] == "ASIN50")

# ---- build_log_entry: recovered / not recovered ----
prev_not_notable = _record(id=1, asin="X1", recommendation="IGNORE", roi=5, roi_90d=5, monthly_sales=0, sales_drops_30d=0)
fresh_now_notable = _record(id=2, asin="X1", recommendation="BUY", roi=40, roi_90d=40, monthly_sales=10, sales_drops_30d=5)
candidate_x1 = {"asin": "X1", "title": "t", "previous_record": prev_not_notable, "days_since_last_scan": 40}
log_recovered = RevisitPoolService.build_log_entry(candidate_x1, fresh_now_notable, rank=1)
check("recovered=True when previously non-notable, now clears is_notable", log_recovered.recovered is True)
check("resulting_action is BUY_NOW for a recovered notable item", log_recovered.resulting_action == "BUY_NOW")
check("status is 'scanned' when a genuine fresh record exists", log_recovered.status == "scanned")

prev_already_notable = _record(id=3, asin="X2", recommendation="BUY", roi=40, roi_90d=40, monthly_sales=10, sales_drops_30d=5)
fresh_still_notable = _record(id=4, asin="X2", recommendation="BUY", roi=45, roi_90d=45, monthly_sales=12, sales_drops_30d=6)
candidate_x2 = {"asin": "X2", "title": "t", "previous_record": prev_already_notable, "days_since_last_scan": 40}
log_not_new = RevisitPoolService.build_log_entry(candidate_x2, fresh_still_notable, rank=2)
check("recovered=False when it was ALREADY notable before (not a new recovery)", log_not_new.recovered is False)

prev_borderline = _record(id=5, asin="X3", recommendation="IGNORE", roi=5, roi_90d=5)
fresh_still_ignore = _record(id=6, asin="X3", recommendation="IGNORE", roi=6, roi_90d=6)
candidate_x3 = {"asin": "X3", "title": "t", "previous_record": prev_borderline, "days_since_last_scan": 40}
log_no_change = RevisitPoolService.build_log_entry(candidate_x3, fresh_still_ignore, rank=3)
check("recovered=False when it's still IGNORE after revisit", log_no_change.recovered is False)
check("resulting_action falls back to the raw recommendation when neither notable nor borderline-tier", log_no_change.resulting_action == "IGNORE")

prev_c = _record(id=7, asin="X4", recommendation="IGNORE")
fresh_consider = _record(id=8, asin="X4", recommendation="CONSIDER", roi=10, roi_90d=10)
candidate_x4 = {"asin": "X4", "title": "t", "previous_record": prev_c, "days_since_last_scan": 40}
log_borderline = RevisitPoolService.build_log_entry(candidate_x4, fresh_consider, rank=4)
check("resulting_action is BORDERLINE for a CONSIDER-tier fresh recommendation", log_borderline.resulting_action == "BORDERLINE")

# ---- build_log_entry: no fresh record produced ----
prev_e = _record(id=9, asin="X5")
candidate_x5 = {"asin": "X5", "title": "t", "previous_record": prev_e, "days_since_last_scan": 40}
log_missing = RevisitPoolService.build_log_entry(candidate_x5, None, rank=5)
check("status is 'no_fresh_record' when fresh is None", log_missing.status == "no_fresh_record")
check("recovered defaults False when no fresh record", log_missing.recovered is False)

log_same_id = RevisitPoolService.build_log_entry(candidate_x5, prev_e, rank=5)
check("status is 'no_fresh_record' when the 'fresh' record IS the previous record (same id)", log_same_id.status == "no_fresh_record")

# ---- build_log_entry: VERIFY_SOURCE_MATCH flag boundary ----
prev_f = _record(id=10, asin="X6", recommendation="IGNORE")
fresh_extreme_roi = _record(id=11, asin="X6", recommendation="CONSIDER", roi=501, roi_90d=200)
candidate_x6 = {"asin": "X6", "title": "t", "previous_record": prev_f, "days_since_last_scan": 40}
log_flagged = RevisitPoolService.build_log_entry(candidate_x6, fresh_extreme_roi, rank=6)
check("verify_source_match=True when best ROI > 500%", log_flagged.verify_source_match is True)

fresh_exactly_500 = _record(id=12, asin="X7", recommendation="CONSIDER", roi=500, roi_90d=200)
candidate_x7 = {"asin": "X7", "title": "t", "previous_record": prev_f, "days_since_last_scan": 40}
log_not_flagged = RevisitPoolService.build_log_entry(candidate_x7, fresh_exactly_500, rank=7)
check("verify_source_match=False at exactly 500% (strictly greater-than)", log_not_flagged.verify_source_match is False)

print(f"\nStage 1: {passed} passed, {failed} failed")

print("\n" + "=" * 90)
print("STAGE 2 -- CONTROLLED LIVE-DATA SELECTION TEST (real atlas.db, read-only)")
print("=" * 90)

before = passed + failed

try:
    rps_module.ensure_table_exists()
    check("revisit_log table creates cleanly against the real atlas.db (additive, create_all only)", True)
except Exception as exc:
    print(f"  exception: {exc}")
    check("revisit_log table creates cleanly against the real atlas.db (additive, create_all only)", False)

BrandScanService.scan = _forbidden_scan
try:
    real_candidates = RevisitPoolService.get_candidates(limit=RevisitPoolService.DEFAULT_DAILY_LIMIT)
    check("get_candidates() against the real DB never calls BrandScanService.scan", True)
finally:
    BrandScanService.scan = _original_scan

check(f"returns at most {RevisitPoolService.DEFAULT_DAILY_LIMIT} candidates", len(real_candidates) <= RevisitPoolService.DEFAULT_DAILY_LIMIT)
check("every candidate is 30+ days stale", all(c["days_since_last_scan"] >= 30 for c in real_candidates))
check("every candidate has best_ever_profit > 0", all(c["best_ever_profit"] > 0 for c in real_candidates))
profits = [c["best_ever_profit"] for c in real_candidates]
check("results are sorted profit-descending", profits == sorted(profits, reverse=True))

print(f"\nReal candidate pool (top {len(real_candidates)}):")
for i, c in enumerate(real_candidates, start=1):
    print(f"  {i:2d}. {c['asin']}  {c['title'][:40]:40s}  best_ever_profit=£{c['best_ever_profit']:>8.2f}  "
          f"{c['days_since_last_scan']}d since last scan")

print(f"\nStage 2: {passed - before} checks passed this stage ({failed} total failed so far)")

print("\n" + "=" * 90)
print(f"TOTAL: {passed} passed, {failed} failed")
print("=" * 90)

if failed:
    raise SystemExit(1)
