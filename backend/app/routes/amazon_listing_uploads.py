from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import amazon_listing_upload_service as svc
from app.services.stale_cache import StaleCache

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# The pending-row count reads the whole Buy Sheet from Google (2-3s) -- served from here and refreshed in the background
# so the page doesn't wait for it (2026-09-21). The upload itself always reads the sheet fresh (svc.pending_rows).
_PENDING_COUNT = StaleCache(ttl_seconds=120, retry_seconds=30, name="amazon-listings-pending")


@router.get("/automation/amazon-listings")
def amazon_listings_page(request: Request):
    batches = svc.batch_history()
    for batch in batches:
        batch["failures"] = svc.batch_failures(batch["id"]) if batch["failed_count"] else []
    # None (not 0) on a Sheets read failure (e.g. the OAuth token expiring --
    # see CLAUDE.md's own operational gotcha) so the page can say "couldn't
    # check" rather than lying that zero rows are pending.
    try:
        pending_count = _PENDING_COUNT.get("count", lambda: len(svc.pending_rows()))
    except Exception:
        pending_count = None
    return templates.TemplateResponse(request=request, name="amazon_listing_uploads.html", context={
        "batches": batches,
        "pending_count": pending_count,
    })


@router.post("/automation/amazon-listings/run-now")
def run_now():
    """
    Manual out-of-cycle trigger (2026-09-18, Tamara: "a one off amazon
    upload from the purchasing sheet") -- calls the exact same
    run_pending_uploads() the 9am/9pm scheduler already uses, so a
    manual run can never behave differently or drift from the
    automatic path. Safe to run anytime, including right next to a
    scheduled run: pending_rows() only ever returns rows still flagged
    "Y" on the sheet, and a successful row has its flag cleared
    immediately (_mark_done), so there's nothing left for either run
    to double-submit.
    """
    svc.run_pending_uploads(preview=False)
    _PENDING_COUNT.invalidate()          # the count just changed -- the next page load must not show the old one
    return RedirectResponse(url="/automation/amazon-listings", status_code=303)


@router.post("/automation/amazon-listings/acknowledge/{batch_id}")
def acknowledge_one(batch_id: int):
    svc.acknowledge_batch(batch_id)
    return RedirectResponse(url="/automation/amazon-listings", status_code=303)


@router.post("/automation/amazon-listings/acknowledge-all")
def acknowledge_all():
    svc.acknowledge_all_failed_batches()
    return RedirectResponse(url="/", status_code=303)
