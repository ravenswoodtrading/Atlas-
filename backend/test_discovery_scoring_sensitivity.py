"""
EU A2A Discovery Intelligence -- scoring sensitivity analysis, 2026-09-04.

VALIDATION EXERCISE ONLY -- per explicit instruction, this does NOT
change discovery_intelligence_service.py and does NOT create a new
permanent scoring model. Variants B/D/E/(and C's normalisation logic)
below are throwaway, local-to-this-script functions that reuse the
SAME real, unchanged aggregation methods (get_competitor_brand_evidence/
get_own_scan_performance/get_category_performance) the approved Phase
1/2 service already exposes. Model A calls the real, approved
score_target() unchanged.

Read-only. Creates nothing, writes nothing, triggers no scan, calls no
external API -- confirmed by the same import self-check
test_discovery_intelligence.py already uses.

Run with `python test_discovery_scoring_sensitivity.py`.
"""
import inspect

from app.services import discovery_intelligence_service as dis_module
from app.services.discovery_intelligence_service import (
    DiscoveryIntelligenceService, TOP_CATEGORY_COUNT,
)

import_lines = [
    line for line in inspect.getsource(dis_module).splitlines()
    if line.strip().startswith("import ") or line.strip().startswith("from ")
]
for forbidden in ("app.keepa", "app.sp_api", "serpapi", "brave", "brand_scan_service", "scan_coordinator"):
    assert not any(forbidden in line for line in import_lines)
print("token-safety self-check: ok (imports unchanged from Phase 1/2, no external API)\n")

competitor = DiscoveryIntelligenceService.get_competitor_brand_evidence()
own = DiscoveryIntelligenceService.get_own_scan_performance()
categories = DiscoveryIntelligenceService.get_category_performance()
top_categories = set(
    sorted(categories.keys(), key=lambda c: categories[c]["competitor_eu_a2a"] + categories[c]["competitor_uk_a2a"], reverse=True)[:TOP_CATEGORY_COUNT]
)
all_brands = set(competitor.keys()) | set(own.keys())


# =====================================================================
# VARIANT SCORING FUNCTIONS -- throwaway, this file only
# =====================================================================

def _competitor_points(c):
    c = c or {}
    d = c.get("distinct_competitors", 0)
    if d >= 3:
        return 3
    if d == 2:
        return 2
    if d == 1 and c.get("recent"):
        return 1
    return 0


def score_B_no_volume_bias(brand, c, o, top_cats):
    """
    B: strip signals sensitive to SCAN FREQUENCY rather than genuine
    quality. The approved model's "+3 our last scan found a BUY" is a
    single-event snapshot -- a brand scanned 285 times has had ~57x
    more chances to have that snapshot land on a BUY purely by
    exposure than a brand scanned 5 times, even at an IDENTICAL true
    hit rate. Same for the small-sample "recent hit rate over last 6
    events" bonus. Replaced with a BINARY "has this brand ever
    produced a real BUY" (still real evidence, just not recency/
    frequency-weighted). The negative 3-straight-failures rule is kept
    unchanged -- it already only fires on brands with 3+ tracked
    events either way, not biased by volume in the same direction.
    """
    score = _competitor_points(c)
    o = o or {}
    cat = (c or {}).get("category_name") or o.get("category_name") or ""
    if cat and cat in top_cats:
        score += 1
    scanned = o.get("scanned", 0)
    if scanned:
        if o.get("buy_count", 0) > 0:
            score += 2
        if o.get("avg_profit", 0) > 5:
            score += 1
        events = (o.get("recent_events") or [])[:3]
        if len(events) >= 3 and all(e["buy"] == 0 and e["consider"] == 0 for e in events):
            score -= 2
    return score


def score_C_normalised(brand, c, o, top_cats):
    """
    C: our-performance signal is a RATE (buy_count / scanned), not a
    raw count or a recency snapshot -- deliberately punishes high
    volume with a low conversion rate (MSI's 3/285 ~= 1%) and rewards
    a small sample with a genuinely high rate, on the theory that rate
    is what should matter, volume is just sample size. Tested here
    specifically to see if that theory holds up against real data or
    over-rewards tiny, statistically meaningless samples (see the
    report's own finding on this).
    """
    score = _competitor_points(c)
    o = o or {}
    cat = (c or {}).get("category_name") or o.get("category_name") or ""
    if cat and cat in top_cats:
        score += 1
    scanned = o.get("scanned", 0)
    if scanned:
        rate = o.get("buy_count", 0) / scanned * 100
        if rate >= 30:
            score += 3
        elif rate >= 15:
            score += 2
        elif rate >= 5:
            score += 1
        elif scanned >= 10 and rate == 0:
            score -= 2
        if o.get("avg_profit", 0) > 5:
            score += 1
    return score


