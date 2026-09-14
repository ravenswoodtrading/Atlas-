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

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

AUTOMATIONS = [
    dict(name="scan_queue", label="Scan Queue", link="/scan-queue",
         description="Rotates through tracked brands, searching for new sourcing opportunities."),
    dict(name="seller_watch", label="Competitor Watch", link="/competitors/sellers",
         description="Checks tracked competitor sellers for new listings."),
    dict(name="weekly_recheck", label="Watchlist/Replen weekly safety net", link="/review-queue",
         description="Rechecks stale Review Queue items and the Revisit Pool so nothing silently goes stale."),
    dict(name="signals", label="Signals", link="/signals/queries",
         description="Rechecks tracked signal queries for stock-outs and price-ceiling changes."),
    dict(name="lead_analysis", label="Lead analysis", link="/review-queue?view=va_to_review",
         description="Runs Keepa + AI verdict analysis on newly queued VA/manual leads."),
    dict(name="va_lead_sheet_sync", label="VA Lead Sheet sync", link="/review-queue?view=va_to_review",
         description="Pulls new leads and Client Rating decisions from the VA Lead Sheet, Mon-Fri 7am-8pm. Won't tick outside that window -- not a bug."),
    dict(name="eu_a2a_freshness", label="EU A2A freshness recheck", link="/review-queue",
         description="Rechecks EU Amazon-to-Amazon opportunities so pricing evidence doesn't go stale."),
    dict(name="inventory_cleanup", label="Out of Stock Cleanup", link="/inventory-cleanup",
         description="Detects FBA SKUs that appear permanently out of stock. Detection only -- deleting a listing always needs your approval on that page."),
    dict(name="amazon_listing_upload", label="Amazon Listing Upload", link="/automation/amazon-listings",
         description="Submits Buy Sheet rows marked \"Listing Uploader (Y)\" directly to Amazon, once a day, and updates the sheet's flags on success."),
    dict(name="storage_fee_watch", label="Storage Fee Watch", link="/storage-fee-watch",
         description="Tracks FBA storage fee charges and long-term storage risk."),
]

ON_DEMAND = [
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
