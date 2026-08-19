import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from app.database.database import SessionLocal
from app.database.models import NotifiedOpportunity

# Explicit path to backend/.env (this file is at backend/app/services/
# discord_notifier.py, so parents[2] is backend) -- deliberately NOT
# relying on load_dotenv()'s default "search upward from the current
# working directory" behaviour. That default only works if Atlas is
# always launched with `backend` as the working directory, which
# isn't guaranteed -- the self-healing "Atlas Server" scheduled task
# (start_atlas_loop.bat, set up to auto-restart Atlas) can run with a
# different working directory than a manual restart.bat invocation
# does, and the first real test of this (DISCORD_WEBHOOK_URL showing
# up as unset despite the .env file existing) is consistent with
# exactly that. An explicit path removes the ambiguity entirely,
# regardless of which process/working-directory started this run.
#
# This intentionally does NOT try to reuse whichever .env the Keepa/
# Anthropic clients load (those hardcode inconsistent parents[N]
# values between themselves -- app/keepa/client.py and app/services/
# serpapi_client.py use parents[3], app/services/anthropic_client.py
# uses parents[2] despite being the same folder depth as
# serpapi_client.py) -- this is its own, separate, unambiguous file.
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_PATH)

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Optional -- if set, pings this specific Discord user in every
# notification (Server Settings -> right-click your name -> Copy User
# ID, with Developer Mode on). If unset, the message still posts to
# the channel, just without an @ mention.
MENTION_USER_ID = os.getenv("DISCORD_MENTION_USER_ID")

# How long before the SAME ASIN is allowed to ping again. Without
# this, a product that stays a genuine BUY for days would re-ping on
# every single scan queue tick (as often as once a minute -- see
# ScanQueueService) forever. 7 days mirrors the same "give it a
# meaningful amount of time before treating it as new news again"
# instinct as ScanQueueService's old RECYCLE_COOLDOWN_HOURS.
RENOTIFY_COOLDOWN_DAYS = 7

DISCORD_COLOR_BUY = 0x22C55E
DISCORD_COLOR_CONSIDER = 0xF59E0B

# Real Amazon retail domain per Keepa marketplace code -- used to link
# straight to both the UK listing and the EU source listing (same ASIN,
# different marketplace) from the Discord message. UK is the odd one
# out (co.uk, not just "uk").
AMAZON_DOMAIN_BY_MARKETPLACE = {
    "UK": "amazon.co.uk",
    "DE": "amazon.de",
    "FR": "amazon.fr",
    "ES": "amazon.es",
    "IT": "amazon.it",
}


def _amazon_url(asin: str, marketplace: str) -> str | None:
    domain = AMAZON_DOMAIN_BY_MARKETPLACE.get((marketplace or "").upper())
    return f"https://www.{domain}/dp/{asin}" if domain and asin else None


def _format_source_label(raw_label: str) -> str:
    """
    Turns the raw `brand` string BrandScanService.scan() was called
    with into a human label for "where did this come from" -- every
    SellerWatchService call site labels its scans starting with
    "competitor" (see run_check's `f"competitor:{nickname}"`,
    rescan_unscored's "competitor-rescan", reclassify's
    "competitor-reclassify"), while every other caller (Discovery,
    Watchlist, Replen, Scan Queue, an uploaded list) passes a real
    brand/label name straight through -- this is the same
    discriminator ReviewQueueService effectively uses (source: "scan"
    vs "competitor"), just recovered from the label rather than
    threaded through as a separate field.
    """
    if not raw_label:
        return "Unknown"

    lower = raw_label.lower()

    if lower.startswith("competitor:"):
        return f"Competitor watch ({raw_label.split(':', 1)[1]})"

    if lower.startswith("competitor"):
        return "Competitor watch"

    return f"Scan ({raw_label})"


