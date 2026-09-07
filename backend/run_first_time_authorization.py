"""
One-time interactive Google Sheets authorization, 2026-09-05.

Run this ONCE:
    python run_first_time_authorization.py

It opens a real browser window -- log into your own Google account and
click Allow. This caches google_oauth_token.json locally so every
future script (the daily OA lead list writer/reader) runs silently,
with no further login needed. Safe to re-run any time (e.g. after
deleting google_oauth_token.json to force a fresh login, or to switch
which Google account is authorized).
"""
from app.services.google_sheets_client import get_credentials, TOKEN_PATH

if __name__ == "__main__":
    get_credentials(interactive=True)
    print(f"Authorization complete. Token cached at: {TOKEN_PATH}")
    print("You will not need to log in again for future runs.")
