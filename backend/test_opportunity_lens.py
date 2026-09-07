"""
Opportunity Engine 2.0 -- OpportunityLensService tests.

Run with `python test_opportunity_lens.py` (plain-script style, no
pytest). Read-only against the real DB where used -- no writes, no
Keepa/SP-API calls.

Three stages:
0. Synthetic Keepa-shaped fixtures for the two new day-by-day
   reconstructions (SourcingClassifier.compute_competition_spike_
   evidence / compute_price_drop_offer_context, 2026-09-04) --
   confirms the raw parsing/reconstruction layer, not just the lens
   that consumes its output.
1. Synthetic fixtures covering every Action branch.
2. Real-data validation against the named ASINs from the Opportunity
   Engine 2.0 audits/simulations (RAM cluster, price-drop, PEAK,
   source-match examples) -- confirms the PRODUCTION code (not a
   simulation script) reproduces what was already validated.
"""
from datetime import datetime, timezone, timedelta

from app.database.database import SessionLocal
from app.database.models import ProductRecord
from app.services.review_queue_service import ReviewQueueService
from app.services.sourcing_classifier import SourcingClassifier
from app.services import opportunity_lens_service as lens

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


def lead(**overrides):
    base = {
        "source": "scan", "asin": "B0TEST", "title": "Test",
        "profit": 0.0, "profit_90d": 0.0, "roi": 0.0, "roi_90d": 0.0,
        "monthly_sales": 0, "sales_drops_30d": 0, "recommendation": "IGNORE",
        "parsed_report": {}, "freshness": "FRESH", "best_source_marketplace": "DE",
        "match_tier": "", "match_confidence_pct": 0,
    }
    base.update(overrides)
    return base


print("=" * 90)
print("STAGE 0 -- SYNTHETIC KEEPA-SHAPED FIXTURES (day-by-day reconstruction)")
print("=" * 90)

KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)


