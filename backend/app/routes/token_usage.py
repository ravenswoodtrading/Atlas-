from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.token_usage_service import TokenUsageService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Human-readable labels for the category/call_type codes TokenUsageEvent
# stores -- kept here (not in the service or model) since this is
# purely a display concern for this one page.
CATEGORY_LABELS = {
    "scan_queue": "Scan Queue",
    "replen": "Replen",
    "watchlist": "Watchlist",
    "competitor_watch": "Competitor Watch",
    "discovery": "Discovery",
    "signals": "Signals",
    "verdict": "Verdict Checker",
    "oa_lookup": "OA Lookup",
    "oa_discovery": "OA Source Discovery",
    "category_survey": "Category Survey",
    "manual_api": "Manual API call",
    "debug": "Debug endpoint",
    "other": "Other / unlabelled",
}


@router.get("/settings/token-usage")
def token_usage_page(request: Request, days: int = 30):
    """
    Settings > Token Usage -- day-by-day and per-feature breakdown of
    real Keepa token spend, plus SP-API's estimated savings, so it's
    possible to see WHAT is actually spending tokens and judge where a
    further Stage 1/2-style change would help most (2026-08-21). See
    TokenUsageService/TokenUsageEvent for how these numbers are
    recorded -- only call sites that have been instrumented show up
    here; anything not yet updated falls into "Other / unlabelled"
    rather than silently vanishing.
    """
    daily = TokenUsageService.daily_summary(days=days)
    category_totals = TokenUsageService.category_totals(days=days)
    savings = TokenUsageService.savings_totals(days=days)

    real_spend_rows = [row for row in daily if row["call_type"] != "sp_api_saved"]

    dates = sorted({row["date"] for row in real_spend_rows})
    categories = sorted({row["category"] for row in real_spend_rows})

    pivot = {date: {} for date in dates}
    for row in real_spend_rows:
        pivot[row["date"]][row["category"]] = pivot[row["date"]].get(row["category"], 0.0) + row["tokens"]

    daily_rows = [
        {
            "date": date,
            "total": round(sum(pivot[date].values()), 1),
            "by_category": {cat: round(pivot[date].get(cat, 0.0), 1) for cat in categories},
        }
        for date in reversed(dates)
    ]

    return templates.TemplateResponse(
        request=request,
        name="token_usage.html",
        context={
            "request": request,
            "days": days,
            "categories": categories,
            "category_labels": CATEGORY_LABELS,
            "daily_rows": daily_rows,
            "category_totals": category_totals,
            "savings": savings,
        },
    )
