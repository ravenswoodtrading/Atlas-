"""
Tests for the Review Queue backend build (ASIN dedup + unified queue
priority + VA first-pass improvements), atlas-review-queue-backend-v1.md's
follow-up task, 2026-09-03.

Run with `python test_review_queue_backend_build.py` (same plain-script
style as the other test_*.py files here, no pytest).

merge_by_asin/_item_priority/_build_merged_item are pure functions of
already-built per-source dicts (exactly what list_leads()/
list_consider_leads() already produce) -- every test here constructs
those dicts directly, no DB or Keepa access needed. A couple of tests
exercise LeadAnalysisService._fetch_inventory_snapshot's aggregation
logic directly against a fake SP-API-shaped payload, also no real
network/DB access.
"""
from app.services.review_queue_service import (
    ReviewQueueService,
    QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
    ATTENTION_CATEGORY_SOURCING,
)
from app.services.lead_analysis_service import LeadAnalysisService


def item(source, asin="B0TEST0001", **overrides):
    """A minimal per-source lead dict -- only `source`/`asin` are ever
    indexed directly by the code under test; everything else is read
    via .get(), so omitted keys are safely None, matching a real
    _scan_lead_dict/_lead_dict/competitor-branch dict's shape closely
    enough for these tests."""
    base = {"source": source, "asin": asin, "recommendation": None}
    base.update(overrides)
    return base


# =========================================================================
# DEDUPLICATION
# =========================================================================

# --- Scan + Competitor, same ASIN -> one item ---------------------------
leads = [
    item("scan", recommendation="BUY", freshness="FRESH"),
    item("competitor", recommendation="WATCH"),
]
merged = ReviewQueueService.merge_by_asin(leads)
assert len(merged) == 1, len(merged)
assert set(merged[0]["sources"]) == {"scan", "competitor"}
assert len(merged[0]["source_items"]) == 2, "both original per-source dicts must be preserved"
print("dedup: scan + competitor -> one item: ok")

# --- Scan + VA, same ASIN -> one item ------------------------------------
leads = [
    item("scan", recommendation="CONSIDER"),
    item("lead", recommendation="BUY"),
]
merged = ReviewQueueService.merge_by_asin(leads)
assert len(merged) == 1
assert set(merged[0]["sources"]) == {"scan", "lead"}
print("dedup: scan + VA -> one item: ok")

# --- Competitor + VA, same ASIN -> one item ------------------------------
leads = [
    item("competitor", recommendation="BUY", freshness="FRESH"),
    item("lead", recommendation="WATCH"),
]
merged = ReviewQueueService.merge_by_asin(leads)
assert len(merged) == 1
assert set(merged[0]["sources"]) == {"competitor", "lead"}
print("dedup: competitor + VA -> one item: ok")

# --- Scan + Competitor + VA, same ASIN -> one item, all retained --------
leads = [
    item("scan", recommendation="BUY", freshness="FRESH"),
    item("competitor", recommendation="WATCH"),
    item("lead", recommendation="BUY"),
]
merged = ReviewQueueService.merge_by_asin(leads)
assert len(merged) == 1
assert set(merged[0]["sources"]) == {"scan", "competitor", "lead"}
assert merged[0]["recommendations"] == {"scan": "BUY", "competitor": "WATCH", "lead": "BUY"}
print("dedup: scan + competitor + VA -> one item, all three sources retained: ok")

# --- Different ASINs never merge -----------------------------------------
leads = [item("scan", asin="B0AAA", recommendation="BUY", freshness="FRESH"),
         item("scan", asin="B0BBB", recommendation="BUY", freshness="FRESH")]
merged = ReviewQueueService.merge_by_asin(leads)
assert len(merged) == 2, "different ASINs must never merge into one item"
print("dedup: different ASINs stay separate: ok")


# =========================================================================
# PRIORITY
# =========================================================================

# --- Clear BUY (scan) -----------------------------------------------------
p = ReviewQueueService._item_priority(item("scan", recommendation="BUY", freshness="FRESH"))
assert p == QUEUE_PRIORITY_BUY_NOW, p
print("priority: clear scan BUY -> BUY_NOW: ok")