def kmin(dt):
    return int((dt - KEEPA_EPOCH).total_seconds() // 60)


def keepa_raw(price_series, offer_series, avg90_price_cents, avg90_offers):
    raw = {"csv": [None] * 20, "stats": {"avg90": [None] * 20}}
    raw["csv"][18] = price_series   # CSV_BUY_BOX (triples: time, price_cents, shipping)
    raw["csv"][11] = offer_series   # CSV_OFFER_COUNT_NEW (pairs: time, count)
    raw["stats"]["avg90"][18] = avg90_price_cents
    raw["stats"]["avg90"][11] = avg90_offers
    return raw


now = datetime.now(timezone.utc)

# ---- competition spike, resolved ~50 days ago, price moved since ----
raw = keepa_raw(
    price_series=[kmin(now - timedelta(days=95)), 2500, 0, kmin(now - timedelta(days=45)), 3000, 0],
    offer_series=[kmin(now - timedelta(days=95)), 5, kmin(now - timedelta(days=60)), 20, kmin(now - timedelta(days=50)), 5],
    avg90_price_cents=2900, avg90_offers=6,
)
result = SourcingClassifier.compute_competition_spike_evidence(raw)
check("resolved spike ~50d ago found, price moved +20% since (25->30)", result.get("days_since_last_competition_spike") == 50 and result.get("price_change_since_competition_spike_pct") == 20.0)

# ---- no spike anywhere in the window -> {} (not a fabricated zero) ----
raw_flat = keepa_raw(
    price_series=[kmin(now - timedelta(days=95)), 2500, 0],
    offer_series=[kmin(now - timedelta(days=95)), 5],
    avg90_price_cents=2500, avg90_offers=5,
)
check("no spike anywhere in the window -> {} (never fabricated)", SourcingClassifier.compute_competition_spike_evidence(raw_flat) == {})

# ---- price dip coinciding with elevated offers (competition-driven) ----
raw_dip = keepa_raw(
    price_series=[kmin(now - timedelta(days=95)), 3000, 0, kmin(now - timedelta(days=20)), 2000, 0],
    offer_series=[kmin(now - timedelta(days=95)), 5, kmin(now - timedelta(days=20)), 15],
    avg90_price_cents=2900, avg90_offers=6,
)
dip_result = SourcingClassifier.compute_price_drop_offer_context(raw_dip)
check("price dip found, offers were +150% above their 90d avg at the dip", dip_result.get("offers_change_at_last_price_dip_pct") == 150.0)

# ---- no dip anywhere in the window -> {} ----
check("no dip anywhere in the window -> {} (never fabricated)", SourcingClassifier.compute_price_drop_offer_context(raw_flat) == {})

print(f"\nStage 0: {passed} passed, {failed} failed")

print("\n" + "=" * 90)
print("STAGE 1 -- SYNTHETIC FIXTURES")
print("=" * 90)

# ---- BLOCKED ----
r = lens.compute(lead(recommendation="GATED"))
check("GATED -> BLOCKED", r["action"] == lens.ACTION_BLOCKED)

# ---- HISTORICAL_RECURRING via PEAK_WINDOW (first-gate-authoritative, point 5) ----
r = lens.compute(lead(recommendation="PEAK_WINDOW", parsed_report={"peak_roi": 20, "peak_profit": 30}))
check("PEAK_WINDOW always -> HISTORICAL_RECURRING (no second gate)", r["action"] == lens.ACTION_HISTORICAL_RECURRING)
r_weak_peak = lens.compute(lead(recommendation="PEAK_WINDOW", parsed_report={"peak_roi": 18, "peak_profit": 3}))
check("even a WEAK-looking PEAK_WINDOW record is never dropped (still HISTORICAL_RECURRING, not discarded)", r_weak_peak["action"] == lens.ACTION_HISTORICAL_RECURRING)

# ---- BUY_NOW: clean notable, no risk, fresh ----
r = lens.compute(lead(recommendation="BUY", profit=20, roi=60, monthly_sales=10, sales_drops_30d=5, freshness="FRESH"))
check("clean BUY, no risk, fresh -> BUY_NOW", r["action"] == lens.ACTION_BUY_NOW)

# ---- What's good / freshness_caption (2026-09-04 UI redesign) ----
r_strong = lens.compute(lead(
    recommendation="BUY", profit=50, roi=200, monthly_sales=50, sales_drops_30d=40, freshness="FRESH",
    best_source_marketplace="DE", match_tier="",
))
check("STRONG value shows up as a positive good_fact", any("economics" in g.lower() for g in r_strong["good"]))
check("STRONG evidence shows up as a positive good_fact", any("Confirmed sales" in g for g in r_strong["good"]))
check("confirmed EU A2A source shows up as a positive good_fact", any("EU A2A source" in g for g in r_strong["good"]))
check("freshness is NEVER in the good list -- it's context, not a reason (Tamara's own correction)", not any("check" in g.lower() for g in r_strong["good"]))
check("freshness lives in its own freshness_caption field instead", r_strong["freshness_caption"] == "Source checked recently")
r_stale = lens.compute(lead(recommendation="BUY", profit=50, roi=200, monthly_sales=50, sales_drops_30d=40, freshness="STALE"))
check("freshness_caption reflects the real state even when the action is gated by it", r_stale["freshness_caption"] == "Source not rechecked recently")

# ---- BUY_NOW via widened is_notable (CONSIDER-tier, real evidence, no risk) ----
r = lens.compute(lead(recommendation="CONSIDER", profit=15, roi=30, monthly_sales=20, sales_drops_30d=20, freshness="FRESH"))
check("CONSIDER + real evidence + ROI>25 + no risk -> BUY_NOW (widened is_notable bar)", r["action"] == lens.ACTION_BUY_NOW)

# ---- freshness hard-gates BUY_NOW -> HISTORICAL_RECURRING (points 1/4) ----
r = lens.compute(lead(recommendation="BUY", profit=20, roi=60, monthly_sales=10, sales_drops_30d=5, freshness="STALE"))
check("otherwise-BUY_NOW item with STALE freshness -> HISTORICAL_RECURRING (freshness stays a hard gate)", r["action"] == lens.ACTION_HISTORICAL_RECURRING)
r = lens.compute(lead(recommendation="BUY", profit=20, roi=60, monthly_sales=10, sales_drops_30d=5, freshness="UNAVAILABLE"))
check("UNAVAILABLE freshness also gates -> HISTORICAL_RECURRING", r["action"] == lens.ACTION_HISTORICAL_RECURRING)

# ---- PRICE_DROP_BUY_NOW: notable, STRONG evidence, price falling ----
r = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}},
))
check("notable + STRONG evidence + price falling -> PRICE_DROP_BUY_NOW", r["action"] == lens.ACTION_PRICE_DROP_BUY_NOW)
check("risk flags list the price-falling flag", any("falling" in f["label"] for f in r["risk"]["flags"]))
check("no recurrence data in parsed_report -> not fabricated as a claimed count", r["risk"]["days_at_25pct_roi_90d"] is None)
check("...and the flag label doesn't claim a day count either", not any("priced days" in f["label"] for f in r["risk"]["flags"]))

