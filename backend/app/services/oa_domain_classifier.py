import re
from urllib.parse import urlparse

# NOT exhaustive by design -- per the user's own MVP spec: "don't rely
# solely on a fixed retailer list because the purpose of this system
# is to discover retailers we don't already know about." This only
# rules out domains that are NEVER a genuine standalone UK retail
# source: Amazon itself, eBay/other marketplaces, price-comparison
# aggregators, and pure review/content sites -- the four excluded
# categories the spec names explicitly. Everything else is treated as
# a real candidate, however obscure, which is the whole point.
EXCLUDED_DOMAINS = {
    # Amazon, every marketplace -- never a genuine independent source
    "amazon.co.uk", "amazon.com", "amazon.de", "amazon.fr", "amazon.it", "amazon.es",
    "amazon.ca", "amazon.nl", "amazon.pl", "amazon.se",
    # eBay / other open marketplaces -- not a single accountable retailer
    "ebay.co.uk", "ebay.com", "etsy.com", "wish.com", "onbuy.com", "shpock.com",
    "vinted.co.uk", "gumtree.com",
    # Search engines themselves, when a result link resolves back to one
    "google.com", "google.co.uk", "bing.com",
    # Price comparison / aggregator sites -- list a price but aren't
    # who you'd actually buy from
    "pricerunner.com", "kelkoo.co.uk", "shopping.google.com", "idealo.co.uk",
    "pricespy.co.uk", "trolley.co.uk", "compare.co.uk", "google.com/shopping",
    "shopmania.co.uk", "shopzilla.co.uk",
    # Review / content / reference sites -- informational, not a
    # purchase source
    "trustpilot.com", "reddit.com", "youtube.com", "wikipedia.org",
    "which.co.uk", "reviews.io", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "pinterest.com",
}

# Country-code TLDs that mean "not a UK retail source" (2026-08-28).
# Atlas's OA pipeline exists to find somewhere in the UK to BUY stock
# for a UK Amazon listing -- an EU/US storefront isn't that, whatever
# its price says, once import duty, VAT handling and delivery are
# real. Added after a live run auto-matched allegro.pl and
# elcorteingles.es at "ean" tier and presented them as sources.
#
# A BLOCKLIST of foreign ccTLDs rather than an allowlist of .co.uk
# deliberately: plenty of genuine UK retailers sit on a bare .com
# (and the whole point of this module, per the original MVP spec, is
# "discover retailers we don't already know about"), so an allowlist
# would quietly throw away real finds. The known cost of doing it this
# way is that a foreign retailer on a .com -- e.g. pclocura.com, also
# seen in that same run -- still gets through; that's left to the
# human check on the results page rather than solved by guessing at
# nationality from a domain string.
NON_UK_TLDS = {
    ".pl", ".es", ".de", ".fr", ".it", ".nl", ".be", ".pt", ".at", ".ch",
    ".se", ".no", ".dk", ".fi", ".cz", ".sk", ".hu", ".ro", ".gr", ".bg",
    ".ie", ".us", ".ca", ".au", ".nz", ".cn", ".jp", ".in", ".ru", ".tr",
}

# Marketplaces and reseller types that are never a viable OA source,
# matched on SerpApi's free-text `source` NAME rather than a domain
# (see classify_shopping_source for why a name is all we get there).
# Lowercased substrings -- matched loosely on purpose, since Google
# renders the same seller as "eBay", "eBay - cheapest_electrical" and
# "Amazon.co.uk - Amazon.co.uk-Seller" depending on the listing.
#
# The used-goods names are here for a DIFFERENT reason from the
# marketplaces: CeX, Cash Generator and Music Magpie are perfectly
# real accountable UK retailers, but they sell USED stock, and every
# figure Atlas computes downstream (FeeEngine, OpportunityEngine, the
# Amazon buy box being matched against) assumes a New listing. Sourcing
# used stock against a New-condition profit model is a silent
# mispricing, not a bargain -- the £199 used TomTom in run 3 being the
# case in point.
EXCLUDED_SOURCE_NAMES = {
    # Marketplaces -- not a single accountable retailer
    "amazon", "ebay", "onbuy", "etsy", "wish", "aliexpress", "temu",
    "alibaba", "vinted", "gumtree", "shpock", "bonanza", "fruugo",
    # Used / refurbished -- wrong condition for a New-listing model
    "cex", "cash generator", "music magpie", "musicmagpie", "back market",
    "backmarket", "webuy", "gamestop", "cash converters", "ziffit",
    # Import concierges / grey-import resellers -- UK-facing storefronts
    # reselling imported stock at import-inflated prices, with no real
    # trade terms behind them
    "big apple buddy", "u-buy", "ubuy", "etoren", "greatecno",
}

