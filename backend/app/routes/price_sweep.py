"""
Status page for the free price sweep (2026-09-21) -- see PriceSweepService. Read-only: it shows what the sweep
found, next to what Keepa's last full scan said, so the sweep's accuracy can be judged before anything is made to
depend on it.
"""
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.routes.dashboard import _format_ago
from app.services.activity_log import ActivityLog
from app.services.price_sweep_service import PriceSweepService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_AMAZON_DOMAINS = {"UK": "co.uk", "DE": "de", "FR": "fr", "ES": "es", "IT": "it"}


def _amazon_url(asin: str, market: str = "UK") -> str:
    return f"https://www.amazon.{_AMAZON_DOMAINS.get(market, 'co.uk')}/dp/{asin}"


@router.get("/price-sweep")
def price_sweep_page(request: Request):
    overview = PriceSweepService.overview()
    tick = next((r for r in ActivityLog.scheduler_overview() if r["name"] == "price_sweep"), None)
    return templates.TemplateResponse(request=request, name="price_sweep.html", context={
        "o": overview,
        "last_ago": _format_ago(tick["last_tick_at"]) if tick and tick["last_tick_at"] else None,
        "last_summary": (tick["last_summary"] if tick else "") or "",
        "amazon_url": _amazon_url,
    })
