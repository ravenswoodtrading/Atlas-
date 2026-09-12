from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import amazon_listing_upload_service as svc

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/automation/amazon-listings")
def amazon_listings_page(request: Request):
    batches = svc.batch_history()
    for batch in batches:
        batch["failures"] = svc.batch_failures(batch["id"]) if batch["failed_count"] else []
    return templates.TemplateResponse(request=request, name="amazon_listing_uploads.html", context={
        "batches": batches,
    })


@router.post("/automation/amazon-listings/acknowledge/{batch_id}")
def acknowledge_one(batch_id: int):
    svc.acknowledge_batch(batch_id)
    return RedirectResponse(url="/automation/amazon-listings", status_code=303)


@router.post("/automation/amazon-listings/acknowledge-all")
def acknowledge_all():
    svc.acknowledge_all_failed_batches()
    return RedirectResponse(url="/", status_code=303)