class DiscordNotifier:
    """
    Posts a Discord message (via an incoming webhook -- no bot process
    or OAuth needed) whenever a scan finds a notable opportunity, same
    "worth a look" bar as the Review Queue (ProductRepository.
    is_notable). Entirely optional: every method here no-ops quietly
    if DISCORD_WEBHOOK_URL isn't set in .env, so this is safe to leave
    wired into the scan pipeline whether or not it's configured.
    """

    @staticmethod
    def is_configured() -> bool:
        return bool(WEBHOOK_URL)

    @staticmethod
    def _should_notify(asin: str) -> bool:
        db = SessionLocal()

        try:
            row = db.get(NotifiedOpportunity, asin)

            if row is None:
                return True

            # last_notified_at is written via datetime.now(timezone.utc)
            # but SQLite hands it back timezone-NAIVE (same quirk noted
            # elsewhere in this codebase, e.g. ScanQueueService) -- the
            # cutoff is stripped to naive too so the comparison isn't
            # comparing aware to naive.
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=RENOTIFY_COOLDOWN_DAYS)
            ).replace(tzinfo=None)

            return row.last_notified_at <= cutoff

        finally:
            db.close()

    @staticmethod
    def _mark_notified(asin: str):
        db = SessionLocal()

        try:
            row = db.get(NotifiedOpportunity, asin)
            now = datetime.now(timezone.utc)

            if row is None:
                db.add(NotifiedOpportunity(asin=asin, first_notified_at=now, last_notified_at=now))
            else:
                row.last_notified_at = now

            db.commit()

        finally:
            db.close()

    @staticmethod
    def _build_embed(product_dict: dict, report_dict: dict, source_label: str) -> dict:
        """
        Shared field layout for both a real notification and the
        /debug/discord/test sample -- kept in one place so the test
        ping is guaranteed to actually preview what a real one looks
        like, rather than a hand-maintained approximation that could
        silently drift out of sync.
        """
        asin = product_dict.get("asin") or ""
        recommendation = report_dict.get("recommendation") or ""
        profit = product_dict.get("profit") or 0.0
        roi = product_dict.get("roi") or 0.0
        monthly_sales = product_dict.get("monthly_sales") or 0

        uk_url = _amazon_url(asin, "UK")

        source_marketplace = product_dict.get("best_source_marketplace") or ""
        source_cost = product_dict.get("best_source_cost_gbp") or 0.0
        source_url = _amazon_url(asin, source_marketplace)

        return {
            "title": (product_dict.get("title") or asin)[:250],
            "url": uk_url,
            "color": DISCORD_COLOR_BUY if recommendation == "BUY" else DISCORD_COLOR_CONSIDER,
            "fields": [
                {"name": "Recommendation", "value": recommendation or "-", "inline": True},
                {"name": "Score", "value": str(report_dict.get("score") or 0), "inline": True},
                {"name": "Found via", "value": _format_source_label(source_label), "inline": True},
                {"name": "Profit / ROI", "value": f"£{profit:.2f} ({roi:.1f}%)", "inline": True},
                {
                    "name": "Source listing",
                    "value": (
                        f"[{source_marketplace} £{source_cost:.2f}]({source_url})"
                        if source_url else
                        f"{source_marketplace or '-'} £{source_cost:.2f}"
                    ),
                    "inline": True,
                },
                {
                    "name": "Monthly sales",
                    "value": str(monthly_sales) if monthly_sales else "Unconfirmed",
                    "inline": True,
                },
                {
                    "name": "Amazon UK",
                    "value": f"[View listing]({uk_url})" if uk_url else "-",
                    "inline": True,
                },
                {
                    # Triple-backtick fenced block (not inline code/plain
                    # text) so Discord desktop shows a one-click "Copy"
                    # button on hover, and mobile's long-press selects the
                    # whole block cleanly in one go -- much easier than
                    # dragging a selection across plain field text, which
                    # is what made pasting this into SAS fiddly before.
                    # Not inline (full-width) so the block/button has room
                    # to render properly rather than being squeezed into a
                    # third of the row alongside two other fields.
                    "name": "ASIN",
                    "value": f"```\n{asin}\n```",
                    "inline": False,
                },
                {
                    # SellerAmp SAS has no supported "open with this ASIN
                    # pre-filled" URL -- confirmed via SellerAmp's own
                    # developer docs, which only describe triggering the
                    # SAS Chrome extension from an embedded link on a
                    # third-party page (integration token + data-asin
                    # attribute), not a standalone shareable URL. The
                    # honest working equivalent: the Amazon UK link above
                    # opens the real listing, and if the SAS Chrome
                    # extension is installed it activates on that page
                    # automatically, already reading this exact ASIN.
                    "name": "SellerAmp SAS",
                    "value": "No direct link exists -- open Amazon UK above with the SAS extension installed to run it for this ASIN.",
                    "inline": False,
                },
            ],
        }

    @staticmethod
    def notify_opportunity(product_dict: dict, report_dict: dict, source_label: str = "") -> bool:
        """
        Posts one Discord message for a notable opportunity. Returns
        True if a message was actually sent, False for every no-op
        case (not configured, already notified within the cooldown, or
        the post itself failed) -- callers don't need to check the
        return value, this never raises, matching every other
        best-effort side-channel in this codebase (e.g.
        ProductRepository.save_opportunity's own try/except).

        source_label: the raw `brand` string BrandScanService.scan()
        was called with -- see _format_source_label for how this
        becomes "Scan (philips)" vs "Competitor watch (SellerName)" in
        the message. Optional (defaults to "Unknown") so this doesn't
        become a breaking change for any caller that doesn't pass it.
        """
        asin = product_dict.get("asin") or ""

        if not asin or not WEBHOOK_URL:
            return False

        if not DiscordNotifier._should_notify(asin):
            return False

        embed = DiscordNotifier._build_embed(product_dict, report_dict, source_label)

        payload = {
            "content": f"<@{MENTION_USER_ID}>" if MENTION_USER_ID else None,
            "embeds": [embed],
        }

        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            # A failed Discord post must never break the scan pipeline
            # it's called from -- same defensive stance as every other
            # best-effort integration in this codebase.
            print(f"Discord notification failed for {asin}: {exc}")
            return False

        DiscordNotifier._mark_notified(asin)
        return True

    @staticmethod
    def send_test_ping() -> dict:
        """
        Sends one sample message so the webhook/mention setup can be
        confirmed working without waiting for a real scan to find a
        genuine opportunity. Deliberately bypasses the ASIN cooldown
        (_should_notify) entirely -- this isn't a real opportunity, so
        there's nothing to de-duplicate against, and gating a manual
        "does this even work" test behind the same 7-day cooldown as
        real finds would make it useless for repeated testing.

        Returns a small dict describing what happened rather than a
        plain bool -- see /debug/test-discord in main.py, which shows
        this directly in the browser so a misconfigured URL/ID is
        immediately visible instead of just "nothing happened".
        """
        if not WEBHOOK_URL:
            file_exists = ENV_PATH.exists()
            return {
                "sent": False,
                "expected_env_file": str(ENV_PATH),
                "env_file_found": file_exists,
                "reason": (
                    f"DISCORD_WEBHOOK_URL is not set. Looked for it in {ENV_PATH} -- "
                    + (
                        "that file exists, so check DISCORD_WEBHOOK_URL is spelled exactly "
                        "right inside it (no quotes needed, one line, no extra spaces)."
                        if file_exists else
                        "that file does NOT exist at this exact path. A common cause: "
                        "Notepad silently saved it as \".env.txt\" instead of \".env\" -- "
                        "check the file's actual name (turn on 'File name extensions' in "
                        "File Explorer's View tab if you can't tell)."
                    )
                ),
            }

        # Built through the exact same field layout a real notification
        # uses (see _build_embed) with made-up sample numbers -- a fake
        # ASIN ("TEST0000000") so the Amazon UK / source listing links
        # are present but obviously won't resolve to a real product.
        sample_product = {
            "asin": "TEST0000000",
            "title": "Atlas test ping -- sample product",
            "profit": 12.34,
            "roi": 28.5,
            "monthly_sales": 150,
            "best_source_marketplace": "DE",
            "best_source_cost_gbp": 19.99,
        }
        sample_report = {"recommendation": "BUY", "score": 92}

        embed = DiscordNotifier._build_embed(sample_product, sample_report, "test")
        embed["description"] = "If you can see this in Discord, your webhook (and mention, if you set one) is working. The links below use a fake ASIN and won't resolve to a real product."

        payload = {
            "content": f"<@{MENTION_USER_ID}>" if MENTION_USER_ID else None,
            "embeds": [embed],
        }

        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"sent": False, "reason": f"Discord rejected the request: {exc}"}

        return {
            "sent": True,
            "mentioned": bool(MENTION_USER_ID),
            "reason": "Posted -- check the Discord channel your webhook points to.",
        }
