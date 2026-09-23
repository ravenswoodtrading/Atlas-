"""
Single discoverability page for everything Atlas automates (2026-09-14,
Tamara: "I couldn't find this whole thing about the upload and had to
search through chats... I think I have this organised badly" -- the fix
isn't better chat organisation, it's Atlas being self-documenting about
its own automation, so finding out "does Atlas already do X" never
requires remembering which chat built it.

AUTOMATIONS covers every background scheduler in main.py (name must
match the `name` each scheduler passes to ActivityLog.mark_tick).
ON_DEMAND covers processes that need a human trigger, not a timer --
these never show a "next due" countdown, only "last run" plus, where
one exists, the reminder cadence a Command Centre banner already
enforces.
"""
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.activity_log import ActivityLog
from app.routes.dashboard import _format_ago
from app.services.scan_queue_service import SCAN_QUEUE_DAILY_TOKEN_CEILING, SCAN_QUEUE_TOKEN_RESERVE

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

AUTOMATIONS = [
    dict(name="scan_queue", label="Scan Queue", link="/scan-queue",
         description=f"Rotates through tracked brands, searching for new sourcing opportunities. Capped at {SCAN_QUEUE_DAILY_TOKEN_CEILING:,} Keepa tokens per rolling 24 hours (SCAN_QUEUE_DAILY_TOKEN_CEILING in .env), and never spends the last {SCAN_QUEUE_TOKEN_RESERVE:,} tokens of the balance (SCAN_QUEUE_TOKEN_RESERVE), so every other job always has tokens -- when the cap is hit it says so here and resumes as older spend ages out."),
    dict(name="seller_watch", label="Competitor Watch", link="/competitors/sellers",
         description="Checks tracked competitor sellers for new listings, 3 times a day (whenever the last successful pass is 8 hours old -- a restart doesn't reset it). Highest priority for tokens: brand scans and the safety nets stand aside while it waits for its turn."),
    dict(name="weekly_recheck", label="Watchlist safety net", link="/review-queue",
         description="Rechecks stale Watchlist and Review Queue items and the Revisit Pool so nothing silently goes stale. Runs once a day (only when the last successful run is 20+ hours old, so a restart doesn't re-run it). Revisit pool: 5 a day. (Replen has its own job below.)"),
    dict(name="replen_a2a", label="Replen (EU A2A)", link="/replen",
         description="Reads the Buy Sheet for every EU A2A purchase, refreshes FBA stock and 30-day sales from Amazon (free), and re-prices about 25% of the list on Keepa each day, so every ASIN is priced roughly every 4 days. Each item gets a verdict (Buy more / Well stocked / Wait: cost up / ...) and you get a Discord ping when one flips to Buy more. Runs once a day, and never just because the server restarted."),
    dict(name="price_sweep", label="Free price sweep (monitoring)", link="/price-sweep",
         description="Every 30 minutes checks the next ~20 products (Watchlist, near misses, UK-recovery candidates and every Replen item) against live UK and EU prices from Amazon -- free, no Keepa tokens -- and works out whether each would be a lead at today's prices, in either direction (UK up, EU down, or both). Covers everything about twice a day. MONITORING ONLY: nothing alerts or changes a verdict yet; /price-sweep shows what it found next to what Keepa's last scan said so its accuracy can be judged first."),
    dict(name="signals", label="Signals", link="/signals/queries",
         description="Rechecks saved Keepa searches for stock-outs and price-ceiling changes every 2 hours. EU price-drop searches are Run now only (see below)."),
    dict(name="lead_analysis", label="Lead analysis", link="/review-queue?view=va_to_review",
         description="Runs Keepa + AI verdict analysis on newly queued VA/manual leads."),
    dict(name="va_lead_sheet_sync", label="VA Lead Sheet sync", link="/review-queue?view=va_to_review",
         description="Pulls new leads and Client Rating decisions from the VA Lead Sheet, Mon-Fri 7am-8pm. Won't tick outside that window -- not a bug."),
    dict(name="eu_a2a_freshness", label="EU A2A freshness recheck", link="/review-queue",
         description="Rechecks EU Amazon-to-Amazon opportunities so pricing evidence doesn't go stale."),
    dict(name="inventory_cleanup", label="Out of Stock Cleanup", link="/inventory-cleanup",
         description="Detects FBA SKUs that appear permanently out of stock. Detection only -- deleting a listing always needs your approval on that page."),
    dict(name="amazon_listing_upload", label="Amazon Listing Upload", link="/automation/amazon-listings",
         description="Submits Buy Sheet rows marked \"Listing Uploader (Y)\" directly to Amazon at 9am and 9pm each day, and updates the sheet's flags on success.",
         run_now_url="/automation/amazon-listings/run-now"),
    dict(name="storage_fee_watch", label="Storage Fee Watch", link="/storage-fee-watch",
         description="Tracks FBA storage fee charges and long-term storage risk."),
    dict(name="restriction_sweep", label="Amazon restriction check", link="/review-queue",
         description="Every 10 minutes asks Amazon (free, no Keepa tokens) whether we're allowed to list each product waiting for review, and hides gated ones from the Review Queue and Command Centre. Brand scans and EU price-drop searches also skip gated products before spending Keepa tokens. Doesn't cover hazmat/dangerous-goods holds."),
]

ON_DEMAND = [
    dict(label="EU price-drop Keepa searches", link="/signals/queries", cadence="Manual -- Run now on Keepa Searches",
         description="Finds products whose price just dropped on DE/FR/ES/IT in a chosen category, checks UK-vs-source prices for free, then runs the survivors through the full scan. Leads land in the Review Queue; each run's funnel and token cost is listed on the page. Never automatic -- a run costs roughly 12 Keepa tokens per candidate scanned."),
    dict(label="Seller Toolkit Cost of Goods", link="/reports/uploads#stk-cogs", cadence="Weekly reminder on Command Centre",
         description="You upload STK's Cost of Goods export; Atlas fills in missing Unit Cost and hands back a file ready to reupload to STK. Never automatic -- STK only creates a CoG row once a shipment has actually gone out."),
]


@router.get("/automations")
def automations_page(request: Request):
    rows = {row["name"]: row for row in ActivityLog.scheduler_overview()}
    automations = []
    for a in AUTOMATIONS:
        row = rows.get(a["name"])
        automations.append({
            **a,
            "last_ago": _format_ago(row["last_tick_at"]) if row else None,
            "last_summary": (row["last_summary"] if row else "") or "",
        })
    return templates.TemplateResponse(request=request, name="automations.html", context={
        "automations": automations,
        "on_demand": ON_DEMAND,
    })