# ---- price-falling WITH real day-by-day recurrence evidence (Tamara's own ask, 2026-09-04) ----
r = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}, "days_at_25pct_roi_90d": 62, "priced_days_90d": 88},
))
check("recurrence data present -> exposed as structured fields", r["risk"]["days_at_25pct_roi_90d"] == 62 and r["risk"]["priced_days_90d"] == 88)
check(
    "recurrence NOT folded into the price-falling flag's label any more -- it gets its own standalone box (UI redesign, 2026-09-04)",
    not any("62 of the last 88" in f["label"] for f in r["risk"]["flags"]),
)
r_zero = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}, "days_at_25pct_roi_90d": 0, "priced_days_90d": 90},
))
check("a genuinely computed ZERO (0 of 90) is shown honestly, not treated as 'unknown'", r_zero["risk"]["days_at_25pct_roi_90d"] == 0)

# ---- price-dip offer context (Tamara's follow-up ask, 2026-09-04): did offers rise when price last dropped ----
r_dip_competition = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}, "days_since_last_price_dip": 12, "offers_change_at_last_price_dip_pct": 85.0},
))
check("dip context present, offers were elevated at the dip -> flagged as looking competition-driven (in the detail sub-line, not the headline)", "looks competition-driven" in (r_dip_competition["risk"]["flags"][0]["detail"] or ""))
check("...and exposed structurally too", r_dip_competition["risk"]["days_since_last_price_dip"] == 12 and r_dip_competition["risk"]["offers_change_at_last_price_dip_pct"] == 85.0)
r_dip_not_competition = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}, "days_since_last_price_dip": 12, "offers_change_at_last_price_dip_pct": 3.0},
))
check("dip context present, offers were normal -> flagged as NOT looking competition-driven (detail sub-line)", "doesn't look competition-driven" in (r_dip_not_competition["risk"]["flags"][0]["detail"] or ""))
r_no_dip_context = lens.compute(lead(
    recommendation="CONSIDER", profit=50, profit_90d=400, roi=20, roi_90d=150,
    monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -40, "offer_change": 0}},
))
check("no dip context in report_json (pre-2026-09-04 scan) -> not fabricated", r_no_dip_context["risk"]["days_since_last_price_dip"] is None)

# ---- competition-spike price outcome (Tamara's own ask, 2026-09-04): what did price do after the last spike ----
r_spike = lens.compute(lead(
    recommendation="CONSIDER", profit=50, roi=40, monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": 2, "offer_change": 80}, "days_since_last_competition_spike": 9, "price_change_since_competition_spike_pct": -22.0},
))
check("spike context folded into the Competition surging flag's detail sub-line", any("After the last spike (9d ago), price moved -22" in (f["detail"] or "") for f in r_spike["risk"]["flags"]))
check("...and exposed structurally", r_spike["risk"]["price_change_since_competition_spike_pct"] == -22.0)

# ---- BUY_WITH_CAUTION: notable, STRONG evidence, competition surging (not falling price) ----
r = lens.compute(lead(
    recommendation="CONSIDER", profit=50, roi=40, monthly_sales=30, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": 2, "offer_change": 80}},
))
check("notable + STRONG evidence + competition surging (no price drop) -> BUY_WITH_CAUTION", r["action"] == lens.ACTION_BUY_WITH_CAUTION)

# ---- HIGH_VALUE_LOW_CONFIDENCE: exceptional value, INSUFFICIENT evidence ----
r = lens.compute(lead(
    recommendation="LOW_SCORE", profit=60.99, profit_90d=1138.27, roi=12.8, roi_90d=239.7,
    monthly_sales=0, sales_drops_30d=0, freshness="FRESH",
))
check("exceptional value, zero sales evidence -> HIGH_VALUE_LOW_CONFIDENCE (never silently dropped)", r["action"] == lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE)
check(
    "insufficient-evidence risk flag honestly states 'not necessarily poor demand', not 'confirmed poor demand'",
    any("Not necessarily poor demand" in (f["detail"] or "") for f in r["risk"]["flags"]),
)
check("no sales-evidence claim leaks into What's good when evidence is insufficient", not any("sales" in g.lower() for g in r["good"]))

