from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.routes.scan_queue import build_scan_queue_context
from app.routes.signals import build_signals_context, build_signal_queries_context
from app.routes.competitors import build_competitors_context, build_competitors_sellers_context

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/leads-hub")
def leads_hub_page(request: Request, group: str = "manual"):
    """
    Atlas nav consolidation Phase 2 (2026-08-24), "deep" inlining per
    the user's explicit choice, built as Option B: each tab shows its
    page's real content immediately (an empty/default-state form for
    the Manual Search group's search-driven pages, or real live data
    for Automated's dashboard-style pages -- Scan Queue/Signals/
    Competitors don't have a meaningful "empty" state the way Discovery
    does), but actually running a search/upload/lookup, or acting on
    a row, takes you to that page's own, completely unchanged route/URL
    -- deliberately NOT rebuilt as a single shared-URL dispatch
    (Option C), which the user considered and passed on given the
    added risk across 7 already-working pages.

    The Automated group's context comes from the exact same build_*_
    context() helpers each standalone route now calls (extracted from
    those routes specifically so this hub can't silently drift out of
    sync with them) -- default/no-filter state each time, same as
    visiting the page fresh with no query params.
    """
    group = group if group in ("manual", "automated", "websourced") else "manual"

    context = {"request": request, "group": group}

    if group == "automated":
        context["scan_queue"] = build_scan_queue_context()
        context["signals"] = build_signals_context()
        context["signal_queries"] = build_signal_queries_context()
        context["competitors"] = build_competitors_context()
        context["competitors_sellers"] = build_competitors_sellers_context()

    return templates.TemplateResponse(request=request, name="leads_hub.html", context=context)
