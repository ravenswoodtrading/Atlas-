"""
Regression test for the 2026-08-29 match-confidence fixes, prompted by
a real false positive Tamara caught by eye: B0DV6CDXKT (a UbiQuiti
USW-FLEX-2.5G-8-POE switch) auto-matched to Optdex's GBP114 listing for
the base "USW-FLEX" model -- a different, cheaper product in the same
family -- and landed straight in the Review Queue with no human check.
Run with `python test_oa_match_confidence.py` (plain script, no pytest,
same style as the other test_*.py files here).

Three things changed, each covered below:

1. classify_match/classify_shopping_match's title-similarity canonical
   string no longer double-counts the brand when the Amazon title
   already starts with it (see _canonical_product_string).
2. _promote_if_qualifying's automated (price_source == "serpapi_auto")
   path now only trusts EAN/MPN/brand+MPN tiers
   (TRUSTED_MATCH_TIERS_AUTO_PROMOTE) -- "brand_title" (Medium
   confidence, no real identifier) can no longer silently promote
   itself into the real Review Queue. The manual path (a human
   confirming a price themselves) is unaffected -- see match_trusted_enough.
3. A promoted candidate's match_tier/match_confidence_pct/
   source_confidence are now copied onto the ProductRecord it creates,
   so review_queue_service/review_queue.html can actually show "how
   sure Atlas was" -- previously this only ever lived on the
   OaSourceCandidate row, invisible from the Review Queue itself.

Uses the REAL run_batch/_promote_if_qualifying/classify_shopping_match
(no reimplementation) -- only external I/O boundaries are stubbed,
same technique as test_serper_fallback.py.
"""
from app.database.database import SessionLocal
from app.database.models import ProductRecord
from app.services import oa_source_discovery_service as svc
from app.services import shopping_search
from app.services.product_service import ProductService
from app.keepa.parser import KeepaParser

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

db = SessionLocal()

# === 1. Canonical string no longer double-counts a brand the title already has ===
assert svc.OaSourceDiscoveryService._canonical_product_string("Ubiquiti", "UbiQuiti USW-FLEX-2.5G-8-POE") \
    == "ubiquiti usw-flex-2.5g-8-poe"
assert svc.OaSourceDiscoveryService._canonical_product_string("Philips", "Airfryer XL") \
    == "philips airfryer xl"
print("_canonical_product_string: no double-brand: ok")

# === 2. The real B0DV6CDXKT listings: Optdex must no longer be trusted at ALL ===
real_candidates = [
    {"title": "Ubiquiti UniFi Flex 2.5G PoE 8-Port PoE++ Managed Switch", "source": "LambdaTek", "extracted_price": 191.56},
    {"title": "Ubiquiti UniFi Switch Flex 2.5G", "source": "IT Planet", "extracted_price": 177.50},
    {"title": "Ubiquiti Networks Ubiquiti UniFi Switch USW-FLEX", "source": "Optdex", "extracted_price": 114.00},
    {"title": "Ubiquiti USW-FLEX-2.5G-8 UniFi Flex 2.5G Ultra Compact 8 Port POE/USB-C Powered Managed Switch - Network Switches - Managed Switches", "source": "Cartridge World UK", "extracted_price": 177.56},
]
ean, mpn, brand, title = "0810084695968", "USW-Flex-2.5G-8-PoE", "Ubiquiti", "UbiQuiti USW-FLEX-2.5G-8-POE"

tiers = {c["source"]: svc.OaSourceDiscoveryService.classify_shopping_match(ean, mpn, brand, title, c) for c in real_candidates}
assert tiers["Optdex"] == "title_only", f"Optdex (wrong product) must now be untrusted, got {tiers['Optdex']!r}"
assert tiers["Optdex"] not in svc.TRUSTED_MATCH_TIERS
print(f"real B0DV6CDXKT data: Optdex tier={tiers['Optdex']!r} (untrusted) -- ok. Full tiers: {tiers}")

