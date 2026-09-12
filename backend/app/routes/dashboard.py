from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
import time

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.product_repository import ProductRepository
from app.services.product_service import ProductService
from app.services.scan_queue_service import ScanQueueService
from app.services.seller_watch_service import SellerWatchService
from app.services.review_queue_service import (
    ReviewQueueService, QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
)
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.activity_log import ActivityLog
from app.services.google_sheets_client import open_sheet
# Quick Reject reason list (UI redesign pass, 2026-09-03) -- the Buy Now
# table's row-level quick-reject dropdown on this page uses the exact
# same six reasons as /review-queue's own rows, imported rather than
# duplicated so the two can't drift apart.
from app.routes.review_queue import QUICK_REJECT_REASONS, VIEW_FILTERS, _matches_workflow_view, _requires_human_review

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Friendly labels + a stable display order for the "What's automated
# next" panel -- keyed on the scheduler `name` each background loop in
# main.py passes to ActivityLog.mark_tick. Every known scheduler is
# always shown, even before its first tick since this feature shipped
# (2026-08-19), so the panel itself never looks broken/empty on a
# fresh restart -- it just shows "never yet" until the first tick.
AUTOMATION_ORDER = ["scan_queue", "seller_watch", "weekly_recheck", "signals", "lead_analysis"]
AUTOMATION_LABELS = {
    "scan_queue": "Scan Queue (brand searches)",
    "seller_watch": "Competitor Watch",
    "weekly_recheck": "Watchlist/Replen weekly safety net",
    "signals": "Signals (stock-out/ceiling recheck)",
    "lead_analysis": "Lead analysis (VA/manual queue)",
}

# Same idea for the "Today's activity" tiles -- fixed order/labels so
# the row doesn't reshuffle day to day depending on which activity
# types happened to log first.
ACTIVITY_ORDER = [
    "brand_search", "competitor_check", "replen_check",
    "watchlist_check", "review_queue_recheck", "revisit_pool", "signal_check", "oa_discovery_run", "lead_analysis",
]
ACTIVITY_LABELS = {
    "brand_search": "Brand searches",
    "competitor_check": "Competitor checks",
    "replen_check": "Replen checks",
    "watchlist_check": "Watchlist rechecks",
    # Added 2026-09-04 -- ReviewQueueService.recheck_stale_items, wired
    # into main.py's existing daily _weekly_recheck_scheduler.
    "review_queue_recheck": "Review Queue rechecks",
    # Added 2026-09-04 -- Opportunity Engine 2.0 Phase 3A's targeted
    # Revisit Pool (RevisitPoolService), wired into the SAME daily
    # scheduler tick as review_queue_recheck just above.
    "revisit_pool": "Revisit Pool",
    "signal_check": "Signal checks",
    "oa_discovery_run": "OA Discovery runs",
    "lead_analysis": "Leads analyzed",
}


# How many items to show in each Home page preview list before "View
# all" -- the Command Centre is a summary, not the full queue (that's
# what /review-queue itself is for).
COMMAND_CENTRE_PREVIEW_SIZE = 5

# Command Centre purchasing snapshot. These are the user's existing
# authoritative purchasing workbook tabs. Atlas only ever reads them.
PURCHASING_SHEET_URL = "https://docs.google.com/spreadsheets/d/1WcO8SQ6cQEmoVG-GUmAJwBde1AIgp4aBg9pUBJ62UA0/edit?pli=1&gid=1374354957#gid=1374354957"
PURCHASING_CACHE_TTL = timedelta(hours=1)
_purchasing_snapshot_cache = {"created_at": None, "value": None}


def _as_number(value):
    """Read a currency/quantity cell without relying on sheet formatting."""
    cleaned = re.sub(r"[^0-9.()-]", "", str(value or "")).replace("(", "-").replace(")", "")
    if not cleaned or cleaned == "-":
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return Decimal("0")


