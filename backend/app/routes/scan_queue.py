from urllib.parse import quote

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.scan_queue_service import ScanQueueService
from app.services.scan_coordinator import ScanCoordinator
from app.services.product_repository import ProductRepository
from app.services.scan_schedule_service import queue_rows, set_brand_tier, recent_runs, pending_reviews, TIERS

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


def _describe_tick(result: dict) -> str:
    """
    Turns a ScanQueueService.run_next_tick() result into a one-line,
    human-readable summary for the "Run one now" confirmation banner
    -- otherwise the only visible sign anything happened is a small
    stat changing, easy to miss entirely.
    """
    if result.get("skipped") == "paused":
        return "Automated scanning is paused -- nothing was run. Resume it first."

    if result.get("skipped") == "no brands due":
        return "No brands are due yet under their saved scan tiers."

    if result.get("skipped") == "queue empty":
        return "Queue is empty -- nothing to run."

    if result.get("skipped") and "brand" in result:
        # ScanCoordinator.busy_reason() (whatever a manual/high-priority
        # scan is doing right now, or that one is waiting) -- the exact
        # message varies, unlike the fixed paused/no-brands-due/queue-
        # empty sentinels above, which is why this is a fallback rather
        # than another exact-string check.
        return f"Skipped {result.get('brand', 'the next item')} -- {result['skipped']}."

    if result.get("error"):
        return f"Couldn't run {result.get('brand', 'the next item')}: {result['error']}"

    if result.get("ran_out_of_tokens"):
        return (
            f"Ran out of tokens partway through {result['brand']} -- scanned "
            f"{result['asins_scanned_this_tick']} more ({result['scanned_count']} scanned so far this lap). "
            f"Will pick up the same page next time."
        )

    if "asins_scanned_this_tick" in result:
        lap_note = " -- completed a full lap, starting over from the top" if result.get("lap_completed") else ""

        return (
            f"Scanned {result['asins_scanned_this_tick']} more {result['brand']} products "
            f"({result['opportunities_found']} opportunities found){lap_note}."
        )

    return "Nothing to report."


def build_scan_queue_context(tick_result: str = "") -> dict:
    """
    Kept as its own function even though /scan-queue is its only caller
    now -- Leads Hub used to be a second caller (removed 2026-09-04,
    Navigation redesign: Scan Queue got a real sidebar entry of its own,
    under System > Automation, instead of only being reachable via the
    hub's "Automated" tab).
    """
    items = ScanQueueService.list_items()
    paused = ScanQueueService.is_paused()
    cadence = ScanQueueService.estimate_cadence(len(items))

    return {
        "items": queue_rows(items, paused),
        "performance": {},
        "cadence": cadence,
        "paused": paused,
        "is_busy": ScanCoordinator.is_busy(),
        "scan_holder": ScanCoordinator.status(),
        "tick_result": tick_result,
        "pending_review_count": len(pending_reviews()),
    }


@router.get("/scan-queue")
def scan_queue_page(request: Request, tick_result: str = "", brand: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="scan_queue.html",
        context={"request": request, "brand_prefill": brand, **build_scan_queue_context(tick_result)}
    )


@router.post("/scan-queue/add")
def scan_queue_add(brand: str = Form(...), category_ids: str = Form("")):
    if not brand.strip():
        raise HTTPException(422, "Enter a brand name")
    ids = [c.strip() for c in category_ids.split(",") if c.strip()] or None
    ScanQueueService.add_item(brand, category_ids=ids)
    return RedirectResponse(url="/scan-queue", status_code=303)


@router.get("/scan-queue/brand/{item_id}")
def scan_queue_brand(request: Request, item_id: int):
    items = ScanQueueService.list_items()
    item = next((i for i in items if i.id == item_id), None)
    if item is None:
        raise HTTPException(404, "Brand is no longer in the queue")
    row = next(r for r in queue_rows(items, ScanQueueService.is_paused()) if r["brand"] == item.brand)
    return templates.TemplateResponse(request=request, name="scan_brand_details.html", context={
        "request": request, "row": row,
        "performance": ProductRepository.get_brand_performance([item.brand]).get(item.brand, {}),
        "runs": recent_runs(item.brand), "tiers": TIERS,
        "recommendation": next((r for r in pending_reviews() if r.brand == item.brand), None),
    })


@router.post("/scan-queue/tier")
def scan_queue_tier(item_id: int = Form(...), tier: str = Form(...)):
    item = next((i for i in ScanQueueService.list_items() if i.id == item_id), None)
    if item is None:
        raise HTTPException(404, "Brand is no longer in the queue")
    try:
        set_brand_tier(item.brand, tier)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(url=f"/scan-queue/brand/{item_id}", status_code=303)


@router.post("/scan-queue/add-bulk")
def scan_queue_add_bulk(brands: str = Form(...), category_ids: str = Form("")):
    """
    Same category filter applied to every brand in the list -- one
    queue item per brand, each added via the same add_item() a single
    add uses (so the "continue where a previous campaign for this
    brand left off" behaviour still applies per brand). Accepts brands
    one per line, comma-separated on one line, or a mix of both --
    pasting a list is more likely to come out one of those ways than
    reliably one-per-line.
    """
    ids = [c.strip() for c in category_ids.split(",") if c.strip()] or None

    names = []
    seen = set()
    for line in brands.replace(",", "\n").splitlines():
        name = line.strip().lower()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for name in names:
        ScanQueueService.add_item(name, category_ids=ids)

    return RedirectResponse(url="/scan-queue", status_code=303)


@router.post("/scan-queue/delete")
def scan_queue_delete(item_id: int = Form(...)):
    ScanQueueService.remove_brand(item_id)
    return RedirectResponse(url="/scan-queue", status_code=303)


@router.post("/scan-queue/move")
def scan_queue_move(item_id: int = Form(...), direction: str = Form(...)):
    ScanQueueService.move_item(item_id, direction)
    return RedirectResponse(url="/scan-queue", status_code=303)


@router.post("/scan-queue/pause")
def scan_queue_pause():
    ScanQueueService.set_paused(True)
    return RedirectResponse(url="/scan-queue", status_code=303)


@router.post("/scan-queue/resume")
def scan_queue_resume():
    ScanQueueService.set_paused(False)
    return RedirectResponse(url="/scan-queue", status_code=303)


@router.post("/scan-queue/run-now")
def scan_queue_run_now():
    """
    Manual "run one tick now" -- lets the user make immediate progress
    without waiting for the scheduler's next interval, e.g. right
    after adding a new item. Redirects back with a summary of exactly
    what happened, since otherwise the only visible change is a small
    stat update.
    """
    result = ScanQueueService.run_next_tick()
    message = _describe_tick(result)
    return RedirectResponse(url=f"/scan-queue?tick_result={quote(message)}", status_code=303)
