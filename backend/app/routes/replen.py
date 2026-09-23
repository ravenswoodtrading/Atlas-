from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.replen_a2a_service import (
    ReplenA2AService, STATUS_GROUPS, STATUS_LABELS, DAILY_FRACTION, BUY_ROI_PCT, COVER_DAYS_MAX,
    SCHEDULER_NAME, STOCK_FILTERS, SOLD_FILTERS, BOUGHT_FILTERS, ROI_FILTERS, TREND_FILTERS,
    CHECKED_FILTERS, SORT_CHOICES, SORT_DEFAULT_DESC, PAGE_SIZES,
)
from app.services.activity_log import ActivityLog

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

DEFAULT_PAGE_SIZE = 50

# Every query-string parameter the page understands, in the order they appear in a URL.
# `ignored` is handled separately (a bool). Anything empty is left out of generated links.
FILTER_KEYS = ("group", "q", "stock", "sold", "bought", "market", "brand", "category", "roi", "trend",
               "checked", "sort", "order", "per")


def _redirect(message: str, params: dict | None = None):
    url = "/replen?" + urlencode({"message": message, **{k: v for k, v in (params or {}).items() if v}},
                                  quote_via=quote)
    return RedirectResponse(url=url, status_code=303)


@router.get("/replen")
def replen_page(request: Request, message: str = "", group: str = "", q: str = "", stock: str = "",
                sold: str = "", bought: str = "", market: str = "", brand: str = "", category: str = "",
                roi: str = "", trend: str = "", checked: str = "", sort: str = "verdict", order: str = "",
                per: str = "", page: int = 1, ignored: bool = False):
    per_size = DEFAULT_PAGE_SIZE
    if per.isdigit() and int(per) in PAGE_SIZES:
        per_size = int(per)

    data = ReplenA2AService.list_rows(
        group=group, query=q, show_ignored=ignored, stock=stock, sold=sold, bought=bought, market=market,
        brand=brand, category=category, roi=roi, trend=trend, checked=checked, sort=sort, order=order,
        page=page, page_size=per_size,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stock_age_hours = (
        (now - data["stock_checked_at"]).total_seconds() / 3600 if data["stock_checked_at"] else None
    )
    scheduler = next((r for r in ActivityLog.scheduler_overview() if r["name"] == SCHEDULER_NAME), None)

    # The current view, minus anything at its default, so links stay short and a page reload keeps
    # every filter.
    state = {"group": group, "q": q, "stock": stock, "sold": sold, "bought": bought, "market": market,
             "brand": brand, "category": category, "roi": roi, "trend": trend, "checked": checked,
             "sort": "" if sort == "verdict" else sort, "order": order,
             "per": "" if per_size == DEFAULT_PAGE_SIZE else str(per_size),
             "ignored": "true" if ignored else ""}

    def url(**changes):
        """
        A /replen link for the current view with `changes` applied (None or "" removes a
        parameter). Any change other than `page` sends you back to page 1, so narrowing a filter
        never strands you on a page that no longer exists.
        """
        merged = dict(state)
        if "page" not in changes:
            merged.pop("page", None)
        for key, value in changes.items():
            merged[key] = value
        query = {k: v for k, v in merged.items() if v not in (None, "", 1)}
        return "/replen" + ("?" + urlencode(query, quote_via=quote) if query else "")

    active_sort = sort if sort in dict(SORT_CHOICES) else "verdict"
    sort_desc = (order == "desc") if order in ("asc", "desc") else SORT_DEFAULT_DESC[active_sort]
    # The current view as a query string (page included) -- posted back by the row buttons so
    # hiding or re-pricing one item returns you to the same filters, sort and page.
    view_qs = url(page=data["page"]).partition("?")[2]

    def with_selected(options, selected):
        """(value, label) pairs, adding the selected value if the list doesn't already contain it."""
        if selected and not any(value == selected for value, _ in options):
            options = options + [(selected, selected)]
        return options

    market_opts = [(m, f"{m} ({n})") for m, n in data["options"]["markets"]] + [("none", "No source")]
    brand_opts = with_selected([(b, f"{b} ({n})") for b, n in data["options"]["brands"]], brand)
    category_opts = with_selected([(c, f"{c} ({n})") for c, n in data["options"]["categories"]], category)

    filter_labels = {
        "stock": dict(STOCK_FILTERS), "sold": dict(SOLD_FILTERS), "bought": dict(BOUGHT_FILTERS),
        "roi": dict(ROI_FILTERS), "trend": dict(TREND_FILTERS), "checked": dict(CHECKED_FILTERS),
    }
    # Active filters as removable pills: (label shown, link that removes just that filter).
    pills = []
    if q:
        pills.append((f'Search: "{q}"', url(q=None)))
    for key, title in (("stock", "Stock"), ("sold", "Sold"), ("bought", "Bought"), ("roi", "ROI"),
                       ("trend", "Trend"), ("checked", "Priced")):
        value = state[key]
        if value:
            pills.append((f"{title}: {filter_labels[key].get(value, value)}", url(**{key: None})))
    if market:
        pills.append((f"Source: {'none' if market == 'none' else market.upper()}", url(market=None)))
    if brand:
        pills.append((f"Brand: {brand}", url(brand=None)))
    if category:
        pills.append((f"Category: {category}", url(category=None)))
    if group:
        pills.append((f"Verdict: {group}", url(group=None)))

    return templates.TemplateResponse(
        request=request,
        name="replen.html",
        context={
            "request": request,
            "data": data,
            "group": group,
            "q": q,
            "state": state,
            "url": url,
            "pills": pills,
            "sort": active_sort,
            "sort_desc": sort_desc,
            "view_qs": view_qs,
            "order": order,
            "per_size": per_size,
            "show_ignored": ignored,
            "message": message,
            "groups": list(STATUS_GROUPS),
            "labels": STATUS_LABELS,
            "run": ReplenA2AService.run_status(),
            "stock_age_hours": stock_age_hours,
            "scheduler": scheduler,
            "daily_pct": int(DAILY_FRACTION * 100),
            "buy_roi": BUY_ROI_PCT,
            "cover_days_max": COVER_DAYS_MAX,
            "stock_filters": STOCK_FILTERS,
            "sold_filters": SOLD_FILTERS,
            "bought_filters": BOUGHT_FILTERS,
            "roi_filters": ROI_FILTERS,
            "trend_filters": TREND_FILTERS,
            "checked_filters": CHECKED_FILTERS,
            "sort_choices": SORT_CHOICES,
            "page_sizes": PAGE_SIZES,
            "market_opts": market_opts,
            "brand_opts": brand_opts,
            "category_opts": category_opts,
        },
    )


@router.post("/replen/refresh")
def replen_refresh():
    """Free: re-reads the Buy Sheet, refreshes FBA stock and 30-day sales from Amazon, re-judges every row."""
    started = ReplenA2AService.start_background("Refresh purchases, stock and sales", ReplenA2AService.refresh_free_data)
    return _redirect(
        "Refreshing purchases, stock and sales in the background (no Keepa tokens) -- takes a few minutes; "
        "this page reloads itself." if started else "Something is already running -- wait for it to finish."
    )


@router.post("/replen/check-batch")
def replen_check_batch():
    """Spends Keepa tokens: the same rolling batch the daily job would take next, run now."""
    started = ReplenA2AService.start_background(
        "Keepa re-price of the next batch", ReplenA2AService.run_daily, manual=True, force_stock=True,
    )
    return _redirect(
        "Re-pricing the next batch on Keepa in the background -- takes a few minutes; this page reloads itself."
        if started else "Something is already running -- wait for it to finish."
    )


async def _return_view(request: Request) -> dict:
    """The view the button was pressed on, posted back as a hidden `view` query string."""
    form = await request.form()
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(str(form.get("view", ""))).items() if v}


@router.post("/replen/check-one")
async def replen_check_one(request: Request, asin: str = Form(...)):
    view = await _return_view(request)
    started = ReplenA2AService.start_background(f"Keepa re-price of {asin}", ReplenA2AService.check_one, asin)
    return _redirect(
        f"Re-pricing {asin} on Keepa -- this page reloads itself." if started
        else "Something is already running -- wait for it to finish.",
        view,
    )


@router.post("/replen/ignore")
async def replen_ignore(request: Request, asin: str = Form(...)):
    view = await _return_view(request)
    ReplenA2AService.set_ignored(asin, True)
    return _redirect(f"Hidden {asin}. It won't be re-priced or alerted on. Use 'Show hidden' to bring it back.", view)


@router.post("/replen/unignore")
async def replen_unignore(request: Request, asin: str = Form(...)):
    view = await _return_view(request)
    ReplenA2AService.set_ignored(asin, False)
    return _redirect(f"Restored {asin}.", view)