# The same names, as domains, for the Brave path -- a grey importer on
# a bare .com slips past NON_UK_TLDS by construction (see that
# constant's own note), so the ones actually seen doing it in live runs
# are named here. This list is a STOPGAP and is expected to stay
# incomplete: enumerating every foreign storefront on a .com is not
# winnable by blocklist. The structural fix is sourcing from a known
# set of UK retailers rather than open web search -- see the Awin
# retailer-feed work, whose whole premise is replacing this guesswork.
EXCLUDED_GREY_IMPORT_DOMAINS = {
    "etoren.com", "greatecno.com", "u-buy.co.uk", "ubuy.co.uk",
    "bigapplebuddy.com",
}


def classify_domain(url: str) -> tuple:
    """
    Returns (domain, category) where domain is the bare registrable
    host (www. stripped) and category is "excluded" (see
    EXCLUDED_DOMAINS above) or "candidate" -- anything else, i.e. a
    genuine unknown-until-now UK retailer candidate. Malformed/empty
    URLs come back as ("", "excluded") so they're silently dropped
    rather than crashing the caller.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return "", "excluded"

    if not netloc:
        return "", "excluded"

    domain = re.sub(r"^www\d*\.", "", netloc)

    for excluded in EXCLUDED_DOMAINS | EXCLUDED_GREY_IMPORT_DOMAINS:
        if domain == excluded or domain.endswith("." + excluded):
            return domain, "excluded"

    if is_non_uk_domain(domain):
        return domain, "excluded"

    return domain, "candidate"


def is_non_uk_domain(domain: str) -> bool:
    """
    True for a domain on a foreign ccTLD (see NON_UK_TLDS). Checked
    against the FULL domain's suffix, so ".co.uk" is never mistaken
    for a match on some other list entry, and a UK subdomain of a
    foreign parent (which doesn't make it a UK retailer) still fails.
    """
    domain = (domain or "").lower().rstrip(".")

    if not domain:
        return False

    return any(domain.endswith(tld) for tld in NON_UK_TLDS)


def classify_shopping_source(source: str) -> tuple:
    """
    The `classify_domain` equivalent for a Google Shopping result,
    which does NOT give us a retailer URL to parse (2026-08-28).

    CONFIRMED against real stored results: SerpApi's `product_link` is
    always a google.co.uk/search?ibp=oshop... URL, never the retailer's
    own page -- so classify_domain(product_link) would resolve every
    single shopping result to "google.co.uk" and exclude the lot. The
    only retailer identity available is the free-text `source` display
    name ("Argos", "u-buy.co.uk", "eBay - cheapest_electrical",
    "Amazon.co.uk - Amazon.co.uk-Seller"), so this classifies THAT.

    Returns (cleaned_name, category) with the same two categories
    classify_domain uses, so callers can treat both paths alike.

    Until this existed the SerpApi path applied no source filtering at
    all -- run 3 auto-priced an ASIN against Amazon.co.uk itself, and
    promoted an eBay listing straight into the Review Queue, because
    only the Brave fallback path ever went through classify_domain.
    """
    name = (source or "").strip()

    if not name:
        return "", "excluded"

    # Google appends the specific seller after a dash on marketplace
    # listings ("eBay - cheapest_electrical") -- the part before it is
    # the actual storefront being matched.
    primary = name.split(" - ")[0].strip()
    haystack = primary.lower()

    for excluded in EXCLUDED_SOURCE_NAMES:
        # Word-ish boundary check so "cex" doesn't match "Cexpress" but
        # does match "CeX", "CeX.co.uk" and "cex uk".
        if re.search(rf"(^|[^a-z0-9]){re.escape(excluded)}([^a-z0-9]|$)", haystack):
            return name, "excluded"

    # Some sources ARE bare domains ("u-buy.co.uk", "thomann.co.uk") --
    # apply the ccTLD rule to those too.
    if is_non_uk_domain(haystack):
        return name, "excluded"

    return name, "candidate"
