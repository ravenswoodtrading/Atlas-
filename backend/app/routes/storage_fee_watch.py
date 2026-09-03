from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.storage_fee_service import StorageFeeService, SELL_THROUGH_LOOKBACK_DAYS

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/storage-fee-watch")
def storage_fee_watch_page(request: Request, message: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="storage_fee_watch.html",
        context={
            "request": request,
            "rows": StorageFeeService.list_ranked(),
            "sell_through_days": SELL_THROUGH_LOOKBACK_DAYS,
            "message": message,
        }
    )


@router.post("/storage-fee-watch/refresh-now")
def storage_fee_watch_refresh_now():
    result = StorageFeeService.refresh()

    if result.get("message") and result["checked"] == 0:
        message = result["message"]
    else:
        message = (
            f"Checked {result['checked']} SKUs with a storage fee or surcharge -- "
            f"{result['flagged_surcharge']} carrying an aged-inventory surcharge, "
            f"{result['flagged_shift']} flagged to consider shifting."
        )

    return RedirectResponse(url=f"/storage-fee-watch?message={quote(message)}", status_code=303)
