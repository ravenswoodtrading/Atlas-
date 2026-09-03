"""
Smoke test for the SerpApi-quota-triggered Serper fallback
(2026-08-29) -- run with `python test_serper_fallback.py` (same
plain-script style as the other test_*.py files here, no pytest).

Tamara's instruction: "use both so when we are out of free credits on
one switch to the other." OaSourceDiscoveryService.run_batch already
had a SerpApi quota-safety buffer (SERPAPI_QUOTA_SAFETY_BUFFER,
2026-08-19) that used to just give up on a Google-Shopping-style
search once SerpApi's monthly reserve ran low. It now reaches for
Serper specifically instead (shopping_search.search_uk_shopping_via)
for the rest of that run -- see run_batch's own "Quota safety buffer"
docstring paragraph.

Both scenarios below run the REAL run_batch() end to end (real
ProductMapper, FeeEngine, match-tier scoring, DB writes via
is_test=True rows) -- only the actual external I/O boundaries are
stubbed: Keepa parsing (KeepaParser methods, keyed off a `_price`/
`_ean` marker, same technique as test_product_mapper_duty_cap.py),
the category-name lookup (degrades to {} on its own when
ProductService.api is None -- confirmed safe, not stubbed further),
and the two shopping-search client calls themselves.

Each scenario POISONS the client call that should NOT be used (raises
if it's ever reached) rather than just asserting counts afterward --
proves the fallback is a genuine switch, not just "also calls both."
"""
from app.database.database import SessionLocal
from app.database.models import OaSourceCandidate
from app.services import oa_source_discovery_service as svc
from app.services import shopping_search
from app.services.product_service import ProductService
from app.keepa.parser import KeepaParser

# --- Stub out real Keepa/network entirely -- see module docstring ------
ProductService.__init__ = lambda self: setattr(self, "api", None)

_KEEPA_DEFAULTS = {
    "buy_box_90d": 0, "buy_box_min_90d": 0, "buy_box_max_90d": 0,
    "offers_now": 0, "offers_90d": 0, "sales_rank_now": 0, "sales_rank_90d": 0,
    "sales_drops_30d": 0, "monthly_sales": 0, "monthly_sales_as_of": None,
    "fba_fee": 0.0, "is_hazmat": False,
}
for _name, _val in _KEEPA_DEFAULTS.items():
    setattr(KeepaParser, _name, (lambda v: lambda self: v)(_val))
KeepaParser.ean = lambda self: self.product.get("_ean", "")
KeepaParser.buy_box_now = lambda self: self.product.get("_price", 0)
KeepaParser.price_drop_count = lambda self, days=90: 0
KeepaParser.price_avg = lambda self, days=30: 0

FAKE_EAN = "1234567890123"
RAW = {
    "asin": "B0TESTAAAA", "title": "Test Widget", "brand": "TestBrand", "rootCategory": 1,
    "_ean": FAKE_EAN, "_price": 20.0,
}
# EAN embedded in the title -> classify_shopping_match scores this "ean"
# (a TRUSTED_MATCH_TIERS tier), and "TestRetailer" isn't in
# EXCLUDED_SOURCE_NAMES / a non-UK domain, so classify_shopping_source
# accepts it as a candidate -- both needed for run_batch to actually
# auto-price and tag the candidate, not just find a raw result.
FAKE_RESULT = {
    "title": f"Widget {FAKE_EAN} Blue", "source": "TestRetailer",
    "extracted_price": 50.0, "price": "£50.00",
    "product_link": "https://example.com", "thumbnail": "",
}

db = SessionLocal()


def latest_candidate(run_id):
    return db.query(OaSourceCandidate).filter(OaSourceCandidate.run_id == run_id).first()


# === SCENARIO A: healthy SerpApi quota -> must use SerpApi, Serper never touched ===
shopping_search.get_account_status = lambda: {"plan_searches_left": 1000}
shopping_search.active_provider_name = lambda: "serpapi"
shopping_search.search_uk_shopping = lambda query, num=None: [dict(FAKE_RESULT)]


def _poison_via_a(provider_name, query, num=None):
    raise AssertionError(f"search_uk_shopping_via must NOT be called with healthy quota (got {provider_name!r})")


shopping_search.search_uk_shopping_via = _poison_via_a

result_a = svc.OaSourceDiscoveryService.run_batch(test_mode=True, test_asins=["B0TESTAAAA"], raw_products=[RAW])
assert result_a is not None, f"run_batch returned None unexpectedly"
assert result_a["serpapi_search_count"] == 1
assert result_a["serper_search_count"] == 0
assert result_a["serpapi_quota_stopped"] is False
cand_a = latest_candidate(result_a["run_id"])
assert cand_a.shopping_provider == "serpapi", cand_a.shopping_provider
assert cand_a.price_source == "serpapi_auto"
assert cand_a.match_tier == "ean"
print("healthy quota -> SerpApi used, Serper never touched: ok")

# === SCENARIO B: SerpApi quota at/below SERPAPI_QUOTA_SAFETY_BUFFER -> must switch to Serper ===
shopping_search.get_account_status = lambda: {"plan_searches_left": 5}  # <= buffer (15)


def _poison_serpapi_b(query, num=None):
    raise AssertionError("search_uk_shopping (SerpApi path) must NOT be called once the quota buffer is hit")


shopping_search.search_uk_shopping = _poison_serpapi_b


def _fake_via_b(provider_name, query, num=None):
    assert provider_name == "serper", f"expected serper, got {provider_name!r}"
    return [dict(FAKE_RESULT)]


shopping_search.search_uk_shopping_via = _fake_via_b

result_b = svc.OaSourceDiscoveryService.run_batch(test_mode=True, test_asins=["B0TESTAAAA"], raw_products=[RAW])
assert result_b is not None, "run_batch returned None unexpectedly"
assert result_b["serpapi_search_count"] == 0
assert result_b["serper_search_count"] == 1
assert result_b["serpapi_quota_stopped"] is True
cand_b = latest_candidate(result_b["run_id"])
assert cand_b.shopping_provider == "serper", cand_b.shopping_provider
assert cand_b.price_source == "serpapi_auto"
print("quota buffer hit -> switches to Serper, SerpApi never touched again: ok")

print("\nALL PASS")