def _as_date(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %y", "%d %b %Y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _monthly_daily_tracker_total(rows, month_start):
    """Sum the date/value pairs from either official Daily Tracker tab."""
    total = Decimal("0")
    for row in rows[1:]:
        for index in range(1, len(row) - 1):
            entry_date = _as_date(row[index])
            if entry_date and entry_date.year == month_start.year and entry_date.month == month_start.month:
                total += _as_number(row[index + 1])
    return total


def _purchasing_snapshot():
    """Return a cached, read-only month-to-date view of the official workbook."""
    now = datetime.now()
    cached_at = _purchasing_snapshot_cache["created_at"]
    if cached_at and now - cached_at < PURCHASING_CACHE_TTL:
        return _purchasing_snapshot_cache["value"]

    month_start = date.today().replace(day=1)
    snapshot = {
        "available": False,
        "month_label": month_start.strftime("%B %Y"),
        "updated_at": now.strftime("%H:%M"),
        "spend": Decimal("0"),
        "profit": Decimal("0"),
        "expected_profit": Decimal("0"),
        "units": 0,
        "purchase_lines": 0,
        "error": None,
    }
    try:
        workbook = open_sheet(PURCHASING_SHEET_URL)
        spend_rows = workbook.worksheet("Daily Tracker - Spend").get_all_values()
        profit_rows = workbook.worksheet("Daily Tracker - Profit").get_all_values()
        buy_rows = workbook.worksheet("Buy Sheet").get_all_values()

        snapshot["spend"] = _monthly_daily_tracker_total(spend_rows, month_start)
        snapshot["profit"] = _monthly_daily_tracker_total(profit_rows, month_start)

        # Buy Sheet has a second export block further right with another
        # "Date Ordered" header. Keep the first (purchasing) block.
        headers = {}
        for index, header in enumerate(buy_rows[0]):
            headers.setdefault(header.strip().lower(), index)
        date_index = headers.get("date ordered")
        quantity_index = headers.get("quantity")
        expected_profit_index = headers.get("profit (total)")
        if date_index is None or quantity_index is None or expected_profit_index is None:
            raise ValueError("The Buy Sheet no longer has its expected Date Ordered, Quantity and Profit (Total) columns.")

        for row in buy_rows[1:]:
            if len(row) <= date_index:
                continue
            ordered_on = _as_date(row[date_index])
            if not ordered_on or ordered_on.year != month_start.year or ordered_on.month != month_start.month:
                continue
            snapshot["purchase_lines"] += 1
            snapshot["units"] += int(_as_number(row[quantity_index])) if len(row) > quantity_index else 0
            snapshot["expected_profit"] += _as_number(row[expected_profit_index]) if len(row) > expected_profit_index else 0

        snapshot["available"] = True
    except Exception as exc:
        snapshot["error"] = str(exc)

    _purchasing_snapshot_cache.update({"created_at": now, "value": snapshot})
    return snapshot


def _command_centre():
    """
    Home page Command Centre (Command Centre UI build, 2026-09-03;
    refined 2026-09-03 pass 2 -- Buy Now made the dominant section, a
    Borderline preview added, and a small sub-breakdown line added to
    each of the three secondary cards). ONE call to
    ReviewQueueService.list_queue_items(), reused for the four workflow
    counts, the sub-breakdowns, and all four preview lists, so the
    Dashboard doesn't pay for the full unified-queue computation
    (hundreds of rows across four scan filters plus both competitor
    queries) more than once on the same page load. No new dedup/
    priority logic here -- workflow membership comes from the same
    helpers used by the Review Queue, including conflict/attention
    verification tasks and the exclusion of monitor-only items.
    """
    cached = globals().get('_COMMAND_CENTRE_CACHE')
    now = time.monotonic()
    if cached and now - cached[0] < 15:
        return cached[1]

    items = [item for item in ReviewQueueService.list_queue_items() if _requires_human_review(item)]

    counts = {
        QUEUE_PRIORITY_BUY_NOW: 0, QUEUE_PRIORITY_VA_TO_REVIEW: 0,
        QUEUE_PRIORITY_BORDERLINE: 0, QUEUE_PRIORITY_NEEDS_ATTENTION: 0,
        "OA_INVESTIGATE": 0,
    }
    for view, priority in VIEW_FILTERS.items():
        counts[priority] = sum(bool(_matches_workflow_view(item, view)) for item in items)

    def preview(view_name):
        view = next(key for key, priority in VIEW_FILTERS.items() if priority == view_name)
        return [i for i in items if _matches_workflow_view(i, view)][:COMMAND_CENTRE_PREVIEW_SIZE]

    # Sub-breakdown lines for the three secondary cards -- a quick "what
    # kind of thing is actually in here" without opening the full view.
    # These are informative tallies, not a strict partition: an item can
    # land in more than one bucket (e.g. a Borderline item sourced from
    # both Competitor and VA counts in both), so bucket totals are not
    # guaranteed to sum to the card's own headline count.
    va_items = [i for i in items if _matches_workflow_view(i, "va_to_review")]
    va_breakdown = {
        "strong_buy": sum(1 for i in va_items if QUEUE_PRIORITY_BUY_NOW in i["views"]),
        "borderline": sum(
            1 for i in va_items
            if QUEUE_PRIORITY_BORDERLINE in i["views"] and QUEUE_PRIORITY_BUY_NOW not in i["views"]
        ),
        "need_more_info": sum(
            1 for i in va_items
            if QUEUE_PRIORITY_BUY_NOW not in i["views"] and QUEUE_PRIORITY_BORDERLINE not in i["views"]
        ),
    }

    borderline_items = [i for i in items if _matches_workflow_view(i, "borderline")]
    borderline_breakdown = {
        "from_competitor": sum(1 for i in borderline_items if "competitor" in i["sources"]),
        "from_va": sum(1 for i in borderline_items if "lead" in i["sources"]),
        "from_scan": sum(1 for i in borderline_items if "scan" in i["sources"]),
    }

    attention_items = [i for i in items if QUEUE_PRIORITY_NEEDS_ATTENTION in i["views"]]
    attention_breakdown = {
        "historical_not_buyable": sum(
            1 for i in attention_items
            if i.get("historical_sourcing_evidence") and QUEUE_PRIORITY_BUY_NOW not in i["views"]
        ),
        "conflict": sum(1 for i in attention_items if i.get("conflict")),
    }
    attention_breakdown["other"] = max(
        0, len(attention_items) - attention_breakdown["historical_not_buyable"] - attention_breakdown["conflict"]
    )

    result = {
        "unique_total": len(items),
        "counts": counts,
        # Only Buy Now still gets an item-level preview -- the other
        # three used to (see dashboard.html's own comment, 2026-09-05)
        # but that duplicated the count+breakdown cards below with a
        # second full listing, which is what led Tamara to work leads
        # from Command Centre instead of Review Queue.
        "buy_now_preview": preview(QUEUE_PRIORITY_BUY_NOW),
        "va_breakdown": va_breakdown,
        "borderline_breakdown": borderline_breakdown,
        "attention_breakdown": attention_breakdown,
    }
    globals()['_COMMAND_CENTRE_CACHE'] = (now, result)
    return result


def _lead_counts():
    """
    Leads with status="analyzed" awaiting a human decision, split
    sheet-sourced (arriving unattended, most likely to pile up -- see
    spec section 7) from manual /verdict checks.
    """
    db = SessionLocal()
    try:
        analyzed = db.query(Lead).filter(Lead.status == "analyzed").all()
        sheet_count = sum(1 for lead in analyzed if lead.source == "sheet")
        manual_count = sum(1 for lead in analyzed if lead.source == "manual")
        return sheet_count, manual_count
    finally:
        db.close()


def _stk_cogs_due():
    """
    Weekly Command Centre reminder (2026-09-12, Tamara) for the Seller
    Toolkit Cost-of-Goods fill process -- STK only creates a CoG row once
    a shipment has actually gone out, so Atlas can't trigger this itself
    on a schedule; this just nudges Tamara to run it herself via
    /reports/uploads if it's been a week (or never) since the last run.
    """
    from app.services.stk_cogs_service import latest_run
    run = latest_run()
    if not run:
        return True
    return datetime.now(timezone.utc) - run["run_at"].replace(tzinfo=timezone.utc) >= timedelta(days=7)


def _keepa_tokens_remaining():
    """
    Current Keepa token balance, read from the cached client's own
    running total -- costs NOTHING extra (no fresh API call), since
    every real query anywhere in the app already keeps this updated
    as a side effect (see app/keepa/client.py). Can be stale if
    nothing's queried Keepa yet this run (falls back to whatever
    update_status() reported at client creation), same caveat as
    every other "tokens_remaining" display already in Atlas
    (Discovery, Categories, Replen) -- informational, not a live
    guarantee. None if Keepa isn't configured at all, so the
    Dashboard degrades gracefully rather than 500ing.
    """
    try:
        return ProductService().api.tokens_left
    except Exception:
        return None


def _scan_queue_health():
    item_count = len(ScanQueueService.list_items())
    return ScanQueueService.estimate_cadence(item_count)


def _new_competitor_detections_today():
    """
    Sum of every sourcing-tag bucket (including not-yet-classified)
    detected in the last 24h, across every tracked seller -- same
    counts/filters the Competitors page's own tab badges use
    (get_detection_counts), just totalled rather than split by tag,
    since the Dashboard just needs "is anything new happening" at a
    glance.
    """
    counts = SellerWatchService.get_detection_counts(since_days=1)
    return sum(counts.values())


def _best_gated_opportunity():
    """
    The single strongest scored opportunity currently sitting on a
    gated brand (already sorted best-ROI-first by
    list_gated_opportunities) -- surfaces the case for pursuing
    ungating without requiring a separate visit to the Gated
    Opportunities page to notice one exists. None if there aren't any.
    """
    opportunities = ProductRepository.list_gated_opportunities()
    return {
        "count": len(opportunities),
        "best": opportunities[0] if opportunities else None,
    }


def _oa_discovery_activity():
    """
    OA Source Discovery activity for the Dashboard (2026-08-19) -- this
    module shipped the same day as this redesign pass and was
    otherwise invisible unless you remembered to visit /oa-discovery.
    Surfaces how many priced candidates are sitting there needing a
    human call, plus a warning if the last run had to stop early to
    protect the SerpApi quota (mirrors the warning banner already on
    the page itself, see SERPAPI_QUOTA_SAFETY_BUFFER).
    """
    quota = OaSourceDiscoveryService.latest_run_quota_status()
    return {
        "awaiting_review": OaSourceDiscoveryService.count_awaiting_review(),
        "quota_stopped": bool(quota and quota["quota_stopped"]),
        "searches_left": quota["searches_left"] if quota else None,
    }


def _format_ago(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = max(0, (datetime.now(timezone.utc) - dt).total_seconds())

    if seconds < 90:
        return "just now"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"

    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m ago" if mins else f"{hours}h ago"

    return f"{hours // 24}d ago"


def _format_due_in(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "due now"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"in ~{minutes} min"

    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"in ~{hours}h {mins}m" if mins else f"in ~{hours}h"

    return f"in ~{hours // 24}d"


def _automation_overview():
    """
    One row per known background scheduler (see AUTOMATION_ORDER/
    AUTOMATION_LABELS above) -- when it last actually ran and roughly
    when it's next due, for the Dashboard's "What's automated next"
    panel. See ActivityLog.scheduler_overview/mark_tick for how
    last/next are tracked; a scheduler that hasn't ticked yet since
    this feature shipped shows as "never yet" rather than being
    omitted, so the panel never looks broken/empty on a fresh restart.
    """
    rows = {row["name"]: row for row in ActivityLog.scheduler_overview()}

    overview = []
    for name in AUTOMATION_ORDER:
        row = rows.get(name)
        # Sub-2-minute intervals (Lead Analysis is 30s) are
        # effectively continuous -- a literal "next due" countdown for
        # those is noise, not signal.
        continuous = bool(row) and row["interval_seconds"] <= 120

        overview.append({
            "label": AUTOMATION_LABELS[name],
            "last_ago": _format_ago(row["last_tick_at"]) if row else None,
            "last_summary": (row["last_summary"] if row else "") or "",
            "next_due": (
                None if continuous
                else _format_due_in(row["next_due_at"]) if row else None
            ),
            "continuous": continuous,
        })

    return overview


def _todays_activity():
    counts = ActivityLog.counts_today()
    return [
        {"label": ACTIVITY_LABELS[key], "count": counts.get(key, 0)}
        for key in ACTIVITY_ORDER
    ]


def _greeting() -> str:
    """Time-of-day greeting for the Command Centre header (section 3) --
    no user-name field exists anywhere in Atlas's data model, so this
    deliberately stays generic ("Good evening.") rather than inventing one."""
    hour = datetime.now(timezone.utc).hour
    if hour < 12:
        return "Good morning."
    if hour < 18:
        return "Good afternoon."
    return "Good evening."


@router.get("/")
def dashboard(request: Request):
    from app.services.scan_schedule_service import pending_reviews
    stats = ProductRepository.get_summary_stats()
    sheet_leads_waiting, manual_leads_waiting = _lead_counts()
    purchasing_snapshot = _purchasing_snapshot()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "stats": stats,
            "scan_tier_review_count": len(pending_reviews()),
            "stk_cogs_due": _stk_cogs_due(),
            "sheet_leads_waiting": sheet_leads_waiting,
            "manual_leads_waiting": manual_leads_waiting,
            "keepa_tokens_remaining": _keepa_tokens_remaining(),
            "scan_queue_health": _scan_queue_health(),
            "new_competitor_detections": _new_competitor_detections_today(),
            "gated_opportunity": _best_gated_opportunity(),
            "command_centre": _command_centre(),
            "greeting": _greeting(),
            "last_updated": datetime.now().strftime("%H:%M"),
            "quick_reject_reasons": QUICK_REJECT_REASONS,
            "purchasing_sheet_url": PURCHASING_SHEET_URL,
            "purchasing_snapshot": purchasing_snapshot,
            # review_queue_summary/consider_summary intentionally no
            # longer computed here (2026-09-03, Command Centre UI
            # build) -- they were only ever used by the three alert
            # banners now removed from dashboard.html (superseded by
            # command_centre above), and each call re-runs a full
            # list_leads()-equivalent scan (~2s) -- paying for that
            # twice on every Home page load for banners that no longer
            # render would be pure waste. Nothing else in the app reads
            # these two context keys.
            "oa_discovery": _oa_discovery_activity(),
            "new_signal_matches": ProductRepository.count_new_signal_matches(),
            "today_activity": _todays_activity(),
            "automation_overview": _automation_overview(),
        }
    )