def score_D_competitor_only(brand, c, o, top_cats):
    """D: competitor evidence ONLY -- own performance and category both zeroed out."""
    return _competitor_points(c)


def score_E_own_only(brand, c, o, top_cats):
    """E: our own performance ONLY -- competitor evidence and category both zeroed out."""
    o = o or {}
    score = 0
    scanned = o.get("scanned", 0)
    if scanned:
        if o.get("buy_count", 0) > 0 and o.get("recent_events") and o["recent_events"][0]["buy"] > 0:
            score += 3
        rhr = o.get("recent_hit_rate")
        if rhr is not None and rhr >= 20:
            score += 2
        if o.get("avg_profit", 0) > 5:
            score += 1
        events = (o.get("recent_events") or [])[:3]
        if len(events) >= 3 and all(e["buy"] == 0 and e["consider"] == 0 for e in events):
            score -= 2
    return score


def rank(score_fn):
    rows = []
    for brand in all_brands:
        c, o = competitor.get(brand), own.get(brand)
        s = score_fn(brand, c, o, top_categories)
        rows.append((brand, s, c, o))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def tier_of(score):
    """Simple threshold-only tier -- used for variants B-E, which are
    throwaway raw-score functions with no cap concept of their own.
    Model A does NOT use this -- see real_tier_of below, which reflects
    the actual approved model's conflict cap (2026-09-04 fix)."""
    return "HIGH" if score >= 5 else ("MEDIUM" if score >= 2 else "LOW")


def real_tier_of(brand, c, o):
    """The ACTUAL tier score_target() would report for this brand,
    including the conflict cap -- NOT re-derived from the raw score
    alone (a bug in an earlier version of this script: it called
    tier_of(score) on Model A's raw score, which silently ignored the
    cap and showed pre-fix tiers even after the fix landed)."""
    return DiscoveryIntelligenceService.score_target(brand, c, o, top_categories)["tier"]


def print_top(title, rows, n=25, real_tiers=False):
    print(f"\n=== {title} ===")
    print(f"{'Rank':<5}{'Brand':<20}{'Score':>6}  {'Tier':<7}{'Comp':>5}{'Scanned':>9}{'Buy':>5}{'Rate%':>7}")
    print("-" * 70)
    for i, (brand, score, c, o) in enumerate(rows[:n], 1):
        comp_n = c["distinct_competitors"] if c else 0
        scanned = o["scanned"] if o else 0
        buy = o["buy_count"] if o else 0
        rate = round(buy / scanned * 100, 1) if scanned else 0.0
        tier = real_tier_of(brand, c, o) if real_tiers else tier_of(score)
        print(f"{i:<5}{brand:<20}{score:>6}  {tier:<7}{comp_n:>5}{scanned:>9}{buy:>5}{rate:>7}")


rows_A = rank(lambda b, c, o, t: DiscoveryIntelligenceService.score_target(b, c, o, t)["score"])
rows_B = rank(score_B_no_volume_bias)
rows_C = rank(score_C_normalised)
rows_D = rank(score_D_competitor_only)
rows_E = rank(score_E_own_only)

print_top("A. CURRENT APPROVED MODEL (tiers include the conflict cap)", rows_A, real_tiers=True)
print_top("B. WITHOUT RAW SCAN-VOLUME/RECENCY BIAS", rows_B)
print_top("C. OUR PERFORMANCE NORMALISED BY SCANS (rate, not count)", rows_C)
print_top("D. COMPETITOR EVIDENCE ONLY", rows_D)
print_top("E. OUR OWN PERFORMANCE ONLY", rows_E)

# ---- Where does MSI rank under each? ----
def find_rank(rows, brand):
    for i, (b, s, c, o) in enumerate(rows, 1):
        if b == brand:
            return i, s
    return None, None

print("\n=== MSI across all 5 models ===")
for label, rows in [("A", rows_A), ("B", rows_B), ("C", rows_C), ("D", rows_D), ("E", rows_E)]:
    r, s = find_rank(rows, "msi")
    print(f"  Model {label}: rank #{r}, score {s}")

print("\n=== Western Digital across all 5 models (the conflict case) ===")
for label, rows in [("A", rows_A), ("B", rows_B), ("C", rows_C), ("D", rows_D), ("E", rows_E)]:
    r, s = find_rank(rows, "western digital")
    print(f"  Model {label}: rank #{r}, score {s}")

# ---- Bad-ranking pattern checks (§4) ----
print("\n=== §4a: huge scan volume, poor results (scanned >= 100, hit rate < 3%) ===")
for brand in all_brands:
    o = own.get(brand)
    if o and o["scanned"] >= 100 and (o["buy_count"] / o["scanned"] * 100) < 3:
        c = competitor.get(brand)
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} scanned={o['scanned']:<6} buy={o['buy_count']:<4} rate={o['buy_count']/o['scanned']*100:.1f}%  -> Model A rank #{a_rank} ({real_tier_of(brand, c, o)})")

