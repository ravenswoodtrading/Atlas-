import os
from pathlib import Path

from dotenv import load_dotenv

from app.services import serpapi_client
from app.services import serper_client

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

# Which provider actually serves Google Shopping lookups. Set
# OA_SHOPPING_PROVIDER in .env to "serpapi" or "serper".
#
# WHY A SWITCH RATHER THAN JUST SWAPPING THE IMPORT (2026-08-28):
# Serper costs roughly $0.30-1.00 per 1000 searches against SerpApi's
# $15.00, which is a 15-50x difference -- but the goal is DEALS FOUND,
# not cheap searches. If SerpApi's Shopping results produce materially
# better match tiers or more promoted leads, the extra cost pays for
# itself many times over and switching would be a false economy.
#
# So this is deliberately a decision to be made ON EVIDENCE: run the
# same batch of ASINs through both providers, compare match-tier
# distribution, candidates found and leads promoted, then keep the
# winner and delete the loser. Until that comparison has actually been
# run, the default stays on the provider already in production.
DEFAULT_PROVIDER = "serpapi"

_PROVIDERS = {
    "serpapi": serpapi_client,
    "serper": serper_client,
}


def active_provider_name() -> str:
    """
    The configured provider, lowercased, falling back to
    DEFAULT_PROVIDER if unset or set to something unrecognised.

    An unrecognised value falls back LOUDLY rather than silently -- a
    typo in .env would otherwise look exactly like the chosen provider
    performing badly, which is the worst possible failure mode during an
    A/B comparison.
    """
    configured = (os.getenv("OA_SHOPPING_PROVIDER") or "").strip().lower()

    if not configured:
        return DEFAULT_PROVIDER

    if configured not in _PROVIDERS:
        print(
            f"OA_SHOPPING_PROVIDER={configured!r} is not one of "
            f"{sorted(_PROVIDERS)} -- falling back to {DEFAULT_PROVIDER}."
        )
        return DEFAULT_PROVIDER

    return configured


def _provider():
    return _PROVIDERS[active_provider_name()]


def search_uk_shopping(query: str, num: int | None = None) -> list:
    """
    Google Shopping UK results for `query` from whichever provider is
    configured. Returns the provider-independent shape both clients
    already produce:

        [{title, source, price, extracted_price, thumbnail, product_link}]

    `num` is passed through only when given, so each provider keeps its
    own DEFAULT_NUM_RESULTS rather than this module imposing one.

    Never raises -- both underlying clients return [] on any failure.
    """
    provider = _provider()

    if num is None:
        return provider.search_uk_shopping(query)

    return provider.search_uk_shopping(query, num)


def search_uk_shopping_via(provider_name: str, query: str, num: int | None = None) -> list:
    """
    Same as search_uk_shopping, but for a SPECIFIC named provider
    rather than whichever OA_SHOPPING_PROVIDER is currently configured.

    Exists for OaSourceDiscoveryService.run_batch's SerpApi quota-
    safety fallback (2026-08-29): once SerpApi's monthly reserve is
    hit, the run needs to reach for Serper specifically for its
    remaining ASINs -- prepaid credits, no monthly cliff -- regardless
    of what OA_SHOPPING_PROVIDER happens to be set to. See that
    module's "Quota safety buffer" docstring for the full behaviour.

    Deliberately raises KeyError for an unrecognised provider_name --
    unlike active_provider_name()'s "fall back loudly but keep going"
    convention, a caller here is asking for one SPECIFIC client by
    name, so a typo should fail immediately rather than silently
    resolving to something else mid-run.
    """
    provider = _PROVIDERS[provider_name]

    if num is None:
        return provider.search_uk_shopping(query)

    return provider.search_uk_shopping(query, num)


def get_account_status() -> dict:
    """
    Remaining-quota information, where the provider has such a concept.

    SerpApi returns real figures including plan_searches_left; Serper is
    prepaid credits with no monthly cliff and returns {}. Callers must
    treat a missing plan_searches_left as "unknown" and simply not
    enforce a quota buffer -- which is what
    OaSourceDiscoveryService.run_batch already does.
    """
    return _provider().get_account_status()


def cost_per_1000_usd() -> float:
    """
    Published price per 1000 searches for the active provider, for
    estimating a run's search spend. An ESTIMATE from a published rate,
    not a billed figure -- same framing as
    brave_search_client.COST_PER_1000_USD.
    """
    provider = _provider()

    # serpapi_client predates this constant; its Developer plan works
    # out at $75/month for 5,000 searches.
    return getattr(provider, "COST_PER_1000_USD", 15.0)