# ---- HIGH_VALUE_LOW_CONFIDENCE: exceptional value, MODERATE (not STRONG) evidence + a risk flag ----
r = lens.compute(lead(
    recommendation="LOW_SCORE", profit=60.99, profit_90d=1138.27, roi=12.8, roi_90d=239.7,
    monthly_sales=0, sales_drops_30d=3, freshness="FRESH",
    parsed_report={"trend": {"price_change": -70, "offer_change": 100}},
))
check(
    "exceptional value, MODERATE (not STRONG) evidence + risk flags present -> HIGH_VALUE_LOW_CONFIDENCE "
    "(risk stacked on only-moderate evidence isn't enough to say buy-now-with-caution)",
    r["action"] == lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,
)
check("but the SAME numbers with STRONG evidence instead become actionable", lens.compute(lead(
    recommendation="LOW_SCORE", profit=60.99, profit_90d=1138.27, roi=12.8, roi_90d=239.7,
    monthly_sales=0, sales_drops_30d=20, freshness="FRESH",
    parsed_report={"trend": {"price_change": -70, "offer_change": 100}},
))["action"] == lens.ACTION_PRICE_DROP_BUY_NOW)

# ---- INVESTIGATE: moderate value, INSUFFICIENT evidence ----
r = lens.compute(lead(recommendation="IGNORE", profit=10, roi=20, monthly_sales=0, sales_drops_30d=0, freshness="FRESH"))
check("moderate value, no evidence -> INVESTIGATE", r["action"] == lens.ACTION_INVESTIGATE)

# ---- WATCH: everything else ----
r = lens.compute(lead(recommendation="IGNORE", profit=0, roi=0, monthly_sales=0, sales_drops_30d=0))
check("weak/no value -> WATCH (defensive fallback)", r["action"] == lens.ACTION_WATCH)

# ---- VERIFY SOURCE MATCH flag ----
r = lens.compute(lead(recommendation="CONSIDER", profit=5, roi=600, roi_90d=600, monthly_sales=5))
check("ROI > 500% -> verify_source_match flag set", r["verify_source_match"] is True)
check("verify_source_match flag appears in risk.flags", any("Verify source match" in f["label"] for f in r["risk"]["flags"]))
r = lens.compute(lead(recommendation="CONSIDER", profit=5, roi=500, roi_90d=500, monthly_sales=5))
check("ROI exactly 500% -> NOT flagged (strictly greater-than)", r["verify_source_match"] is False)

# ---- source confidence: UK-OA with no confirmed match (real B0GCD2GCR1 pattern) ----
r = lens.compute(lead(
    recommendation="LOW_CONFIDENCE", profit=151, profit_90d=195, roi=336, roi_90d=434,
    monthly_sales=100, sales_drops_30d=68, best_source_marketplace="UK-OA", match_tier="",
    parsed_report={"trend": {"price_change": -18, "offer_change": 145}},
))
check("UK-OA source with empty match_tier -> a risk flag, never a 'good' claim", any("No confirmed source match" in f["label"] for f in r["risk"]["flags"]))
check("...and the unconfirmed source never leaks into What's good", not any("source" in g.lower() for g in r["good"]))
r_eu = lens.compute(lead(recommendation="BUY", profit=20, roi=60, monthly_sales=25, sales_drops_30d=20, best_source_marketplace="DE", match_tier=""))
check("EU A2A source never needs a match_tier -- no false 'unconfirmed' risk flag", not any("No confirmed source match" in f["label"] for f in r_eu["risk"]["flags"]))
check("...and a confirmed EU A2A source shows up in What's good", any("EU A2A source" in g for g in r_eu["good"]))

# ---- point 7: today and 90d always shown separately ----
r = lens.compute(lead(profit=10, profit_90d=999, roi=5, roi_90d=400))
check("today's and 90d figures both present, distinctly, in value", r["value"]["today_profit"] == 10 and r["value"]["typical_profit_90d"] == 999)