# === 3. _promote_if_qualifying: brand_title blocked on auto, allowed on manual ===
FAKE_EAN = "1234567890123"
RAW = {
    "asin": "B0TESTCONF1", "title": "Test Widget", "brand": "TestBrand", "rootCategory": 1,
    "_ean": FAKE_EAN, "_price": 20.0,
}
FAKE_RESULT_BRAND_TITLE = {
    # No EAN/MPN string in this one -- forces brand_title tier (brand
    # present + high similarity), not ean/mpn.
    "title": "TestBrand Test Widget Deluxe Edition", "source": "SomeRetailer",
    "extracted_price": 5.0, "price": "£5.00",
    "product_link": "https://example.com", "thumbnail": "",
}


def _poison(*a, **kw):
    raise AssertionError("shopping_search must not be called in this scenario")


shopping_search.get_account_status = lambda: {"plan_searches_left": 1000}
shopping_search.active_provider_name = lambda: "serpapi"
shopping_search.search_uk_shopping = lambda query, num=None: [dict(FAKE_RESULT_BRAND_TITLE)]
shopping_search.search_uk_shopping_via = _poison

result = svc.OaSourceDiscoveryService.run_batch(test_mode=True, test_asins=["B0TESTCONF1"], raw_products=[RAW])
assert result is not None

cand = db.query(svc.OaSourceCandidate).filter(svc.OaSourceCandidate.run_id == result["run_id"]).first()
assert cand.match_tier == "brand_title", f"expected brand_title tier, got {cand.match_tier!r}"
assert cand.price_source == "serpapi_auto"
assert cand.added_to_review_queue is False, \
    "a Medium-confidence brand_title auto-match must NOT auto-promote into the real Review Queue"
print("brand_title tier + serpapi_auto: correctly blocked from auto-promoting: ok")

# Now simulate a human confirming that same candidate manually -- the
# match-tier bar must NOT apply on the manual path (pre-existing
# behaviour, must still hold after this change).
cand.price_source = "manual"
db.add(cand)
db.commit()

from app.models.product import Product
product = Product(asin="B0TESTCONF1", title="Test Widget", brand="TestBrand", category="Fixture",
                   buy_box_now=20.0, buy_box_90d=20.0, ean=FAKE_EAN)
promoted = svc.OaSourceDiscoveryService._promote_if_qualifying(db, cand, product, "", 5.0, dry_run=False)
assert promoted is True, "a human-confirmed (manual) brand_title match must still be allowed to promote"
print("brand_title tier + manual confirm: still allowed to promote: ok")

# === 4. Match confidence lands on the ProductRecord, readable via review_queue_service ===
from app.services.review_queue_service import ReviewQueueService

record = (
    db.query(ProductRecord)
    .filter(ProductRecord.asin == "B0TESTCONF1")
    .order_by(ProductRecord.scanned_at.desc())
    .first()
)
assert record is not None, "manual promotion should have created a ProductRecord"
assert record.match_tier == "brand_title"
assert record.match_confidence_pct == 55
assert record.source_confidence == "Medium"

row = ReviewQueueService._scan_lead_dict(record)
assert row["match_tier"] == "brand_title"
assert row["match_confidence_pct"] == 55
assert row["source_confidence"] == "Medium"
print("match confidence persisted onto ProductRecord and surfaced via _scan_lead_dict: ok")

# A record with no match_tier at all (ordinary scan pipeline) must
# default to "" / 0, not error or carry over another record's value.
# (flush, not just construct -- mapped_column defaults apply at
# flush/INSERT time, not at Python object construction, same as any
# other ORM column here; a real ordinary scan record always goes
# through save_opportunity -> commit before review_queue_service ever
# sees it.)
plain_record = ProductRecord(asin="B0TESTCONF2", title="Plain scan lead", brand="X", category="Y")
db.add(plain_record)
db.flush()
plain_row = ReviewQueueService._scan_lead_dict(plain_record)
assert plain_row["match_tier"] == ""
assert plain_row["match_confidence_pct"] == 0
print("ordinary (non-OA) ProductRecord defaults to empty match confidence: ok")

# --- cleanup ---
db.query(ProductRecord).filter(ProductRecord.asin.in_(["B0TESTCONF1", "B0TESTCONF2"])).delete(synchronize_session=False)
db.commit()
db.close()

print("\nALL PASS")
