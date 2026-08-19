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

    for excluded in EXCLUDED_DOMAINS:
        if domain == excluded or domain.endswith("." + excluded):
            return domain, "excluded"

    return domain, "candidate"
