from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.scan_queue_service import ScanQueueService
from app.services.scan_coordinator import ScanCoordinator
from app.services.product_repository import ProductRepository

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

    if result.get("skipped") == "queue empty":
        return "Queue is empty -- nothing to run."

    if result.get("skipped") == "manual scan in progress":
        return f"Skipped {result.get('brand', 'the next item')} -- a manual scan is running elsewhere right now."

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
    performance = ProductRepository.get_brand_performance([item.brand for item in items])
    cadence = ScanQueueService.estimate_cadence(len(items))

    return {
        "items": items,
        "performance": performance,
        "cadence": cadence,
        "paused": ScanQueueService.is_paused(),
        "is_busy": ScanCoordinator.is_busy(),
        "tick_result": tick_result,
    }


@router.get("/scan-queue")
def scan_queue_page(request: Request, tick_result: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="scan_queue.html",
        context={"request": request, **build_scan_queue_context(tick_result)}
    )


@router.post("/scan-queue/add")
def scan_queue_add(brand: str = Form(...), category_ids: str = Form("")):
    ids = [c.strip() for c in category_ids.split(",") if c.strip()] or None
    ScanQueueService.add_item(brand, category_ids=ids)
    return RedirectResponse(url="/scan-queue", status_code=303)


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
    ScanQueueService.delete_item(item_id)
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
