"""
Smoke test for the step-2 free screen -- run with
`python test_oa_candidate_screener.py` (same plain-script style as the
other test_*.py files here, no pytest). SP-API and the DB are both
stubbed out, so this costs nothing and needs no credentials to run.

Covers every branch in oa_candidate_screener.screen_asin: gated,
excluded, SP-API unreachable, rank over the ceiling, no fee headroom,
and a genuine screened_in candidate -- plus that a reachable-but-empty
SP-API answer is treated as reachable (not "unavailable"), since a
real ASIN with no rank data is a meaningful answer, not a failure.
"""
from types import SimpleNamespace

from app.services.oa_candidate_screener import screen_asin, MAX_SALES_RANK_UK
from app.services.fee_engine import FeeEngine

RECORD = SimpleNamespace(
    brand="acme", category="123456", category_name="Toys & Games", buy_box_now=25.0,
)


class FakeSpClient:
    """Canned SP-API responses -- None means 'call failed', matching the real client's convention."""

    def __init__(self, catalog_result=None, offer_result=None):
        self.catalog_result = catalog_result
        self.offer_result = offer_result
        self.catalog_calls = []
        self.offer_calls = []

    def search_catalog_items(self, asins, marketplace="UK"):
        self.catalog_calls.append((tuple(asins), marketplace))
        return self.catalog_result

    def get_item_offers(self, asin, marketplace, item_condition="New"):
        self.offer_calls.append((asin, marketplace))
        return self.offer_result


ASIN = "B0TEST1"

# --- 1. gated brand -> rejected before any SP-API call ----------------
sp = FakeSpClient()
result = screen_asin(ASIN, RECORD, sp, {("acme", "")}, set())
assert result == {
    "status": "screened_out", "screened_out_reason": "gated",
    "amazon_price_gbp": None, "sales_rank": None, "target_price_gbp": 0.0,
}
assert not sp.catalog_calls and not sp.offer_calls, "gated ASIN must never spend an SP-API call"
print("gated: ok")

# --- 2. excluded category -> also rejected before any SP-API call -----
sp = FakeSpClient()
result = screen_asin(ASIN, RECORD, sp, set(), {"123456"})
assert result["status"] == "screened_out"
assert result["screened_out_reason"] == "excluded"
assert not sp.catalog_calls and not sp.offer_calls
print("excluded: ok")

# --- 3. SP-API not configured at all (get_sp_api_client() returns None) ---
result = screen_asin(ASIN, RECORD, None, set(), set())
assert result["screened_out_reason"] == "sp_api_unavailable"
print("sp_api not configured: ok")

# --- 4. SP-API configured but both calls fail --------------------------
sp = FakeSpClient(catalog_result=None, offer_result=None)
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result["screened_out_reason"] == "sp_api_unavailable"
print("sp_api both calls failed: ok")

# --- 5. SP-API reachable but genuinely has nothing for this ASIN ------
#     (empty dict / real dict with no price) -- must NOT be read as
#     "unavailable"; it should fall through to the fee check using
#     record.buy_box_now.
sp = FakeSpClient(catalog_result={}, offer_result={"status": "Success", "price": None, "offer_count": 0})
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result["screened_out_reason"] != "sp_api_unavailable", (
    "a reachable-but-empty SP-API answer must not be treated as unavailable"
)
print("sp_api reachable but empty: ok (not misread as unavailable)")

# --- 6. rank over the ceiling -> rejected, no fee calc needed ----------
sp = FakeSpClient(
    catalog_result={ASIN: {"rank": MAX_SALES_RANK_UK + 1, "rank_category": "Toys", "dimensions_cm": None}},
    offer_result={"status": "Success", "price": 25.0, "offer_count": 3},
)
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result == {
    "status": "screened_out", "screened_out_reason": "rank_too_high",
    "amazon_price_gbp": 25.0, "sales_rank": MAX_SALES_RANK_UK + 1, "target_price_gbp": 0.0,
}
print("rank_too_high: ok")

# --- 7. price too low for any profit at target ROI ---------------------
sp = FakeSpClient(
    catalog_result={ASIN: {"rank": 5000, "rank_category": "Toys", "dimensions_cm": None}},
    offer_result={"status": "Success", "price": 4.0, "offer_count": 3},
)
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result["status"] == "screened_out"
assert result["screened_out_reason"] == "no_headroom"
assert result["target_price_gbp"] == 0.0
print("no_headroom: ok")

# --- 8. genuine screened_in candidate -----------------------------------
sp = FakeSpClient(
    catalog_result={ASIN: {"rank": 5000, "rank_category": "Toys", "dimensions_cm": None}},
    offer_result={"status": "Success", "price": 25.0, "offer_count": 3},
)
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result["status"] == "screened_in"
assert result["screened_out_reason"] == ""
assert result["sales_rank"] == 5000
assert result["amazon_price_gbp"] == 25.0
expected_target = FeeEngine.max_source_cost(25.0, "Toys & Games", None, FeeEngine.OA_TARGET_ROI_PCT)
assert result["target_price_gbp"] == expected_target
print(f"screened_in: ok (target_price_gbp={result['target_price_gbp']})")

# --- 9. no SP-API price, but SP-API rank present -> falls back to record.buy_box_now for the fee check ---
sp = FakeSpClient(
    catalog_result={ASIN: {"rank": 5000, "rank_category": "Toys", "dimensions_cm": None}},
    offer_result={"status": "Success", "price": None, "offer_count": 0},
)
result = screen_asin(ASIN, RECORD, sp, set(), set())
assert result["status"] == "screened_in", "should fall back to record.buy_box_now (£25) when SP-API had no price"
assert result["amazon_price_gbp"] is None, "SP-API's own (missing) price must still be recorded as None, not silently swapped for the fallback"
print("no sp_api price, falls back to known buy_box_now for the fee check: ok")

print("\nALL PASS")