# --- VA BUY -----------------------------------------------------------------
p = ReviewQueueService._item_priority(item(
    "lead", recommendation="BUY", already_in_inventory=False, buyability_blocker=None, has_similar_rejection=False,
))
assert p == QUEUE_PRIORITY_BUY_NOW, p
print("priority: clean VA BUY -> BUY_NOW: ok")

# --- Borderline -------------------------------------------------------------
for rec in ("CONSIDER", "PEAK_WINDOW", "LOW_CONFIDENCE", "LOW_SCORE"):
    p = ReviewQueueService._item_priority(item("scan", recommendation=rec))
    assert p == QUEUE_PRIORITY_BORDERLINE, (rec, p)
print("priority: CONSIDER/PEAK_WINDOW/LOW_CONFIDENCE/LOW_SCORE -> BORDERLINE: ok")

# --- Stale BUY (previously BUY, now stale) ----------------------------------
p = ReviewQueueService._item_priority(item("scan", recommendation="BUY", freshness="STALE"))
assert p == QUEUE_PRIORITY_NEEDS_ATTENTION, p
print("priority: stale BUY -> NEEDS_ATTENTION: ok")

# --- No buyable offer --------------------------------------------------------
p = ReviewQueueService._item_priority(item("scan", recommendation="BUY", freshness="UNAVAILABLE"))
assert p == QUEUE_PRIORITY_NEEDS_ATTENTION, p
print("priority: no buyable offer (UNAVAILABLE) -> NEEDS_ATTENTION: ok")

# --- Conflicting recommendations (merged item) ------------------------------
leads = [
    item("scan", recommendation="BUY", freshness="FRESH"),
    item("competitor", recommendation="WATCH"),
]
leads[0]["conflict_note"] = "Atlas's own scan pipeline currently marks this ASIN differently."
merged = ReviewQueueService.merge_by_asin(leads)
assert merged[0]["queue_priority"] == QUEUE_PRIORITY_NEEDS_ATTENTION, merged[0]["queue_priority"]
assert merged[0]["conflict"] is True
print("priority: conflicting sources -> NEEDS_ATTENTION, conflict flag set: ok")

# --- VA WATCH -> VA_TO_REVIEW -------------------------------------------
p = ReviewQueueService._item_priority(item("lead", recommendation="WATCH"))
assert p == QUEUE_PRIORITY_VA_TO_REVIEW, p
print("priority: VA WATCH -> VA_TO_REVIEW: ok")


# =========================================================================
# VA-SPECIFIC FIRST-PASS FLAGS
# =========================================================================

# --- BUY VA lead, clean -----------------------------------------------------
p = ReviewQueueService._item_priority(item(
    "lead", recommendation="BUY", already_in_inventory=False, buyability_blocker=None, has_similar_rejection=False,
))
assert p == QUEUE_PRIORITY_BUY_NOW, p
print("VA: clean BUY -> BUY_NOW: ok")

# --- Rejected-history match --------------------------------------------------
p = ReviewQueueService._item_priority(item(
    "lead", recommendation="BUY", already_in_inventory=False, buyability_blocker=None, has_similar_rejection=True,
))
assert p == QUEUE_PRIORITY_NEEDS_ATTENTION, p
print("VA: BUY with a matching rejection-history entry -> NEEDS_ATTENTION: ok")

# --- Inventory match ----------------------------------------------------------
p = ReviewQueueService._item_priority(item(
    "lead", recommendation="BUY", already_in_inventory=True, buyability_blocker=None, has_similar_rejection=False,
))
assert p == QUEUE_PRIORITY_NEEDS_ATTENTION, p
print("VA: BUY already in inventory -> NEEDS_ATTENTION (flagged, not auto-rejected): ok")

# --- Buyability failure --------------------------------------------------------
p = ReviewQueueService._item_priority(item(
    "lead", recommendation="BUY", already_in_inventory=False, buyability_blocker="fbm_only", has_similar_rejection=False,
))
assert p == QUEUE_PRIORITY_NEEDS_ATTENTION, p
print("VA: BUY with a buyability blocker -> NEEDS_ATTENTION: ok")

