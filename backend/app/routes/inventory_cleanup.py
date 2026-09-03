from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.inventory_cleanup_service import (
    InventoryCleanupService, MIN_DAYS_OUT_OF_STOCK, DISMISS_SNOOZE_DAYS,
)

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/inventory-cleanup")
def inventory_cleanup_page(request: Request, message: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="inventory_cleanup.html",
        context={
            "request": request,
            "pending": InventoryCleanupService.list_pending_review(),
            "monitoring": InventoryCleanupService.list_monitoring(),
            "history": InventoryCleanupService.list_history(),
            "min_days": MIN_DAYS_OUT_OF_STOCK,
            "dismiss_snooze_days": DISMISS_SNOOZE_DAYS,
            "message": message,
        }
    )


@router.post("/inventory-cleanup/check-now")
def inventory_cleanup_check_now():
    result = InventoryCleanupService.detect_out_of_stock_candidates()

    if result.get("message") and result["checked"] == 0:
        message = result["message"]
    else:
        message = (
            f"Checked {result['checked']} SKUs -- {result['flagged']} newly flagged, "
            f"{result['graduated']} graduated to Ready to review, "
            f"{result['restocked_cleared']} cleared (restocked since last check), "
            f"{result.get('revived', 0)} revived after their snooze expired."
        )

    return RedirectResponse(url=f"/inventory-cleanup?message={quote(message)}", status_code=303)


@router.post("/inventory-cleanup/delete")
async def inventory_cleanup_delete(request: Request):
    form = await request.form()
    candidate_ids = [int(v) for v in form.getlist("candidate_ids") if str(v).strip()]

    if not candidate_ids:
        return RedirectResponse(
            url=f"/inventory-cleanup?message={quote('Nothing selected.')}", status_code=303
        )

    result = InventoryCleanupService.approve_and_delete(candidate_ids)
    message = (
        f"Deleted {result['deleted']} listing(s) from Amazon. "
        f"{result['failed']} failed (see the error shown on that row -- left pending, safe to retry). "
        f"{result['restocked_skipped']} were skipped -- they'd restocked since the last check."
    )
    return RedirectResponse(url=f"/inventory-cleanup?message={quote(message)}", status_code=303)


@router.post("/inventory-cleanup/dismiss")
async def inventory_cleanup_dismiss(request: Request):
    form = await request.form()
    candidate_ids = [int(v) for v in form.getlist("candidate_ids") if str(v).strip()]

    if not candidate_ids:
        return RedirectResponse(
            url=f"/inventory-cleanup?message={quote('Nothing selected.')}", status_code=303
        )

    count = InventoryCleanupService.dismiss(candidate_ids)
    return RedirectResponse(
        url=f"/inventory-cleanup?message={quote(f'Snoozed {count} listing(s) for {DISMISS_SNOOZE_DAYS} days -- kept on Amazon, will be re-checked fresh once the snooze expires.')}",
        status_code=303,
    )
