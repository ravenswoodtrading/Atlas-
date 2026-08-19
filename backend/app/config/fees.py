"""
Referral fee and VAT tables used by FeeEngine to turn a gross Keepa
price into a real net profit/ROI number.

WHY THIS EXISTS: FeeEngine used to assume a flat 15% referral fee for
every category and did no VAT accounting at all. For a VAT-registered
seller (charges 20% UK output VAT on sales, reclaims input VAT on
EU-sourced costs and on Amazon's own fees under the UK reverse-charge
rules) that overstates cost on low-referral-rate categories (e.g.
Electronics ~8%) and ignores a ~1/6 chunk of "revenue" that's actually
VAT owed to HMRC, not profit.

REFERRAL_RATE_BY_CATEGORY_NAME rates below are from general knowledge
of Amazon's publicly published UK referral fee schedule -- they are
NOT live-verified against Seller Central's current fee calculator.
Cross-check your actual top brands/categories via the /categories page
(CategorySurveyService) before trusting this for real purchasing
decisions, same discipline as app/config/exclusions.py.

Matched case-insensitively against the category name CategorySurveyService's
get_category_names() resolves for a product's Product.category (Keepa's
numeric rootCategory ID). A name that isn't a key here silently falls
back to DEFAULT_REFERRAL_RATE -- not an error, just means "we don't
have a specific rate for this category yet."

TIERED CATEGORIES (REFERRAL_FEE_TIERS_BY_CATEGORY_NAME): several
categories charge a LOWER rate up to a price threshold and a HIGHER
rate above it, applied PORTION BY PORTION like a tax bracket -- e.g.
"8% up to £20, 15% above" charges 8% on the first £20 of the sale
price and 15% only on the remainder, not 15% on the whole price. Using
the old flat rate for these (previously the single highest tier, e.g.
15% for Home & Kitchen) UNDER-stated profit on anything priced above
the threshold -- the opposite direction of a since-reported ROI
mismatch on a high-ticket item, which is why that mismatch needs more
than this fix alone (see FeeEngine's docstring / the referral rate
shown in the Products page tooltip for that specific ASIN).

Built by cross-referencing several 2026 third-party Amazon-fee sites,
NOT Seller Central directly (its fee tables are behind login) -- they
disagreed with each other on Grocery's exact threshold, and both gave
Jewellery/Watches GBP thresholds suspiciously identical to Amazon's
published USD figures (a common copy-paste error on these sites), so
Jewellery/Watches are deliberately left FLAT below rather than risk a
fabricated number. Verify any tiered category that matters to your
sourcing against Seller Central's own fee calculator before trusting
it for real purchasing decisions.
"""

DEFAULT_REFERRAL_RATE = 0.15

# Amazon charges this minimum per unit regardless of price/rate --
# confirmed by multiple 2026 sources. Matters most for very cheap
# items where a pure percentage fee would round to nearly nothing.
MINIMUM_REFERRAL_FEE_GBP = 0.25

# Flat per-unit prep-center fee (paid to a UK-VAT-registered prepper),
# net of its VAT -- same "VAT-neutral, net cash effect ignored" logic
# already applied to fba_fee below (the 20% charged on top is
# reclaimed as input VAT under Standard VAT accounting, so the real
# economic cost is just the net 45p, not the 54p gross invoice).
PREP_FEE_GBP = 0.45

REFERRAL_RATE_BY_CATEGORY_NAME = {
    "electronics": 0.08,
    "computers & accessories": 0.07,
    "computers": 0.07,
    "camera & photo": 0.08,
    "video games": 0.08,
    "toys & games": 0.15,
    "toys": 0.15,
    "home & kitchen": 0.15,
    "kitchen & home": 0.15,
    "sports & outdoors": 0.15,
    "beauty": 0.15,
    "health & personal care": 0.15,
    "diy & tools": 0.15,
    "diy & tools & garden": 0.15,
    "pet supplies": 0.15,
    "baby products": 0.15,
    "baby": 0.15,
    "grocery": 0.15,
    "musical instruments": 0.15,
    "office products": 0.15,
    "clothing, shoes & jewellery": 0.15,
    "clothing": 0.15,
    "shoes, handbags & wallets": 0.15,
    "watches": 0.16,
    "jewellery": 0.20,
    "automotive": 0.15,
    "garden & outdoors": 0.15,
    "lighting": 0.15,
}

# category name -> ascending list of (band_ceiling_gbp, rate), where
# ceiling=None marks the final/unbounded band. A category listed here
# OVERRIDES its flat entry above -- see FeeEngine._referral_fee for
# the portion-by-portion maths, and this module's docstring for the
# confidence caveats (Grocery lower-confidence; Jewellery/Watches
# deliberately NOT tiered here).
REFERRAL_FEE_TIERS_BY_CATEGORY_NAME = {
    "home & kitchen": [(20.0, 0.08), (None, 0.15)],
    "kitchen & home": [(20.0, 0.08), (None, 0.15)],
    "beauty": [(10.0, 0.08), (None, 0.15)],
    "health & personal care": [(10.0, 0.08), (None, 0.15)],
    "baby products": [(10.0, 0.08), (None, 0.15)],
    "baby": [(10.0, 0.08), (None, 0.15)],
    "grocery": [(10.0, 0.05), (None, 0.08)],
    # Deliberately NOT "clothing, shoes & jewellery" (that combined
    # category name could include real jewellery items, which should
    # never get clothing's lower band) -- only the plain "clothing" key.
    "clothing": [(15.0, 0.05), (20.0, 0.10), (None, 0.15)],
}

# UK VAT (Value Added Tax) -- applied to extract net (ex-VAT) revenue
# from Keepa's gross buy_box price, since Amazon UK B2C listings
# display VAT-inclusive prices to consumers.
UK_VAT_STANDARD_RATE = 0.20

# Category names that are zero-rated for UK VAT purposes (no VAT is
# actually embedded in the displayed price for these) -- small starter
# set, verify/extend via the /categories page same as exclusions.py.
UK_VAT_ZERO_RATED_CATEGORY_NAMES = {
    "books",
    "children's clothing",
    "children's shoes",
}

# EU VAT standard rates by sourcing marketplace -- applied to extract
# net (ex-VAT) cost from Keepa's gross EU buy_box price, since the
# user reclaims this input VAT (Standard VAT accounting, not Flat
# Rate). Standard rates only; a small number of EU-sourced goods
# qualify for a reduced rate, deliberately ignored for v1.
EU_VAT_RATE_BY_MARKETPLACE = {
    "DE": 0.19,
    "FR": 0.20,
    "IT": 0.22,
    "ES": 0.21,
}