print("\n=== §4b: little scan history, strong competitor evidence (scanned <= 5, competitors >= 3) ===")
found_b = False
for brand in all_brands:
    c, o = competitor.get(brand), own.get(brand)
    scanned = o["scanned"] if o else 0
    if c and c["distinct_competitors"] >= 3 and scanned <= 5:
        found_b = True
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} competitors={c['distinct_competitors']:<4} scanned={scanned:<4} -> Model A rank #{a_rank} ({real_tier_of(brand, c, o)})")
if not found_b:
    print("  none found in real data at this exact threshold")

print("\n=== §4c: strong own performance, little/no competitor evidence (rate >= 30%, scanned >= 3, competitors <= 1) ===")
found_c = False
for brand in all_brands:
    c, o = competitor.get(brand), own.get(brand)
    if not o or o["scanned"] < 3:
        continue
    rate = o["buy_count"] / o["scanned"] * 100
    comp_n = c["distinct_competitors"] if c else 0
    if rate >= 30 and comp_n <= 1:
        found_c = True
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} rate={rate:.0f}% scanned={o['scanned']:<4} competitors={comp_n:<4} -> Model A rank #{a_rank} ({real_tier_of(brand, c, o)})")
if not found_c:
    print("  none found in real data at this exact threshold")

print("\n=== §4d: strong competitor evidence, poor own performance (competitors >= 3, scanned >= 5, buy_count == 0) ===")
for brand in all_brands:
    c, o = competitor.get(brand), own.get(brand)
    if c and c["distinct_competitors"] >= 3 and o and o["scanned"] >= 5 and o["buy_count"] == 0:
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} competitors={c['distinct_competitors']:<4} scanned={o['scanned']:<4} buy=0 -> Model A rank #{a_rank} ({real_tier_of(brand, c, o)})")

print("\n=== §4e: manual brand, poor performance -- SYNTHETIC (Phase 3/manual targets don't exist yet, no real data possible) ===")
manual_score = DiscoveryIntelligenceService.score_target(
    "synthetic_manual_brand", competitor=None,
    own={"scanned": 5, "buy_count": 0, "consider_count": 0, "avg_profit": 0.0, "recent_hit_rate": 0.0,
         "last_buy_at": None, "recent_events": [{"buy": 0, "consider": 0}] * 5},
    top_categories=top_categories,
)
print(f"  synthetic: 5 scans, 0 BUY, no competitor evidence -> score={manual_score['score']}, tier={manual_score['tier']}")

# ---- §6: the new-brand problem ----
print("\n=== §6: NEW-BRAND PROBLEM -- strong competitor evidence, ZERO own scans ===")
new_brand_cases = [
    (brand, c) for brand, c in competitor.items()
    if brand not in own and c["distinct_competitors"] >= 2
]
new_brand_cases.sort(key=lambda kv: kv[1]["distinct_competitors"], reverse=True)
if new_brand_cases:
    for brand, c in new_brand_cases[:10]:
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} competitors={c['distinct_competitors']:<4} eu_a2a={c['eu_a2a_count']:<4} recent={c['recent']!s:<6} -> Model A: rank #{a_rank}, score {a_score}, tier {real_tier_of(brand, c, None)}")
else:
    print("  none at threshold >=2 competitors with zero own scans -- lowering to >=1")
    for brand, c in sorted(
        [(b, cc) for b, cc in competitor.items() if b not in own],
        key=lambda kv: kv[1]["distinct_competitors"], reverse=True,
    )[:10]:
        a_rank, a_score = find_rank(rows_A, brand)
        print(f"  {brand:<20} competitors={c['distinct_competitors']:<4} eu_a2a={c['eu_a2a_count']:<4} -> Model A: rank #{a_rank}, score {a_score}, tier {real_tier_of(brand, c, None)}")

# ---- §7: category validation ----
print("\n=== §7: Category validation (real data) ===")
print(f"{'Category':<28}{'Brands':>7}{'CompA2A':>9}{'OurScans':>10}{'OurBUY':>8}{'HitRate%':>10}")
for name, c in sorted(categories.items(), key=lambda kv: kv[1]["competitor_eu_a2a"] + kv[1]["competitor_uk_a2a"], reverse=True)[:10]:
    hit_rate = round(c["own_buy_count"] / c["own_scanned"] * 100, 1) if c["own_scanned"] else 0.0
    print(f"{name:<28}{c['brand_count']:>7}{c['competitor_eu_a2a']:>9}{c['own_scanned']:>10}{c['own_buy_count']:>8}{hit_rate:>10}")

print("\nALL PASS")
