"""
Smoke test for the EU A2A import-duty cap (EU_A2A_DUTY_CAP_GBP, added
2026-08-29) -- run with `python test_product_mapper_duty_cap.py` (same
plain-script style as the other test_*.py files here, no pytest).

Tamara's instruction: "we have to limit EU A2A leads for under the
import duty" -- ProductMapper.from_keepa_multi now excludes any EU
marketplace priced above EU_A2A_DUTY_CAP_GBP (converted to GBP) from
ever being considered a source, exactly like the existing FBM
exclusion -- not just passed over for a cheaper option, but not even
recorded into its raw *_cost field, so it can't surface anywhere
downstream as a real, buyable source.

Real ProductMapper/KeepaParser/CurrencyService run unmodified here --
only the three KeepaParser methods from_keepa_multi's EU loop actually
calls (buy_box_now, buy_box_is_amazon_fulfilled, buy_box_min_90d) are
monkeypatched, keyed off a `_price`/`_fba` marker in each fake
eu_product dict, so a real (opaque) Keepa CSV blob never has to be
hand-built. Same technique already used to verify the FBM exclusion
fix. CurrencyService.to_gbp's real EUR->GBP conversion runs for real --
it falls back to its own hardcoded 0.84 rate with no network access
(this sandbox has none), which is fine here since these assertions
don't depend on the live rate, only on being consistently either side
of the £175 cap.
"""
from app.services.product_mapper import ProductMapper, EU_A2A_DUTY_CAP_GBP
from app.services.currency_service import CurrencyService
from app.keepa.parser import KeepaParser

FX_RATE = CurrencyService._get_eur_to_gbp_rate()  # whatever this run actually uses (live or fallback)

FAKE = {
    "DE_cheap_fba":    {"_price": 100.0, "_fba": True},
    "DE_over_cap":     {"_price": round((EU_A2A_DUTY_CAP_GBP + 25) / FX_RATE, 2), "_fba": True},
    "FR_cheap_fba":    {"_price": 120.0, "_fba": True},
    "FR_over_cap_fbm": {"_price": round((EU_A2A_DUTY_CAP_GBP + 25) / FX_RATE, 2), "_fba": False},
    "ES_at_boundary":  {"_price": round(EU_A2A_DUTY_CAP_GBP / FX_RATE, 4), "_fba": True},
}

KeepaParser.buy_box_now = lambda self: self.product.get("_price", 0)
KeepaParser.buy_box_is_amazon_fulfilled = lambda self: self.product.get("_fba", False)
KeepaParser.buy_box_min_90d = lambda self: self.product.get("_price", 0)

UK_PRODUCT = {"asin": "B0TEST0001", "title": "Test product", "brand": "TestBrand", "rootCategory": 1}


def run(eu_products):
    return ProductMapper.from_keepa_multi(UK_PRODUCT, eu_products)


# --- 1. single EU marketplace under the cap -> wins normally, as before ---
p = run({"DE": FAKE["DE_cheap_fba"], "FR": None, "ES": None, "IT": None})
assert p.best_source_marketplace == "DE"
assert p.best_source_cost_gbp is not None
assert p.de_cost == 100.0
print("under cap: sourced normally: ok")

# --- 2. single EU marketplace over the cap -> excluded entirely, not just deprioritised ---
p = run({"DE": FAKE["DE_over_cap"], "FR": None, "ES": None, "IT": None})
assert p.best_source_marketplace == "", "over-cap DE must not be picked as a source"
assert not p.de_cost, "over-cap DE's raw cost must not be recorded either (same treatment as FBM)"
print("over cap alone: fully excluded, no source at all: ok")

# --- 3. one marketplace over cap, another under -> the under-cap one wins ---
#     even though it isn't the raw cheapest -- proves the cap gates
#     candidacy itself, not just tie-breaking between survivors.
p = run({"DE": FAKE["DE_over_cap"], "FR": FAKE["FR_cheap_fba"], "ES": None, "IT": None})
assert p.best_source_marketplace == "FR", f"expected FR to win over the excluded DE, got {p.best_source_marketplace!r}"
assert not p.de_cost
assert p.fr_cost == 120.0
print("over-cap + under-cap mixed: correct marketplace wins: ok")

# --- 4. over cap AND FBM at once -> still cleanly excluded, no double-exclusion bug ---
p = run({"DE": None, "FR": FAKE["FR_over_cap_fbm"], "ES": None, "IT": None})
assert p.best_source_marketplace == ""
print("over cap + FBM combined: still excluded: ok")

# --- 5. exactly at the cap -> allowed ("under the import duty" cap is a maximum, inclusive) ---
p = run({"DE": None, "FR": None, "ES": FAKE["ES_at_boundary"], "IT": None})
assert p.best_source_marketplace == "ES", f"exact-boundary price should be allowed, got {p.best_source_marketplace!r}"
assert round(p.best_source_cost_gbp, 2) == round(EU_A2A_DUTY_CAP_GBP, 2)
print("boundary (exactly at cap): allowed: ok")

print("\nALL PASS")
