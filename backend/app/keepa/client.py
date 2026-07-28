import os
from pathlib import Path

import keepa
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)

# Cached singleton -- avoids creating a brand new Keepa client (and
# firing a fresh update_status() API call) on every single request.
# BrandScanService creates a new ProductFinder/ProductService on every
# scan, and each used to call get_keepa_client() from scratch --
# meaning every scan was sending 2 extra status-check requests to
# Keepa beyond the actual data calls. Reusing one client cuts that
# down to a single status check per app run, not one per request.
_cached_api = None


def get_keepa_client():
    global _cached_api

    if _cached_api is not None:
        return _cached_api

    api_key = os.getenv("KEEPA_API_KEY")

    if not api_key:
        raise RuntimeError("KEEPA_API_KEY not found")

    _cached_api = keepa.Keepa(api_key)

    # tokens_left stays at its uninitialized 0 until we explicitly ask
    # Keepa for the real account status -- without this, a fresh
    # client silently reports 0 tokens regardless of real balance.
    # Only needed ONCE here, since regular query()/product_finder()
    # calls already update tokens_left themselves from each response.
    _cached_api.update_status()

    return _cached_api