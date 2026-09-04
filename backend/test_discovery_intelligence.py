"""
EU A2A Discovery Intelligence -- Phase 1/2 validation, 2026-09-04.

Run with `python test_discovery_intelligence.py` (same plain-script
style as the other test_*.py files here, no pytest).

PURELY READ-ONLY -- this file creates nothing and cleans up nothing,
because DiscoveryIntelligenceService writes nothing to begin with (see
its own module docstring). No Keepa/SP-API/SerpApi/Brave import
appears anywhere in discovery_intelligence_service.py -- confirmed by
grepping its own imports, and re-confirmed here by asserting the
module doesn't reference app.keepa/app.sp_api anywhere.

Prints the REAL ranked list against the real Atlas database, for the
brief's own "show us the real rankings for existing brands, we will
validate before persisting anything" requirement.
"""
import inspect

from app.services import discovery_intelligence_service as dis_module
from app.services.discovery_intelligence_service import DiscoveryIntelligenceService

# ---- Token-safety self-check ----
# Checks actual import lines only (not prose/comments/docstrings, which
# legitimately name these modules when explaining WHY a field looks the
# way it does -- e.g. the brand_query docstring above references
# SellerWatchService.run_check's own scan() call by name).
import_lines = [
    line for line in inspect.getsource(dis_module).splitlines()
    if line.strip().startswith("import ") or line.strip().startswith("from ")
]
for forbidden in ("app.keepa", "app.sp_api", "serpapi", "brave", "brand_scan_service", "scan_coordinator"):
    assert not any(forbidden in line for line in import_lines), (
        f"discovery_intelligence_service.py imports something matching {forbidden!r} -- Phase 1/2 must be read-only"
    )
print("token-safety self-check: no Keepa/SP-API/SerpApi/Brave/scan-triggering import anywhere in this file: ok")
print(f"  (actual imports: {[l.strip() for l in import_lines]})")

# ---- Correctness: competitor evidence must NOT be keyed by brand_query ----
competitor = DiscoveryIntelligenceService.get_competitor_brand_evidence()
assert not any(b.startswith("competitor:") for b in competitor.keys()), (
    "a 'competitor:<seller>' key leaked into brand-level evidence -- "
    "this means brand_query was used instead of ProductRecord.brand somewhere"
)
print(f"competitor evidence: {len(competitor)} distinct brands, none keyed by the 'competitor:<seller>' brand_query artifact: ok")

own = DiscoveryIntelligenceService.get_own_scan_performance()
assert not any(b.startswith("competitor:") for b in own.keys())
print(f"own scan performance: {len(own)} distinct brands, none keyed by the 'competitor:<seller>' brand_query artifact: ok")

categories = DiscoveryIntelligenceService.get_category_performance()
print(f"category performance: {len(categories)} distinct categories: ok")

# ---- Scoring sanity: synthetic fixtures ----
# NOTE on fixture shape (2026-09-04, EU-scope fix): every `own={}`
# fixture below now needs the eu_-prefixed fields too -- score_target
# reads THOSE for its OUR_SCANS bonuses, not the marketplace-agnostic
# scanned/buy_count/etc (kept for display context only, see
# get_own_scan_performance's own docstring point 5 -- Ninja/Shark's
# bug was exactly a mismatch between these two). Each fixture below
# mirrors eu_ fields onto the same values as the non-prefixed ones,
# since these fixtures are all meant to represent genuine EU A2A
# activity (a mixed EU/non-EU fixture isn't needed here -- the real-
# data section further down already exercises that against Ninja/
# Shark themselves).
strong = DiscoveryIntelligenceService.score_target(
    "legotest",
    competitor={"distinct_competitors": 6, "recent": True, "category_name": "Toys & Games"},
    own={
        "scanned": 6, "buy_count": 4, "consider_count": 1,
        "avg_profit": 8.42, "avg_buy_profit": 8.42, "buy_rate": 66.7,
        "recent_hit_rate": 67.0, "last_buy_at": "2026-09-01",
        "recent_events": [{"buy": 1, "consider": 0}, {"buy": 1, "consider": 0}, {"buy": 1, "consider": 0}],
        "eu_a2a_count": 6, "eu_buy_count": 4, "eu_consider_count": 1,
        "eu_avg_buy_profit": 8.42, "eu_buy_rate": 66.7,
        "eu_recent_hit_rate": 67.0, "eu_last_buy_at": "2026-09-01",
        "eu_recent_events": [{"buy": 1, "consider": 0}, {"buy": 1, "consider": 0}, {"buy": 1, "consider": 0}],
    },
    top_categories={"Toys & Games"},
)
assert strong["tier"] == "HIGH", strong
# All three sources genuinely contributed here (competitor evidence,
# our own BUYs, AND the category bonus) -- the source label must show
# all three, not silently drop one just because two others are also
# present (an earlier version of score_target's source-combining logic
# had exactly that bug, caught by this assertion).
assert strong["source"] == "COMPETITOR + OUR SCANS + CATEGORY", strong["source"]
print(f"synthetic fixture (strong competitor + our own BUYs): score={strong['score']}, tier={strong['tier']}, source={strong['source']}: ok")

