from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.services import actual_performance_service as performance

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/reports/actual-performance")
def actual_performance(request: Request, imported: str = "", error: str = ""):
    rows = performance.report_rows()
    status = performance.import_status()
    passed = sum(item["outcome"] == "Passed" for item in rows)
    stock_held = sum(item["stock"] for item in rows)
    return templates.TemplateResponse(request=request, name="actual_performance.html", context={
        "rows": rows, "status": status, "passed": passed, "stock_held": stock_held,
        "imported": imported, "error": error,
    })


@router.post("/reports/actual-performance/import")
@router.post("/reports/uploads")
async def import_actual_performance(summary_file: UploadFile = File(...), daily_file: UploadFile = File(...), period_start: str = Form(...), period_end: str = Form(...)):
    try:
        summary_count, daily_count = performance.import_reports(
            (await summary_file.read()).decode("utf-8-sig"),
            (await daily_file.read()).decode("utf-8-sig"),
            period_start, period_end,
            summary_file.filename or '', daily_file.filename or '',
        )
        message = f"Imported {summary_count} SKU summaries and {daily_count} daily SKU histories."
        return RedirectResponse(url="/reports/uploads?" + urlencode({"imported": message}), status_code=303)
    except Exception as exc:
        return RedirectResponse(url="/reports/uploads?" + urlencode({"error": str(exc)}), status_code=303)


@router.get('/reports/uploads')
def report_uploads(request: Request, imported: str = '', error: str = ''):
    return templates.TemplateResponse(request=request, name='report_uploads.html', context={
        'status': performance.import_status(), 'imported': imported, 'error': error,
    })


@router.post('/reports/uploads/amazon-ledger')
async def import_amazon_ledger(ledger_file: UploadFile = File(...)):
    try:
        ledger_text = (await ledger_file.read()).decode('utf-8-sig')
        filename = ledger_file.filename or ''
        # The Amazon ledger can be tens of MB and contains hundreds of
        # thousands of rows. Keep parsing and SQLite writes off FastAPI's
        # event loop so the rest of Atlas remains responsive while it runs.
        count, start, end = await run_in_threadpool(
            performance.import_inventory_ledger, ledger_text, filename)
        message = f'Imported {count:,} Amazon ledger rows covering {start} to {end}.'
        return RedirectResponse(url='/reports/uploads?' + urlencode({'imported': message}), status_code=303)
    except Exception as exc:
        return RedirectResponse(url='/reports/uploads?' + urlencode({'error': str(exc)}), status_code=303)
