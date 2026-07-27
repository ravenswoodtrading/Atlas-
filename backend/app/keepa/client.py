import os
from pathlib import Path

import keepa
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(env_path)


def get_keepa_client():
    api_key = os.getenv("KEEPA_API_KEY")

    if not api_key:
        raise RuntimeError("KEEPA_API_KEY not found")

    api = keepa.Keepa(api_key)

    # tokens_left stays at its uninitialized 0 until we explicitly ask
    # Keepa for the real account status -- without this, every fresh
    # client silently reports 0 tokens regardless of real balance.
    api.update_status()

    return api