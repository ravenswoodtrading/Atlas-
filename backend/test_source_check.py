"""
Smoke test for the EU A2A source-marketplace check -- run with
`python test_source_check.py` (same plain-script style as the other
test_*.py files here, no pytest).

Covers the part that actually decides whether a lead is buyable:
resolve_source_marketplace's free-text matching, that a non-EU/absent
marketplace spends no Keepa call at all, and every blocker branch of
VerdictService.check_source_marketplace against fake Keepa payloads --
including the distinction that matters most, "amazon_oos" (park it, it
comes back on restock) vs "fbm_only" (dead). Keepa is stubbed out, so
this costs nothing to run.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.routes.verdict import templates
from app.services.verdict_service import VerdictService, resolve_source_marketplace

client = TestClient(app)

# --- 1. resolver ------------------------------------------------------
cases = [
    (("DE", None), "DE"),
    ((None, "https://www.amazon.de/dp/B0BSCLTYJT"), "DE"),
    ((None, "Amazon Italy"), "IT"),
    ((None, "amazon.es/gp/product/x"), "ES"),
    ((None, "Home Bargains, Filton"), None),
    ((None, "EUA2A"), None),
    ((None, None), None),
    (("", "  amazon.FR  "), "FR"),
]
for args, expected in cases:
    got = resolve_source_marketplace(*args)
    assert got == expected, f"{args} -> {got}, expected {expected}"
print("resolver: ok")

# --- 2. no marketplace = no Keepa call --------------------------------
for value in (None, "", "UK", "US", "  "):
    assert VerdictService.check_source_marketplace("B0TEST", value) is None, value
print("skip-when-not-eu: ok (no Keepa call spent)")

# --- 3. blocker classification, against fake Keepa payloads -----------
def fake(csv_amazon, csv_fba, buy_box, availability, bb_is_amazon=None, bb_is_fba=None):
    """Minimal Keepa-shaped product dict. csv index 18 is buy box (triples)."""
    csv = [[] for _ in range(19)]
    csv[0] = [1, int(csv_amazon * 100)] if csv_amazon else []
    csv[10] = [1, int(csv_fba * 100)] if csv_fba else []
    csv[18] = [1, int(buy_box * 100), 0] if buy_box else []
    stats = {"offerCountFBA": 2}
    if bb_is_amazon is not None:
        stats["buyBoxIsAmazon"] = bb_is_amazon
    if bb_is_fba is not None:
        stats["buyBoxIsFBA"] = bb_is_fba
    return {"asin": "B0TEST", "csv": csv, "stats": stats, "availabilityAmazon": availability}


scenarios = [
    ("Amazon holds box, in stock", fake(24.99, 26.0, 24.99, 0, True, False), None),
    ("FBA seller holds box, Amazon absent", fake(0, 26.0, 26.0, -1, False, True), None),
    ("FBM holds box, Amazon absent", fake(0, 30.0, 27.5, -1, False, False), "fbm_only"),
    ("FBM holds box, Amazon out of stock", fake(0, 30.0, 27.5, 1, False, False), "amazon_oos"),
    ("No buy box at all", fake(0, 0, 0, -1, False, False), "no_buy_box"),
    ("No authoritative fields, price matches FBA", fake(0, 26.0, 26.0, -1), None),
    ("No authoritative fields, no match", fake(0, 30.0, 27.5, -1), "unverified"),
]

import app.services.verdict_service as vs


class FakeProductService:
    payload = None

    def __init__(self):
        pass

    def get_products(self, *a, **kw):
        return [FakeProductService.payload] if FakeProductService.payload else []


real = vs.ProductService
vs.ProductService = FakeProductService
try:
    for label, payload, expected in scenarios:
        FakeProductService.payload = payload
        result = VerdictService.check_source_marketplace("B0TEST", "DE")
        assert result["blocker"] == expected, f"{label}: got {result['blocker']!r}, expected {expected!r}"
        print(f"  {label}: blocker={result['blocker']!r} holder={result['buy_box_holder']!r}")

    FakeProductService.payload = None
    assert VerdictService.check_source_marketplace("B0TEST", "DE")["blocker"] == "not_listed"
    print("  Not listed on that marketplace: blocker='not_listed'")
finally:
    vs.ProductService = real
print("blockers: ok")

# --- 4. the panel renders ---------------------------------------------
FULL_KEYS = [
    "deep_dive", "sp_api_live_check", "asin", "title", "brand", "category_name", "ean",
    "source_check", "keepa_estimate_profit", "keepa_estimate_roi", "keepa_estimate_margin",
    "keepa_estimate_profit_90d", "keepa_estimate_roi_90d", "keepa_estimate_profit_peak",
    "keepa_estimate_roi_peak", "fee_breakdown", "viable_days_90d", "price_avg_30d",
    "price_avg_90d", "price_avg_180d", "buy_box_percentage", "amazon_buy_box_percentage",
    "offers_now", "offers_90d_avg", "offers_fba_present", "offer_count_fba", "offer_trend",
    "monthly_sales", "monthly_sales_as_of", "sales_drops_30d", "rating", "review_count",
    "is_amazon_on_listing", "buy_box_now", "price_drop_count_30d", "price_drop_count_90d",
    "price_drop_count_180d", "price_min_ever", "price_min_90d", "price_max", "price_max_90d",
    "is_out_of_stock", "similar_rejections",
]
metrics = dict.fromkeys(FULL_KEYS)
metrics.update(dict.fromkeys([
    "price_avg_30d", "price_avg_90d", "price_avg_180d", "buy_box_percentage",
    "amazon_buy_box_percentage", "offers_now", "offers_90d_avg", "sales_drops_30d",
    "price_drop_count_30d", "price_drop_count_90d", "price_drop_count_180d",
    "price_min_ever", "price_min_90d", "price_max", "price_max_90d",
], 10))
metrics.update({
    "asin": "B0TEST", "title": "Test", "brand": "TestBrand", "category_name": "Toys", "ean": "",
    "buy_box_now": 39.99, "keepa_estimate_profit": 8.0, "keepa_estimate_roi": 30.0,
    "keepa_estimate_margin": 20.0, "monthly_sales": 40, "rating": 4.5, "review_count": 120,
    "viable_days_90d": None, "source_check": {
        "marketplace": "DE", "buy_box_holder": "fbm", "buyable": False, "blocker": "fbm_only",
        "note": "Amazon DE buy box is held by a merchant-fulfilled (FBM) seller -- not buyable for A2A.",
        "amazon_on_listing": False, "amazon_in_stock": False, "offer_count_fba": 2,
        "buy_box_price": 24.99, "buy_box_price_gbp": 21.4, "currency": "EUR",
    },
})
html = templates.env.get_template("_verdict_metrics.html").render(
    verdict="AVOID", rationale="- Test", metrics=metrics,
    va_ground_truth=False, va_profit=None, va_roi=None,
)
assert "Source check -- Amazon DE" in html
assert "vr-banner-avoid" in html
assert "merchant-fulfilled" in html
print("panel renders: ok")

metrics["source_check"] = None
html = templates.env.get_template("_verdict_metrics.html").render(
    verdict="BUY", rationale="- Test", metrics=metrics,
    va_ground_truth=False, va_profit=None, va_roi=None,
)
assert "Source check" not in html
print("panel omitted for OA leads: ok")

# --- 5. the form renders ----------------------------------------------
r = client.get("/verdict")
assert r.status_code == 200, r.status_code
assert 'name="source_marketplace"' in r.text
assert r.text.count('name="source_marketplace"') == 2, "expected the single AND bulk forms to have it"
print("verdict page: ok")

print("\nALL PASS")