weak = DiscoveryIntelligenceService.score_target(
    "brandxtest",
    competitor=None,
    own={
        "scanned": 5, "buy_count": 0, "consider_count": 0, "avg_profit": 0.0,
        "recent_hit_rate": 0.0, "last_buy_at": None,
        "recent_events": [{"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}],
        "eu_a2a_count": 5, "eu_buy_count": 0, "eu_consider_count": 0,
        "eu_avg_buy_profit": 0.0, "eu_buy_rate": 0.0,
        "eu_recent_hit_rate": 0.0, "eu_last_buy_at": None,
        "eu_recent_events": [{"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}],
    },
    top_categories={"Toys & Games"},
)
assert weak["tier"] == "LOW", weak
assert weak["score"] < 0, "3 straight empty scans must genuinely penalise the score, not just fail to help it"
print(f"synthetic fixture (3 straight empty scans, no competitor evidence): score={weak['score']}, tier={weak['tier']}: ok")

no_history = DiscoveryIntelligenceService.score_target(
    "brandnew", competitor=None, own=None, top_categories={"Toys & Games"},
)
assert no_history["score"] == 0
assert no_history["source"] == "MANUAL"
print(f"synthetic fixture (brand new manual target, zero history): score={no_history['score']}, source={no_history['source']} -- not penalised for having no data yet: ok")

# ---- Regression test for the exact "Western Digital" conflict fixed 2026-09-04 ----
# Direct bug reproduction: a CONSIDER-tier row's profit used to count
# toward the profitability bonus even with buy_count=0. Fixed by
# gating that bonus on buy_count > 0 and avg_buy_profit (BUY-tier
# only) -- this alone is enough to bring the score below HIGH here, so
# the cap doesn't even need to fire for THIS specific pattern (see the
# separate case below for when it does).
wd_style = DiscoveryIntelligenceService.score_target(
    "conflicttest",
    competitor={"distinct_competitors": 5, "recent": True, "category_name": "Computers & Accessories"},
    own={
        "scanned": 5, "buy_count": 0, "consider_count": 1,
        "avg_profit": 12.0, "avg_buy_profit": 0.0, "buy_rate": 0.0,
        "recent_hit_rate": 0.0, "last_buy_at": None,
        "recent_events": [{"buy": 0, "consider": 1}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}],
        "eu_a2a_count": 5, "eu_buy_count": 0, "eu_consider_count": 1,
        "eu_avg_buy_profit": 0.0, "eu_buy_rate": 0.0,
        "eu_recent_hit_rate": 0.0, "eu_last_buy_at": None,
        "eu_recent_events": [{"buy": 0, "consider": 1}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}],
    },
    top_categories={"Computers & Accessories"},
)
assert wd_style["tier"] != "HIGH", f"conflict case must never reach HIGH on competitor/category evidence alone -- got {wd_style}"
print(f"synthetic fixture (Western Digital-style conflict: 5 competitors, top category, 0 confirmed BUY, one profitable CONSIDER): tier={wd_style['tier']} (was HIGH before the fix): ok")

# ---- Regression test for the CAP mechanism itself (not just the profit-bonus fix) ----
# Real pattern found in the live database ("gardena"): buy_count=0 (no
# ASIN's CURRENT/latest scan is a confirmed BUY) but recent_hit_rate is
# still positive, because recent_hit_rate reads raw historical scan
# EVENTS, which can include an ASIN's now-superseded earlier BUY
# reading from before a later rescan changed it. Competitor(+3) +
# category(+1) + this stale-event hit-rate bonus(+2) = 6, which WOULD
# reach HIGH on evidence that doesn't represent this brand's CURRENT
# confirmed state -- this is exactly what the cap exists to catch.
stale_hit_rate_conflict = DiscoveryIntelligenceService.score_target(
    "staletest",
    competitor={"distinct_competitors": 3, "recent": True, "category_name": "Home & Garden"},
    own={
        "scanned": 3, "buy_count": 0, "consider_count": 2,
        "avg_profit": 11.0, "avg_buy_profit": 0.0, "buy_rate": 0.0,
        "recent_hit_rate": 33.3, "last_buy_at": None,
        "recent_events": [{"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 1}],
        "eu_a2a_count": 3, "eu_buy_count": 0, "eu_consider_count": 2,
        "eu_avg_buy_profit": 0.0, "eu_buy_rate": 0.0,
        "eu_recent_hit_rate": 33.3, "eu_last_buy_at": None,
        "eu_recent_events": [{"buy": 0, "consider": 0}, {"buy": 0, "consider": 0}, {"buy": 0, "consider": 1}],
    },
    top_categories={"Home & Garden"},
)
assert stale_hit_rate_conflict["raw_tier"] == "HIGH", "this fixture must genuinely reach HIGH pre-cap, or it isn't testing the cap at all"
assert stale_hit_rate_conflict["tier"] == "MEDIUM"
assert stale_hit_rate_conflict["capped"] is True
print(f"synthetic fixture (real 'gardena' pattern: stale recent_hit_rate despite buy_count=0): raw_tier={stale_hit_rate_conflict['raw_tier']} -> capped tier={stale_hit_rate_conflict['tier']}: ok")

# A brand with the SAME strong competitor evidence but genuinely NOT
# YET tested (own=None, not "tested and failed") must NOT be capped --
# that's "promising, insufficient data", a different case entirely.
promising = DiscoveryIntelligenceService.score_target(
    "promisingtest",
    competitor={"distinct_competitors": 5, "recent": True, "category_name": "Computers & Accessories"},
    own=None,
    top_categories={"Computers & Accessories"},
)
assert promising["capped"] is False, "an untested brand must never be treated as a conflict -- it hasn't failed anything"
assert promising["tier"] != "LOW"
print(f"synthetic fixture (same competitor evidence, but genuinely UNTESTED -- own=None): tier={promising['tier']}, capped={promising['capped']} -- correctly NOT punished for lacking data: ok")

# A brand that HAS a confirmed BUY must be exempt from the cap even at
# a low overall rate (e.g. makita: 847 scanned, 1 BUY) -- the cap is
# specifically about ZERO confirmed BUYs, not about a low rate.
has_one_buy = DiscoveryIntelligenceService.score_target(
    "onebuytest",
    competitor={"distinct_competitors": 3, "recent": True, "category_name": "DIY & Tools"},
    own={
        "scanned": 847, "buy_count": 1, "consider_count": 0,
        "avg_profit": 20.0, "avg_buy_profit": 20.0, "buy_rate": 0.1,
        "recent_hit_rate": 0.0, "last_buy_at": "2026-08-20",
        "recent_events": [{"buy": 0, "consider": 0}] * 3,
        "eu_a2a_count": 847, "eu_buy_count": 1, "eu_consider_count": 0,
        "eu_avg_buy_profit": 20.0, "eu_buy_rate": 0.1,
        "eu_recent_hit_rate": 0.0, "eu_last_buy_at": "2026-08-20",
        "eu_recent_events": [{"buy": 0, "consider": 0}] * 3,
    },
    top_categories={"DIY & Tools"},
)
assert has_one_buy["capped"] is False, "even ONE confirmed BUY exempts a brand from the conflict cap -- the cap is about zero confirmation, not a low rate"
print(f"synthetic fixture (1 confirmed BUY out of 847 scanned -- exempt from the cap by design): tier={has_one_buy['tier']}, capped={has_one_buy['capped']}: ok")

# ---- Regression test for the GATED override fixed 2026-09-04 ----
# Direct bug reproduction: the real "Canon" case -- strong evidence
# (would easily reach HIGH on its own) but the brand is in
# gated_brands with no category restriction, i.e. Atlas cannot
# currently sell it at all. Must be forced to LOW regardless of how
# good the competitor/own-scan evidence looks.
gated_style = DiscoveryIntelligenceService.score_target(
    "gatedtest",
    competitor={"distinct_competitors": 6, "recent": True, "category_name": "Stationery & Office Supplies"},
    own={
        "scanned": 10, "buy_count": 3, "consider_count": 1,
        "avg_profit": 15.0, "avg_buy_profit": 15.0, "buy_rate": 30.0,
        "recent_hit_rate": 50.0, "last_buy_at": "2026-09-01",
        "recent_events": [{"buy": 1, "consider": 0}] * 3,
        "eu_a2a_count": 10, "eu_buy_count": 3, "eu_consider_count": 1,
        "eu_avg_buy_profit": 15.0, "eu_buy_rate": 30.0,
        "eu_recent_hit_rate": 50.0, "eu_last_buy_at": "2026-09-01",
        "eu_recent_events": [{"buy": 1, "consider": 0}] * 3,
    },
    top_categories={"Stationery & Office Supplies"},
    extra_gated_pairs_by_name={("gatedtest", "")},
)
assert gated_style["raw_tier"] == "HIGH", "this fixture must genuinely earn HIGH pre-gating, or it isn't testing the override at all"
assert gated_style["gated"] is True
assert gated_style["tier"] == "LOW", f"a gated brand must never be recommended regardless of score -- got {gated_style}"
print(f"synthetic fixture (real 'Canon' pattern: strong evidence, whole-brand gated): raw_tier={gated_style['raw_tier']} -> gated tier={gated_style['tier']}: ok")

# A brand with the exact same strong evidence but NOT gated must be
# completely unaffected -- confirms the override doesn't leak onto
# brands that were never in gated_brands.
ungated_style = DiscoveryIntelligenceService.score_target(
    "gatedtest",
    competitor={"distinct_competitors": 6, "recent": True, "category_name": "Stationery & Office Supplies"},
    own={
        "scanned": 10, "buy_count": 3, "consider_count": 1,
        "avg_profit": 15.0, "avg_buy_profit": 15.0, "buy_rate": 30.0,
        "recent_hit_rate": 50.0, "last_buy_at": "2026-09-01",
        "recent_events": [{"buy": 1, "consider": 0}] * 3,
        "eu_a2a_count": 10, "eu_buy_count": 3, "eu_consider_count": 1,
        "eu_avg_buy_profit": 15.0, "eu_buy_rate": 30.0,
        "eu_recent_hit_rate": 50.0, "eu_last_buy_at": "2026-09-01",
        "eu_recent_events": [{"buy": 1, "consider": 0}] * 3,
    },
    top_categories={"Stationery & Office Supplies"},
    extra_gated_pairs_by_name=set(),
)
assert ungated_style["gated"] is False
assert ungated_style["tier"] == "HIGH"
print(f"synthetic fixture (identical evidence, NOT gated): tier={ungated_style['tier']}, gated={ungated_style['gated']} -- override correctly scoped to gated brands only: ok")

# ---- Regression test for the EU A2A plug-risk caution added 2026-09-04 ----
# User-raised concern (Ninja/Shark), not a confirmed bad ranking --
# must be a pure informational flag, NEVER a score/tier change.
plug_risk_case = DiscoveryIntelligenceService.score_target(
    "ninjatest",
    competitor={"distinct_competitors": 3, "recent": True, "category_name": "Home & Garden", "eu_a2a_count": 2, "uk_a2a_count": 0},
    own={
        "scanned": 2, "buy_count": 1, "consider_count": 0, "eu_a2a_count": 1,
        "avg_profit": 10.0, "avg_buy_profit": 10.0, "buy_rate": 50.0,
        "recent_hit_rate": 50.0, "last_buy_at": "2026-09-01",
        "recent_events": [{"buy": 1, "consider": 0}],
        "eu_buy_count": 1, "eu_consider_count": 0,
        "eu_avg_buy_profit": 10.0, "eu_buy_rate": 100.0,
        "eu_recent_hit_rate": 50.0, "eu_last_buy_at": "2026-09-01",
        "eu_recent_events": [{"buy": 1, "consider": 0}],
    },
    top_categories=set(),
)
assert plug_risk_case["plug_risk"] is True
assert plug_risk_case["tier"] == "HIGH", "plug-risk caution must NOT change the tier -- it's informational only, per explicit instruction not to reject the brand"
print(f"synthetic fixture (real 'Ninja' pattern: Home & Garden + real EU A2A evidence): plug_risk={plug_risk_case['plug_risk']}, tier UNCHANGED at {plug_risk_case['tier']}: ok")

# Same category, but the evidence is UK A2A only (no eu_a2a_count at
# all) -- must NOT fire, since UK A2A sources domestically and the
# plug is never in question there.
uk_only_case = DiscoveryIntelligenceService.score_target(
    "uktest",
    competitor={"distinct_competitors": 3, "recent": True, "category_name": "Home & Garden", "eu_a2a_count": 0, "uk_a2a_count": 2},
    own=None, top_categories=set(),
)
assert uk_only_case["plug_risk"] is False, "UK A2A evidence alone must never trigger the EU-plug caution -- UK A2A sources domestically"
print(f"synthetic fixture (Home & Garden but UK A2A evidence only): plug_risk={uk_only_case['plug_risk']} -- correctly not flagged: ok")

# A non-electrical category (e.g. Toys & Games) with real EU A2A
# evidence must not fire either -- the flag is category-scoped, not a
# blanket EU A2A warning.
non_electrical_case = DiscoveryIntelligenceService.score_target(
    "toytest",
    competitor={"distinct_competitors": 3, "recent": True, "category_name": "Toys & Games", "eu_a2a_count": 2, "uk_a2a_count": 0},
    own=None, top_categories=set(),
)
assert non_electrical_case["plug_risk"] is False
print(f"synthetic fixture (Toys & Games with real EU A2A evidence): plug_risk={non_electrical_case['plug_risk']} -- correctly not flagged (non-electrical category): ok")

# ---- Real rankings against the real database ----
targets = DiscoveryIntelligenceService.list_discovery_targets(limit=500)
print(f"\n{len(targets)} brands with real evidence (competitor and/or our own scans) ranked:\n")

high = [t for t in targets if t["tier"] == "HIGH"]
medium = [t for t in targets if t["tier"] == "MEDIUM"]
low = [t for t in targets if t["tier"] == "LOW"]
print(f"HIGH: {len(high)}  MEDIUM: {len(medium)}  LOW: {len(low)}\n")

print(f"{'Brand':<20}{'Score':>6}  {'Tier':<7}{'Source':<32}{'Category':<20}{'Comp':>5}{'EUBuy':>6}{'EUScan':>7}{'NonEUBuy':>9}")
print("-" * 118)
for t in targets[:25]:
    comp = t["competitor_evidence"]
    own_p = t["own_performance"]
    # EU-scoped buy/scan counts (2026-09-04 fix) alongside non-EU BUY
    # count, so any brand still relying on non-EU (UK-OA etc) evidence
    # is visible at a glance in this table, not just in the reasons.
    eu_buy = own_p["eu_buy_count"] if own_p else 0
    eu_scan = own_p["eu_a2a_count"] if own_p else 0
    non_eu_buy = (own_p["buy_count"] - own_p["eu_buy_count"]) if own_p else 0
    print(
        f"{t['brand']:<20}{t['score']:>6}  {t['tier']:<7}{t['source']:<32}{(t['category_name'] or '--')[:19]:<20}"
        f"{(comp['distinct_competitors'] if comp else 0):>5}"
        f"{eu_buy:>6}{eu_scan:>7}{non_eu_buy:>9}"
    )

print("\nTop category performance (by combined competitor EU/UK A2A evidence):")
top_cats = sorted(categories.items(), key=lambda kv: kv[1]["competitor_eu_a2a"] + kv[1]["competitor_uk_a2a"], reverse=True)[:8]
for name, c in top_cats:
    print(f"  {name:<28} brands={c['brand_count']:<4} competitor EU A2A={c['competitor_eu_a2a']:<4} UK A2A={c['competitor_uk_a2a']:<4} our BUYs={c['own_buy_count']}")

# ---- Detail: show the full "Why?" reasons for the #1 ranked brand ----
if targets:
    top = targets[0]
    print(f"\nFull explanation for the #1 ranked brand ({top['brand']}, score {top['score']}, {top['tier']}):")
    for r in top["reasons"]:
        sign = "+" if r["points"] >= 0 else ""
        print(f"  [{r['kind']}] {sign}{r['points']}  {r['text']}")

print("\nALL PASS")
