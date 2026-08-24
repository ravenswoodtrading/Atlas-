import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository
from app.services.signal_service import SignalService
from app.services.scan_coordinator import ScanCoordinator
from app.services.opportunity_engine import OpportunityEngine

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

SIGNAL_TYPES = ["stock_out", "price_spike", "ceiling_recheck"]

SIGNAL_TYPE_LABELS = {
    "stock_out": "Stock-out",
    "price_spike": "Price spike",
    "ceiling_recheck": "Ceiling cleared",
}

# Icon + colour per signal type -- matches the badge styling approach
# already used elsewhere (Competitors' sourcing tags, Review Queue's
# recommendation badges): a small dict the template looks up by key
# rather than a chain of Jinja if/elif per row.
SIGNAL_TYPE_BADGE = {
    "stock_out": {"icon": "bi-exclamation-octagon", "bg": "#fdeee0", "fg": "#b3541e"},
    "price_spike": {"icon": "bi-graph-up-arrow", "bg": "#e6e9fb", "fg": "#3a3fc4"},
    "ceiling_recheck": {"icon": "bi-arrow-up-circle", "bg": "#e6f6ea", "fg": "#1e7a41"},
}

SORT_OPTIONS = {
    "newest": "Newest signal first",
    "target_price": "Highest target buy price",
    "sales_evidence": "Strongest sales evidence",
}


def _detail_line(match, reasoning: dict) -> str:
    """
    One human-readable line explaining WHY this matched, built from
    signal_reasoning_json -- the raw numbers stay inspectable via the
    JSON itself, this is just a plain-English summary for the table
    row. Falls back to a generic line if a field's missing (e.g. an
    older match saved before a reasoning field was added).
    """
    if match.signal_type == "stock_out":
        ref = reasoning.get("reference_price_used")
        return f"Currently out of stock -- 90d typical price was £{ref:.2f}" if ref else "Currently out of stock"

    if match.signal_type == "price_spike":
        now = reasoning.get("buy_box_now")
        prior = reasoning.get("buy_box_90d")
        if now and prior:
            pct = round(((now - prior) / prior) * 100)
            return f"Buy Box up from £{prior:.2f} to £{now:.2f} ({pct}% over 90d)"
        return "Buy Box price has risen over the last 90 days"

    if match.signal_type == "ceiling_recheck":
        prior = reasoning.get("buy_box_at_reject")
        now = reasoning.get("reference_price_used") or match.buy_box_now
        if prior:
            return f"Was rejected at £{prior:.2f} -- now clears the fee ceiling at £{now:.2f}"
        return "Now clears the fee ceiling, previously didn't"

    return ""


