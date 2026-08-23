from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.product_repository import ProductRepository
from app.services.product_service import ProductService
from app.services.scan_queue_service import ScanQueueService
from app.services.seller_watch_service import SellerWatchService
from app.services.review_queue_service import ReviewQueueService
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.activity_log import ActivityLog

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
    "watchlist_check", "signal_check", "oa_discovery_run", "lead_analysis",
]
ACTIVITY_LABELS = {
    "brand_search": "Brand searches",
    "competitor_check": "Competitor checks",
    "replen_check": "Replen checks",
    "watchlist_check": "Watchlist rechecks",
    "signal_check": "Signal checks",
    "oa_discovery_run": "OA Discovery runs",
    "lead_analysis": "Leads analyzed",
}


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


@router.get("/")
def dashboard(request: Request):
    stats = ProductRepository.get_summary_stats()
    sheet_leads_waiting, manual_leads_waiting = _lead_counts()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "stats": stats,
            "sheet_leads_waiting": sheet_leads_waiting,
            "manual_leads_waiting": manual_leads_waiting,
            "keepa_tokens_remaining": _keepa_tokens_remaining(),
            "scan_queue_health": _scan_queue_health(),
            "new_competitor_detections": _new_competitor_detections_today(),
            "gated_opportunity": _best_gated_opportunity(),
            "review_queue_summary": ReviewQueueService.count_summary(),
            "consider_summary": ReviewQueueService.consider_summary(),
            "oa_discovery": _oa_discovery_activity(),
            "new_signal_matches": ProductRepository.count_new_signal_matches(),
            "today_activity": _todays_activity(),
            "automation_overview": _automation_overview(),
        }
    )