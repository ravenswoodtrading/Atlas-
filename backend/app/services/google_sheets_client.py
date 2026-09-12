"""
Google Sheets client for the OA lead VA workflow, 2026-09-05.

Uses an OAuth "Desktop app" client (NOT a service account -- Tamara's
Google org has iam.disableServiceAccountKeyCreation enforced, which
blocks downloadable service account keys). This authenticates AS
Tamara's own Google account via a one-time interactive browser login
(run_first_time_authorization.py), then caches a refresh token locally
so every later call is silent -- no repeated logins. Because it
authenticates as the user, it can read/write any Sheet she already has
edit access to, with no separate "share with a service account email"
step needed.

Credentials, both gitignored, never committed:
  - google_oauth_client_secret.json: the OAuth CLIENT's own identity
    (client_id/client_secret) -- not a per-user secret, but still kept
    out of git like every other credential in this codebase.
  - google_oauth_token.json: the cached refresh token from Tamara's own
    one-time login. Deleting this file forces a fresh login next run.
"""
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import gspread

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
CLIENT_SECRET_PATH = os.path.join(_BASE_DIR, "google_oauth_client_secret.json")
TOKEN_PATH = os.path.join(_BASE_DIR, "google_oauth_token.json")


def get_credentials(interactive: bool = False) -> Credentials:
    """
    Loads the cached token if present and valid, refreshing it silently
    if it's just expired. `interactive=True` (only ever passed by the
    one-time authorization script) opens a real browser window for the
    first-ever login. Every other caller gets interactive=False, so a
    missing/unrefreshable token fails loudly rather than ever trying to
    pop a browser window from inside an unattended script.
    """
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            return creds
        except RefreshError:
            # Refresh token itself expired/revoked (Google-side, e.g.
            # 6 months unused, or access was revoked) -- not just an
            # expired access token. Real incident, 2026-09-12: this used
            # to propagate straight out of get_credentials, which meant
            # even run_first_time_authorization.py (interactive=True)
            # could never reach the interactive login below -- it hit
            # this same crash before ever getting the chance to open a
            # browser window. Falling through instead lets a bad token
            # be replaced by a fresh interactive login in one run,
            # rather than needing the stale token file manually moved
            # aside first.
            if not interactive:
                raise RuntimeError(
                    "Google Sheets authorization has expired or been revoked. Run "
                    "run_first_time_authorization.py to log in again."
                )
            creds = None

    if not interactive:
        raise RuntimeError(
            "No valid Google Sheets authorization found. Run "
            "run_first_time_authorization.py once to log in interactively."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def get_client() -> gspread.Client:
    return gspread.authorize(get_credentials(interactive=False))


def open_sheet(sheet_url_or_id: str):
    client = get_client()
    if sheet_url_or_id.startswith("http"):
        return client.open_by_url(sheet_url_or_id)
    return client.open_by_key(sheet_url_or_id)