# --- WATCH VA lead --------------------------------------------------------------
p = ReviewQueueService._item_priority(item("lead", recommendation="WATCH"))
assert p == QUEUE_PRIORITY_VA_TO_REVIEW, p
print("VA: WATCH -> VA_TO_REVIEW: ok")


# =========================================================================
# Merged item field coverage (section 1's field list) + attention category
# =========================================================================
leads = [
    item("scan", recommendation="BUY", freshness="FRESH", title="Widget", brand="Acme",
         best_source_marketplace="DE", best_source_cost_gbp=10.0, buy_box_now=25.0,
         profit=8.0, roi=45.0, score=80, reasoning={"historical_a2a_evidence": {"eu_a2a": {"DE": {}}}}),
    item("lead", recommendation="BUY", lead_id=42, lead_subsource="sheet", rationale="Looks solid",
         already_in_inventory=False, buyability_blocker=None, has_similar_rejection=False,
         inventory_detail=None, similar_rejections=[]),
]
merged = ReviewQueueService.merge_by_asin(leads)[0]
assert merged["category"] == ATTENTION_CATEGORY_SOURCING
assert merged["title"] == "Widget"
assert merged["best_source_marketplace"] == "DE"
assert merged["buy_box_now"] == 25.0
assert merged["profit"] == 8.0 and merged["roi"] == 45.0
assert merged["va_info"]["lead_id"] == 42
assert merged["va_info"]["rationale"] == "Looks solid"
assert "scan" in merged["historical_sourcing_evidence"]
print("merged item: display fields, va_info, historical evidence, category=SOURCING all present: ok")


# =========================================================================
# Inventory snapshot aggregation (LeadAnalysisService)
# =========================================================================
class FakeSpClient:
    def get_inventory_summaries(self, marketplace="UK", seller_skus=None):
        return {
            "SKU-A": {"asin": "B0INV0001", "fulfillable": 3, "title": "Thing A"},
            "SKU-B": {"asin": "B0INV0001", "fulfillable": 7, "title": "Thing A (dup SKU)"},
            "SKU-C": {"asin": "B0INV0002", "fulfillable": 0, "title": "Thing B, none left"},
        }


import app.services.lead_analysis_service as las_module
_original_get_sp_api_client = las_module.get_sp_api_client
las_module.get_sp_api_client = lambda: FakeSpClient()

snapshot = LeadAnalysisService._fetch_inventory_snapshot()
assert snapshot["B0INV0001"]["fulfillable"] == 7, "must keep the SKU with the higher fulfillable count"
assert snapshot["B0INV0002"]["fulfillable"] == 0
print("inventory snapshot: multi-SKU same ASIN collapsed to the higher stock count: ok")

las_module.get_sp_api_client = lambda: None
assert LeadAnalysisService._fetch_inventory_snapshot() == {}, "unconfigured SP-API must degrade to {}, not raise"
print("inventory snapshot: unconfigured SP-API degrades gracefully to {}: ok")

las_module.get_sp_api_client = _original_get_sp_api_client


# =========================================================================
# Existing Review Queue behaviour still works (regression guard)
# =========================================================================
real_items = ReviewQueueService.list_queue_items()
for it in real_items[:25]:
    assert it["queue_priority"] in (
        QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW, QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
    )
    assert it["category"] == ATTENTION_CATEGORY_SOURCING
    assert isinstance(it["source_items"], list) and len(it["source_items"]) >= 1

summary = ReviewQueueService.queue_priority_summary()
assert summary["unique_items"] <= summary["raw_source_rows"]
assert summary["duplicates_merged"] == summary["raw_source_rows"] - summary["unique_items"]
print(f"regression guard (list_queue_items against real data, {len(real_items)} items, "
      f"{summary['duplicates_merged']} duplicates merged): ok")

# Existing count_summary/consider_summary keys must be byte-for-byte
# unchanged -- Dashboard badges read these exact keys (section 8).
cs = ReviewQueueService.count_summary()
assert set(cs.keys()) == {"total", "star_buys", "buys", "peak", "low_confidence", "low_score", "competitor", "leads"}, cs.keys()
consider = ReviewQueueService.consider_summary()
assert set(consider.keys()) == {"total", "today"}, consider.keys()
print("regression guard (count_summary/consider_summary keys unchanged): ok")

print("\nALL PASS")
