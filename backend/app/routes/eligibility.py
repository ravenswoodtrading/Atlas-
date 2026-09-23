"""
Eligibility Check page (2026-09-23) -- upload a Keepa export or paste
barcodes, get back "can this account list it?" columns. All logic lives in
app/services/eligibility_service.py; answers come from RestrictionService.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.services import eligibility_service as eligibility

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE = "/tools/eligibility"


def _error(message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{PAGE}?" + urlencode({"error": message}), status_code=303)


@router.get(PAGE)
def eligibility_page(request: Request, job: str = "", error: str = ""):
    current = eligibility.get_job(job) if job else None
    saved = eligibility.saved_files()
    return templates.TemplateResponse(request=request, name="eligibility.html", context={
        "job": current, "error": error,
        "job_missing": bool(job) and current is None,
        "missing_saved": next((f for f in saved if f["job_id"] == job), None) if job and current is None else None,
        "running": [j for j in eligibility.recent_jobs() if j["status"] in ("queued", "running")],
        "saved": saved[:10], "notable_min": eligibility.NOTABLE_BRAND_MIN_RESTRICTED,
        "seconds_per_asin": eligibility.SECONDS_PER_NEW_ASIN,
    })


@router.post(f"{PAGE}/keepa")
async def start_keepa(keepa_file: UploadFile = File(...)):
    try:
        job = eligibility.start_keepa_job(keepa_file.filename or "", await keepa_file.read())
    except Exception as exc:
        return _error(f"Couldn't read that file: {exc}")
    return RedirectResponse(url=f"{PAGE}?job={job['id']}", status_code=303)


@router.post(f"{PAGE}/barcodes")
async def start_barcodes(barcodes: str = Form(""), barcode_file: UploadFile | None = File(None)):
    try:
        data = await barcode_file.read() if barcode_file and barcode_file.filename else b""
        codes = eligibility.parse_barcodes(barcodes, barcode_file.filename if data else "", data)
        job = eligibility.start_barcode_job(codes)
    except Exception as exc:
        return _error(str(exc))
    return RedirectResponse(url=f"{PAGE}?job={job['id']}", status_code=303)


@router.get(f"{PAGE}/status/{{job_id}}")
def job_status(job_id: str):
    job = eligibility.get_job(job_id)
    if job is None:
        return {"status": "missing"}
    return {"status": job["status"], "stage": job["stage"], "done": job["done"], "total": job["total"],
            "to_ask": job["to_ask"]}


@router.get(f"{PAGE}/download/{{job_id}}")
def download(job_id: str):
    job = eligibility.get_job(job_id)
    if job is None or job["status"] != "done":
        saved = next((f for f in eligibility.saved_files() if f["job_id"] == job_id), None)
        if saved:
            return saved_download(saved["name"])
        return _error("That result is no longer available.")
    return Response(content=job["content"], media_type=job["media_type"],
                    headers={"Content-Disposition": f'attachment; filename="{job["out_name"]}"'})


@router.get(f"{PAGE}/saved/{{name}}")
def saved_download(name: str):
    path = eligibility.saved_file_path(name)
    if path is None:
        return _error("That saved result is no longer available.")
    return FileResponse(path, filename=name.split("__", 1)[1])