def _eu_history_view(eu_history_json: str, target_roi_pct: float) -> dict:
    """
    Turns SignalMatch.eu_history_json (Atlas's own last scan record
    for this ASIN, if any -- see ProductRepository.get_last_eu_check)
    into template-ready fields. Never a fresh EU lookup -- see
    SignalMatch's docstring.
    """
    if not eu_history_json:
        return {}

    try:
        history = json.loads(eu_history_json)
    except (ValueError, TypeError):
        return {}

    if not history or not history.get("best_source_marketplace"):
        return {}

    days_ago = None
    scanned_at = history.get("scanned_at")
    if scanned_at:
        try:
            parsed = datetime.fromisoformat(scanned_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            days_ago = (datetime.now(timezone.utc) - parsed).days
        except ValueError:
            days_ago = None

    roi = history.get("roi") or 0.0
    roi_90d = history.get("roi_90d") or 0.0
    peak_profit = history.get("peak_profit") or 0.0
    peak_roi = history.get("peak_roi") or 0.0
    peak_viable_days_90d = history.get("peak_viable_days_90d") or 0

    # "Don't rule this out" flag (2026-08-20) -- Atlas's last EU check
    # found it not viable at today's OR the 90-day price (the same bar
    # under_target already flags as "too pricey"), but it WOULD clear
    # a genuine, recurring PEAK window (see OpportunityEngine.
    # PEAK_WINDOW/PEAK_MIN_VIABLE_DAYS_90D) -- i.e. this isn't just a
    # one-off spike Atlas is imagining, it was actually profitable on
    # real days. Surfaced so a price-spike/stock-out signal on a
    # product that "looks bad" by the plain roi/roi_90d figures isn't
    # silently written off, matching the user's own framing: a
    # price-drop lead "shouldn't be ruled out... just shown
    # differently."
    not_viable_today_or_90d = (
        max(roi, roi_90d) < OpportunityEngine.MIN_VIABLE_ROI
    )
    peak_worth_noting = (
        not_viable_today_or_90d
        and peak_profit > 0
        and peak_roi >= OpportunityEngine.MIN_VIABLE_ROI
        and peak_viable_days_90d >= OpportunityEngine.PEAK_MIN_VIABLE_DAYS_90D
    )

    return {
        "marketplace": history.get("best_source_marketplace"),
        "cost": history.get("best_source_cost_gbp"),
        "roi": roi,
        "days_ago": days_ago,
        "under_target": roi < target_roi_pct,
        "peak_profit": peak_profit,
        "peak_roi": peak_roi,
        "peak_viable_days_90d": peak_viable_days_90d,
        "peak_worth_noting": peak_worth_noting,
    }


def build_signals_context(signal_type: str = "", category: str = "",
                           sort: str = "newest", check_result: str = "") -> dict:
    """
    Shared with the Leads hub's "Automated" group (leads_hub.py) --
    see build_scan_queue_context's own comment for why this is split
    out rather than duplicated.
    """
    queries = ProductRepository.list_signal_queries()
    matches = ProductRepository.list_signal_matches(include_dismissed=False, limit=500)

    counts_by_type = {t: 0 for t in SIGNAL_TYPES}
    categories_seen = set()

    for m in matches:
        if m.signal_type in counts_by_type:
            counts_by_type[m.signal_type] += 1
        if m.category_name:
            categories_seen.add(m.category_name)

    if signal_type in SIGNAL_TYPES:
        matches = [m for m in matches if m.signal_type == signal_type]

    if category:
        matches = [m for m in matches if m.category_name == category]

    if sort == "target_price":
        matches.sort(key=lambda m: m.target_buy_price_gbp, reverse=True)
    elif sort == "sales_evidence":
        matches.sort(key=lambda m: (m.monthly_sales, m.sales_drops_30d), reverse=True)
    else:
        matches.sort(key=lambda m: m.detected_at, reverse=True)

    rows = []
    for m in matches:
        try:
            reasoning = json.loads(m.signal_reasoning_json or "{}")
        except (ValueError, TypeError):
            reasoning = {}

        rows.append({
            "match": m,
            "badge": SIGNAL_TYPE_BADGE.get(m.signal_type, {"icon": "bi-broadcast", "bg": "#eee", "fg": "#333"}),
            "type_label": SIGNAL_TYPE_LABELS.get(m.signal_type, m.signal_type),
            "detail_line": _detail_line(m, reasoning),
            "eu_history": _eu_history_view(m.eu_history_json, SignalService.TARGET_ROI_PCT),
        })

    return {
        "rows": rows,
        "queries": queries,
        "total_count": len(matches),
        "counts_by_type": counts_by_type,
        "categories_seen": sorted(categories_seen),
        "signal_type": signal_type,
        "category": category,
        "sort": sort,
        "sort_options": SORT_OPTIONS,
        "signal_type_labels": SIGNAL_TYPE_LABELS,
        "target_roi_pct": SignalService.TARGET_ROI_PCT,
        "check_result": check_result,
    }


@router.get("/signals")
def signals_page(request: Request, signal_type: str = "", category: str = "",
                  sort: str = "newest", check_result: str = ""):
    """
    The day-to-day Signals feed -- every non-dismissed SignalMatch
    across all enabled SignalQuery definitions, newest first by
    default. See SignalService for the cost model (a single UK-only
    Keepa lookup per NEW candidate, no EU tokens spent building this
    list at all) and app/database/models.py's SignalMatch/SignalQuery/
    CeilingRejected docstrings for the full design.
    """
    return templates.TemplateResponse(
        request=request,
        name="signals.html",
        context={"request": request, **build_signals_context(signal_type, category, sort, check_result)}
    )


def build_signal_queries_context(check_result: str = "") -> dict:
    """Shared with the Leads hub's "Automated" group (leads_hub.py)."""
    return {
        "queries": ProductRepository.list_signal_queries(),
        "signal_types": SIGNAL_TYPES,
        "signal_type_labels": SIGNAL_TYPE_LABELS,
        "check_result": check_result,
    }


@router.get("/signals/queries")
def signal_queries_page(request: Request, check_result: str = ""):
    """
    "Manage signal queries" -- turn signals on/off per category, same
    pattern as the Scan Queue's brand list, just for signal
    definitions (stock_out/price_spike/ceiling_recheck) instead of
    brands. ceiling_recheck ignores category_ids entirely (see
    SignalQuery's docstring) -- ONE ceiling_recheck query is normally
    all you need, since it just cycles through Atlas's own
    CeilingRejected pool.
    """
    return templates.TemplateResponse(
        request=request,
        name="signal_queries.html",
        context={"request": request, **build_signal_queries_context(check_result)}
    )


@router.post("/signals/queries/add")
def signal_query_add(name: str = Form(...), signal_type: str = Form(...),
                      category_ids: str = Form("")):
    if signal_type not in SIGNAL_TYPES:
        return RedirectResponse(
            url=f"/signals/queries?check_result={quote('Unknown signal type.')}", status_code=303
        )

    cleaned_ids = ",".join(c.strip() for c in category_ids.split(",") if c.strip())
    ProductRepository.create_signal_query(name.strip(), signal_type, cleaned_ids)

    return RedirectResponse(url="/signals/queries", status_code=303)


@router.post("/signals/queries/toggle")
def signal_query_toggle(query_id: int = Form(...), enabled: bool = Form(...)):
    ProductRepository.set_signal_query_enabled(query_id, enabled)
    return RedirectResponse(url="/signals/queries", status_code=303)


@router.post("/signals/queries/delete")
def signal_query_delete(query_id: int = Form(...)):
    ProductRepository.delete_signal_query(query_id)
    return RedirectResponse(url="/signals/queries", status_code=303)


@router.post("/signals/run")
def signal_run(query_id: int = Form(...), return_to: str = Form("/signals")):
    """
    Manual "Run now" -- see SignalService's docstring for why this is
    deliberately NOT on an automatic scheduler yet. Takes the same
    manual-scan-priority lock as every other Keepa-token-spending
    manual action (Discovery/Watchlist/Replen/Competitors "check
    now"), so the background scan-queue scheduler can't sneak a tick
    in mid-run and start competing for the same token budget.
    """
    ScanCoordinator.acquire_for_manual_scan()

    try:
        result = SignalService().run_check(query_id)
    finally:
        ScanCoordinator.release_after_manual_scan()

    if result.get("error"):
        message = result["error"]
    elif "still_rejected" in result:
        message = (
            f"Re-checked {result['candidates_checked']} previously-rejected ASIN(s): "
            f"{result['new_matches']} now clear the ceiling, {result['still_rejected']} still don't."
        )
    else:
        message = (
            f"Found {result.get('raw_candidate_count', 0)} candidate(s), "
            f"{result['new_matches']} new match(es) "
            f"({result.get('skipped_gated', 0)} gated, {result.get('skipped_excluded', 0)} excluded, "
            f"{result.get('skipped_unconfirmed', 0)} unconfirmed skipped)."
        )

    return RedirectResponse(url=f"{return_to}?check_result={quote(message)}", status_code=303)


@router.post("/signals/dismiss")
def signal_dismiss(match_id: int = Form(...), return_to: str = Form("/signals")):
    ProductRepository.dismiss_signal_match(match_id)
    return RedirectResponse(url=return_to, status_code=303)
