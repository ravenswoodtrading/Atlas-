from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/leads-hub")
def leads_hub_page(request: Request, group: str = "manual"):
    """
    Atlas nav consolidation Phase 2 (2026-08-24), "deep" inlining per
    the user's explicit choice, built as Option B: each tab shows its
    page's real content immediately (an empty/default-state form, same
    as visiting that page directly with no query params), but actually
    running a search/upload/lookup takes you to that page's own,
    completely unchanged route/URL -- deliberately NOT rebuilt as a
    single shared-URL dispatch (Option C), which the user considered
    and passed on given the added risk across 7 already-working pages.

    This landing view is intentionally cheap -- no live Keepa/SerpApi
    calls, no DB lookups -- it only ever shows the same empty-state
    each page's own route shows before a form is submitted. Each
    group is its own tab; only "manual" (Discovery/Categories/OA
    Lookup) is built so far -- Automated and Web-Sourced land in
    later passes, per the incremental build-and-spot-check plan.
    """
    group = group if group in ("manual", "automated", "websourced") else "manual"

    return templates.TemplateResponse(
        request=request,
        name="leads_hub.html",
        context={
            "request": request,
            "group": group,
        },
    )
