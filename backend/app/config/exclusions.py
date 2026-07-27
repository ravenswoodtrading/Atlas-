"""
Products, categories, and brand/category combos to skip BEFORE
spending Keepa tokens on the 4 EU marketplace lookups.

Edit the lists below directly -- no other code needs to change.
Categories are Keepa's numeric rootCategory IDs (you'll see these in
scan results, e.g. "category": "340831031"). Run a broad scan and use
the category IDs you see next to product titles to figure out which
ones to add here.

WHY THIS MATTERS: every ASIN normally costs 5x Keepa lookups (UK +
DE + FR + ES + IT). Filtering here happens right after the UK lookup,
BEFORE the 4 EU lookups -- so excluding a category/ASIN/brand here
saves 80% of the tokens that ASIN would have cost.
"""

# Keepa rootCategory IDs you can never actually source from EU Amazon
# (e.g. mains-powered/plug items -- different plug standards, voltage,
# or certification requirements block cross-border resale).
EXCLUDED_CATEGORIES = {
    "340831031",  # monitors -- confirmed dead end for A2A sourcing
    "213077031",  # lighting (bulbs, wake-up lamps, smart bulbs) -- mains-powered
    "79903031",   # surge protectors / power strips / extension cables -- mains-powered
}

# Specific ASINs to always skip, regardless of category or brand.
EXCLUDED_ASINS = set()
# Add ASINs like: EXCLUDED_ASINS.add("B0EXAMPLE1")

# (brand, category) pairs where you're gated / can't get approval to
# sell, even though other categories from the same brand are fine.
# Brand is matched case-insensitively.
GATED_BRAND_CATEGORIES = set()
# Add pairs like: GATED_BRAND_CATEGORIES.add(("philips", "123456789"))


# Human-readable category NAMES (as seen in a Keepa CSV export's
# "Categories: Root" column, e.g. "Lighting") -- used when checking
# against known_products (imported from a CSV), which has names
# rather than Keepa's numeric rootCategory IDs. Matched case-
# insensitively. Confirmed so far: "Lighting" matches the same
# category as numeric ID 213077031 above.
EXCLUDED_CATEGORY_NAMES = {
    "lighting",
}
# Add more as you confirm them from CSV imports, e.g.:
# EXCLUDED_CATEGORY_NAMES.add("surge protectors")


def is_excluded(asin: str, brand: str, category: str) -> bool:
    if asin in EXCLUDED_ASINS:
        return True

    if category in EXCLUDED_CATEGORIES:
        return True

    if (brand.lower(), category) in GATED_BRAND_CATEGORIES:
        return True

    return False


def is_excluded_by_name(asin: str, brand: str, category_name: str) -> bool:
    """
    Same idea as is_excluded(), but for the known_products table,
    which stores human-readable category names from a CSV import
    rather than Keepa's numeric rootCategory IDs.
    """
    if asin in EXCLUDED_ASINS:
        return True

    if category_name.lower() in EXCLUDED_CATEGORY_NAMES:
        return True

    if (brand.lower(), category_name.lower()) in GATED_BRAND_CATEGORIES:
        return True

    return False