print(f"\nStage 1: {passed} passed, {failed} failed")

print("\n" + "=" * 90)
print("STAGE 2 -- REAL-DATA VALIDATION (named ASINs from the Opportunity Engine 2.0 audits)")
print("=" * 90)

NAMED_EXPECTATIONS = {
    # asin: expected action (per the approved Phase 2/3B design + the new
    # STRONG-evidence-required-alongside-risk refinement)
    "B0F9448K9C": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, drops=3 (moderate) + real risk flags
    "B0DT4L3CPH": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, drops=14 (moderate) + price falling
    "B00T7XSBUM": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, zero evidence + price falling
    "B09C5VDH74": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, zero evidence, price stable
    "B01C96GAGA": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # printer, zero evidence
    "B0GCD2GCR1": None,  # Karcher -- STRONG evidence; freshness-dependent (see below)
    "B0F943FMNC": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, drops=4 (moderate) + price falling
    "B0DSQMKYLN": lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,   # RAM, zero evidence + price falling
    "B08F7V27DX": None,  # Braun -- STRONG evidence; freshness-dependent
}

db = SessionLocal()
try:
    for asin, expected in NAMED_EXPECTATIONS.items():
        record = (
            db.query(ProductRecord)
            .filter(ProductRecord.asin == asin)
            .order_by(ProductRecord.scanned_at.desc())
            .first()
        )
        if record is None:
            print(f"  SKIP {asin}: not found in the current DB")
            continue

        item = ReviewQueueService._scan_lead_dict(record)
        result = lens.compute(item)

        if expected is not None:
            check(f"{asin} ({record.title[:35]}) -> {result['action']}", result["action"] == expected)
        else:
            # Freshness-dependent (STRONG evidence + risk -> PRICE_DROP_
            # BUY_NOW/BUY_WITH_CAUTION if fresh, HISTORICAL_RECURRING if
            # stale -- both are correct outcomes, only NOT ending up
            # HIGH_VALUE_LOW_CONFIDENCE matters (its real sales evidence
            # is strong, so it must never read as "low confidence").
            check(
                f"{asin} ({record.title[:35]}) -> {result['action']} "
                f"(STRONG evidence must never read as HIGH_VALUE_LOW_CONFIDENCE)",
                result["action"] != lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE,
            )
            print(f"    evidence tier: {result['evidence']['tier']}, freshness caption: {result['freshness_caption']}")

    # ---- RAM cluster: sample real records, confirm none are silently non-visible ----
    print("\n  RAM cluster sample:")
    ram_rows = (
        db.query(ProductRecord)
        .filter(ProductRecord.title.ilike("%corsair%vengeance%"))
        .order_by(ProductRecord.scanned_at.desc())
        .limit(15)
        .all()
    )
    seen = set()
    ram_actions = {}
    for r in ram_rows:
        if r.asin in seen:
            continue
        seen.add(r.asin)
        item = ReviewQueueService._scan_lead_dict(r)
        result = lens.compute(item)
        ram_actions[r.asin] = result["action"]
    check(f"every sampled RAM record produces a valid Action (n={len(ram_actions)})", all(a in lens.ACTION_LABELS for a in ram_actions.values()))
    for a, act in list(ram_actions.items())[:8]:
        print(f"    {a}: {act}")

    # ---- PEAK examples: confirm none silently dropped ----
    print("\n  PEAK_WINDOW sample:")
    peak_rows = (
        db.query(ProductRecord)
        .filter(ProductRecord.recommendation == "PEAK_WINDOW", ProductRecord.review.is_(None))
        .order_by(ProductRecord.scanned_at.desc())
        .limit(10)
        .all()
    )
    peak_ok = 0
    for r in peak_rows:
        item = ReviewQueueService._scan_lead_dict(r)
        result = lens.compute(item)
        if result["action"] == lens.ACTION_HISTORICAL_RECURRING:
            peak_ok += 1
    check(f"every sampled unreviewed PEAK_WINDOW record -> HISTORICAL_RECURRING, none dropped (n={len(peak_rows)})", peak_ok == len(peak_rows))

finally:
    db.close()

print(f"\nStage 2 complete.")

print("\n" + "=" * 90)
print(f"TOTAL: {passed} passed, {failed} failed")
print("=" * 90)

if failed:
    raise SystemExit(1)
