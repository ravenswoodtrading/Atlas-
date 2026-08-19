"""
Products, categories, and brand/category combos to skip BEFORE
spending Keepa tokens on the 4 EU marketplace lookups.

Edit the lists below directly -- no other code needs to change.
EXCLUDED_CATEGORIES matches against a product's FULL category tree
(root all the way down to its most specific subcategory/leaf), not
just the root -- so you can add a narrow ID like "true monitors"
without also blocking everything else under "Computers &
Accessories". Keepa's categoryTree gives every level with names
already attached (see CategorySurveyService/the /categories page, or
just run a broad scan and inspect a product's categoryTree) -- add
whichever level's ID actually matches what you want to exclude.

WHY THIS MATTERS: every ASIN normally costs 5x Keepa lookups (UK +
DE + FR + ES + IT). Filtering here happens right after the UK lookup,
BEFORE the 4 EU lookups -- so excluding a category/ASIN/brand here
saves 80% of the tokens that ASIN would have cost.
"""

# Keepa category IDs you can never actually source from EU Amazon
# (e.g. mains-powered/plug items -- different plug standards, voltage,
# or certification requirements block cross-border resale). Can be a
# root category OR a narrow subcategory/leaf ID -- matched against
# EVERY level of a product's category tree, not just its root.
#
# 340831031 ("monitors") and 79903031 ("surge protectors / power
# strips / extension cables") used to be listed here as what were
# THOUGHT to be narrow categories, back when this only checked a
# product's root category -- confirmed via Keepa's category_lookup
# that they're actually the ENTIRE "Computers & Accessories" and "DIY
# & Tools" root categories, silently blocking 100% of Hansgrohe, 40%
# of Corsair, and 35% of Brother (all genuinely sourceable). Removed;
# if you want to exclude true monitors or surge-protectors
# specifically going forward, find their real (narrower) category IDs
# and add them here -- they'll now correctly leave the rest of
# Computers & Accessories / DIY & Tools untouched.
EXCLUDED_CATEGORIES = {
    "213077031",  # lighting (bulbs, wake-up lamps, smart bulbs) -- mains-powered, confirmed correctly scoped
}

# Specific ASINs to always skip, regardless of category or brand.
EXCLUDED_ASINS = set()
# Add ASINs like: EXCLUDED_ASINS.add("B0EXAMPLE1")

# (brand, category) pairs where you're gated / can't get approval to
# sell, even though other categories from the same brand are fine.
# Brand is matched case-insensitively; category can be any level of
# the product's category tree, same as EXCLUDED_CATEGORIES. Leave
# category as "" to gate the WHOLE brand regardless of category.
#
# NOTE: checked via is_gated()/is_gated_by_name() below, NOT
# is_excluded()/is_excluded_by_name() -- a gated brand behaves
# differently from a real exclusion (see GatedBrand's docstring in
# app/database/models.py): it blocks brand-search-driven scanning
# (BrandScanService.scan Step 1) but does NOT drop an incidentally-
# found ASIN, it just tags it Product.gated=True so the opportunity is
# still tracked. This static set is normally just a bootstrap/backup --
# day to day, manage gated brands from the Exclusions page (GatedBrand
# table), which is checked the same way alongside this set.
GATED_BRAND_CATEGORIES = set()
# Add pairs like: GATED_BRAND_CATEGORIES.add(("philips", "123456789"))
# Or gate a whole brand: GATED_BRAND_CATEGORIES.add(("hp", ""))


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


def is_excluded(asin: str, brand: str, category_ids, extra_excluded_category_ids: set = frozenset()) -> bool:
    """
    category_ids: every category ID a product belongs to, root through
    leaf (see BrandScanService.scan's Step 3 for how this is built
    from Keepa's categoryTree/categories fields) -- a single root
    category string still works fine too (treated as a one-item set),
    for any other caller that only has that.

    extra_excluded_category_ids: the DB-backed, user-editable
    companion to the static EXCLUDED_CATEGORIES set above (see the
    Exclusions page / ExcludedCategory) -- fetched ONCE per scan by
    the caller, not queried here, same reason EXCLUDED_ASINS' DB
    counterpart (ExcludedProduct) is fetched once by the caller rather
    than checked in here.
    """
    if isinstance(category_ids, str):
        category_ids = {category_ids} if category_ids else set()
    else:
        category_ids = set(category_ids)

    if asin in EXCLUDED_ASINS:
        return True

    if (EXCLUDED_CATEGORIES | extra_excluded_category_ids) & category_ids:
        return True

    return False


def is_excluded_by_name(asin: str, brand: str, category_name: str, extra_excluded_category_names: set = frozenset()) -> bool:
    """
    Same idea as is_excluded(), but for the known_products table,
    which stores human-readable category names from a CSV import
    rather than Keepa's numeric rootCategory IDs.

    extra_excluded_category_names: DB-backed companion to
    EXCLUDED_CATEGORY_NAMES, already lower-cased by the caller (see
    ProductRepository.get_excluded_category_names).
    """
    if asin in EXCLUDED_ASINS:
        return True

    category_name_lower = category_name.lower()

    if category_name_lower in EXCLUDED_CATEGORY_NAMES or category_name_lower in extra_excluded_category_names:
        return True

    return False


def is_gated(brand: str, category_ids, extra_gated_pairs: set = frozenset()) -> bool:
    """
    Whether `brand` (optionally scoped by `category_ids`, every
    category ID a product belongs to -- same shape is_excluded()
    takes) is currently gated -- i.e. Atlas can't get Amazon approval
    to sell it. Unlike is_excluded(), a True result here does NOT mean
    "drop this ASIN": see GatedBrand's docstring (app/database/
    models.py) for why gated brands are tracked, not thrown away, when
    found incidentally (e.g. via Competitor Watch) -- this function
    only answers "is it gated", the caller decides what to do with
    that (BrandScanService.scan Step 1 blocks brand-search scanning of
    it; Step 3 tags an incidental find rather than dropping it).

    extra_gated_pairs: DB-backed GatedBrand rows, as (brand_lower,
    category_id or "") pairs -- "" means "whole brand, any category".
    Fetched once per scan by the caller (see
    ProductRepository.get_gated_brand_pairs), same pattern as
    is_excluded()'s extra_excluded_category_ids.
    """
    if isinstance(category_ids, str):
        category_ids = {category_ids} if category_ids else set()
    else:
        category_ids = set(category_ids)

    brand_lower = brand.lower()

    for gated_brand, gated_category in (GATED_BRAND_CATEGORIES | extra_gated_pairs):
        if gated_brand != brand_lower:
            continue
        if not gated_category or gated_category in category_ids:
            return True

    return False


def is_gated_by_name(brand: str, category_name: str, extra_gated_pairs_by_name: set = frozenset()) -> bool:
    """
    Same idea as is_gated(), but for the known_products table, which
    stores human-readable category names rather than Keepa's numeric
    category IDs -- mirrors is_excluded_by_name()'s relationship to
    is_excluded().

    extra_gated_pairs_by_name: DB-backed GatedBrand rows, as
    (brand_lower, category_name_lower or "") pairs -- see
    ProductRepository.get_gated_brand_pairs_by_name().
    """
    brand_lower = brand.lower()
    category_name_lower = category_name.lower()

    for gated_brand, gated_category_name in (GATED_BRAND_CATEGORIES | extra_gated_pairs_by_name):
        if gated_brand != brand_lower:
            continue
        if not gated_category_name or gated_category_name == category_name_lower:
            return True

    return False
