import io
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.replen_service import ReplenService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/replen")
def replen_page(request: Request, message: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="replen.html",
        context={
            "request": request,
            "items": ReplenService.list_items(),
            "message": message,
        }
    )


@router.post("/replen/add")
def replen_add(asin: str = Form(...), notes: str = Form("")):
    ReplenService.add_manual(asin, notes=notes)
    return RedirectResponse(url="/replen", status_code=303)


@router.post("/replen/remove")
def replen_remove(item_id: int = Form(...)):
    ReplenService.remove(item_id)
    return RedirectResponse(url="/replen", status_code=303)


@router.post("/replen/check-now")
def replen_check_now():
    result = ReplenService.check_now()

    if result["total"] == 0:
        message = "Nothing on the list to check yet."
    elif result.get("stopped_early"):
        message = (
            f"Ran low on tokens after {result['attempted']} of {result['total']} ASINs "
            f"({result['checked']} came back viable) -- stopped there rather than fail the whole "
            f"batch. {result.get('tokens_remaining')} Keepa tokens remaining. Click again once "
            f"tokens refill to continue with the rest."
        )
    else:
        message = (
            f"Checked all {result['total']} ASINs -- {result['checked']} came back as a viable "
            f"opportunity right now. {result.get('tokens_remaining')} Keepa tokens remaining."
        )

    return RedirectResponse(url=f"/replen?message={quote(message)}", status_code=303)


@router.post("/replen/import")
async def replen_import(
    buy_sheet: UploadFile = File(...),
    stk_sales: UploadFile = File(...),
    min_achieved_roi: float = Form(15.0),
):
    buy_sheet_bytes = await buy_sheet.read()
    stk_bytes = await stk_sales.read()

    try:
        summary = ReplenService.import_from_files(
            io.BytesIO(buy_sheet_bytes), io.BytesIO(stk_bytes), min_achieved_roi=min_achieved_roi,
        )
        message = (
            f"Added {summary['added']} new ASINs to the replen list "
            f"({summary['skipped_existing']} already on it, "
            f"{summary['skipped_below_threshold']} below the {min_achieved_roi:.0f}% ROI bar)."
        )
    except Exception as exc:
        message = f"Import failed: {exc}"

    return RedirectResponse(url=f"/replen?message={quote(message)}", status_code=303)
