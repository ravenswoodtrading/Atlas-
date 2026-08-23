import os
import time

import requests
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
EU_ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"

# Marketplace IDs for this account's UK + EU (Unified European Account)
# marketplaces -- confirmed live via GET /sellers/v1/marketplaceParticipations
# (2026-08-21), not guessed from Amazon's docs. Also confirmed live
# that getItemOffers returns REAL competitive pricing for third-party
# ASINs on this account (not just the seller's own listings) across
# all 5 of these -- see brand_scan_service.py Step 4's own comment for
# how that was validated before being wired into the real pipeline.
MARKETPLACE_IDS = {
    "UK": "A1F83G8C2ARO7P",
    "DE": "A1PA6795UKMFR9",
    "FR": "A13V1IB3VIYZZH",
    "ES": "A1RKKUPIHCS9HS",
    "IT": "APJ6JRA9NG5V4",
}

# Refresh the cached LWA access token this many seconds before its
# real expiry (Amazon issues them with a 3600s lifetime) -- avoids a
# request firing with a token that expires mid-flight.
TOKEN_REFRESH_MARGIN_SECONDS = 120

# getItemOffers' rate limit is tight (a handful of requests/second
# with a small burst). On a 429, back off and retry rather than
# treating a rate-limit hit as "no data" -- that would wrongly make
# BrandScanService's caller fall through to Keepa thinking SP-API
# genuinely had nothing, when it just hadn't been asked yet.
# MAX_RETRIES bounds the worst case so one stubborn rate-limited ASIN
# can't stall an entire scan.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Proactive pacing between requests -- checking one ASIN can mean up
# to 4 sequential calls (DE/FR/ES/IT) before either finding a viable
# price or giving up, and a scan can check many ASINs, so this adds up
# to real wall-clock time even though it costs no Keepa tokens. A
# fixed minimum gap keeps this well under the documented rate limit
# proactively, rather than firing as fast as possible and eating 429
# retries reactively -- steadier latency, and doesn't waste the retry
# budget on self-inflicted rate-limiting.
MIN_REQUEST_INTERVAL_SECONDS = 0.25


class SPAPIClient:
    """
    Minimal Amazon SP-API client -- LWA (Login with Amazon) token
    exchange/caching plus getItemOffers, the one endpoint
    BrandScanService's Stage 2 free-first-check (see brand_scan_service.py
    Step 4) actually needs.

    Deliberately narrow -- this is not a general SP-API SDK, just the
    one call Atlas needs. No AWS SigV4 signing: Amazon phased that
    requirement out for Pricing/Catalog in favour of LWA-only auth, so
    a bearer access token in the x-amz-access-token header is enough.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = None
        self._access_token_expires_at = 0.0
        self._last_request_at = 0.0

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get_access_token(self) -> str:
        now = time.monotonic()

        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()

        self._access_token = body["access_token"]
        self._access_token_expires_at = (
            now + body.get("expires_in", 3600) - TOKEN_REFRESH_MARGIN_SECONDS
        )

        return self._access_token

    def get_item_offers(self, asin: str, marketplace: str, item_condition: str = "New") -> dict | None:
        """
        Returns {"status": str, "price": float_or_None, "offer_count": int_or_None}
        for the CURRENT Buy Box price on `marketplace` (one of
        MARKETPLACE_IDS' keys), or None if the call itself failed to
        produce a usable answer at all (network error, exhausted
        retries on rate-limiting, an invalid ASIN for that
        marketplace, unconfigured marketplace).

        That None-vs-real-dict distinction matters exactly like
        ProductFinder.find_brand's None-vs-[] one: a failed call must
        never be read as "confirmed no source here", or a caller would
        wrongly skip a Keepa check it still genuinely needs to make.
        price is None (inside a real dict) whenever there's no
        genuinely buyable offer right now (out of stock, FBM-only, or
        just not sold on this marketplace) -- that IS real information,
        unlike a failed call.
        """
        marketplace_id = MARKETPLACE_IDS.get(marketplace)
        if not marketplace_id:
            return None

        for attempt in range(MAX_RETRIES):
            try:
                access_token = self._get_access_token()
            except Exception as exc:
                print(f"SP-API token refresh failed: {exc}")
                return None

            self._pace()

            try:
                resp = requests.get(
                    f"{EU_ENDPOINT}/products/pricing/v0/items/{asin}/offers",
                    headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                    params={"MarketplaceId": marketplace_id, "ItemCondition": item_condition},
                    timeout=15,
                )
            except Exception as exc:
                print(f"SP-API getItemOffers request failed for {asin}/{marketplace}: {exc}")
                return None

            if resp.status_code == 429:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if resp.status_code != 200:
                # Includes a real 400 "invalid ASIN for marketplace" --
                # a genuine "not sold here" answer, but still returned
                # as None (not a fabricated status/price dict) so a
                # future auth/config regression can't silently masquerade
                # as normal-looking empty data.
                return None

            payload = resp.json().get("payload", {})
            buy_box_prices = payload.get("Summary", {}).get("BuyBoxPrices", [])
            price = buy_box_prices[0]["LandedPrice"]["Amount"] if buy_box_prices else None

            return {
                "status": payload.get("status"),
                "price": price,
                "offer_count": payload.get("Summary", {}).get("TotalOfferCount"),
            }

        # Exhausted retries, still rate-limited.
        return None


_cached_client = None
_credentials_missing_logged = False


def get_sp_api_client():
    """
    Cached singleton, same convention as app/keepa/client.py's
    get_keepa_client() -- avoids re-doing the LWA token exchange on
    every call. Returns None (never raises) if SP_API_* isn't fully
    configured in .env -- SP-API is an OPTIONAL cost-saving layer on
    top of Keepa (see brand_scan_service.py Step 4), not a hard
    dependency, so a missing/incomplete credential set makes Atlas
    fall back to Keepa-only behaviour rather than crash.
    """
    global _cached_client, _credentials_missing_logged

    if _cached_client is not None:
        return _cached_client

    client_id = os.getenv("SP_API_CLIENT_ID")
    client_secret = os.getenv("SP_API_CLIENT_SECRET")
    refresh_token = os.getenv("SP_API_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        if not _credentials_missing_logged:
            print("SP-API credentials not configured -- skipping SP-API pre-checks, Keepa-only.")
            _credentials_missing_logged = True
        return None

    _cached_client = SPAPIClient(client_id, client_secret, refresh_token)
    return _cached_client
