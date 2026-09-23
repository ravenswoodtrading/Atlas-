"""Read-only VA reporting from the VA Lead Sheet."""

import json
import time
from datetime import date, datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.google_sheets_client import open_sheet
from app.services.google_sheets_lead_sync import LEAD_SHEET_TAB, LEAD_SHEET_URL
from app.services.stale_cache import StaleCache
from app.database.database import SessionLocal
from app.database.models import WeeklyVaReportSummary

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
CHANNELS = ("OA (Price drop)", "OA (Non-price drop)", "EU A2A", "UK A2A", "Other")
RATINGS = ("Ok", "Good", "Avoid")
# The VA began submitting leads in April 2026. Earlier sheet history is not
# VA performance data and must never dilute the monthly/YTD reporting view.
VA_REPORT_START = date(2026, 4, 1)
# The Lead Sheet read (1-3s) is served from here and refreshed in the background once it is a couple of minutes old, so a
# report page never waits for Google (2026-09-21) -- see StaleCache. generate_va_summary asks for a fresh read.
_VA_SHEET_CACHE = StaleCache(ttl_seconds=120, retry_seconds=30, name="va-lead-sheet")


@lru_cache(maxsize=8192)
def _normalise(text: str) -> str:
    return " ".join(text.strip().lower().split())


@lru_cache(maxsize=512)
def _wanted(names: tuple) -> frozenset:
    return frozenset(_normalise(str(name)) for name in names)


def _value(row: dict, *names: str):
    # Cached normalisation (2026-09-21): this ran ~2 million string normalisations to build the monthly report.
    wanted = _wanted(names)
    return next((value for key, value in row.items() if _normalise(str(key)) in wanted), None)


def _number(value) -> float:
    try:
        return float(str(value or "").replace("£", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def _channel(row: dict) -> str:
    method = str(_value(row, "sourcing method", "source method", "method") or "").upper()
    if ("OA" in method or "ONLINE ARBITRAGE" in method) and "A2A" not in method:
        return "OA"
    if "EU" in method:
        return "EU A2A"
    if "UK" in method or "A2A" in method:
        return "UK A2A"
    return "Other"


def _report_channel(row: dict) -> str:
    channel = _channel(row)
    if channel != "OA":
        return channel
    status = str(_value(row, "Price drop (>5%)") or "").strip().lower()
    return "OA (Price drop)" if status == "yes" else "OA (Non-price drop)"


def _rating(row: dict) -> str | None:
    text = str(_value(row, "client rating", "client_rating", "rating") or "").strip().lower()
    return {"ok": "Ok", "good": "Good", "avoid": "Avoid"}.get(text)


def _sourcing_method_used(row: dict) -> str:
    """The VA's actual research method, distinct from the sales channel."""
    return str(_value(row, "sourcing method used") or "Not recorded").strip() or "Not recorded"


_DATE_PATTERNS = ("%d %b %y", "%d %b %Y", "%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y", "%Y-%m-%d", "%d-%m-%Y",
                  "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S")


@lru_cache(maxsize=8192)
def _parse_date_candidates(text: str) -> tuple:
    """Every date `text` parses to under any of the sheet's formats, in pattern order, without duplicates."""
    candidates = []
    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern).date()
            if parsed not in candidates:
                candidates.append(parsed)
        except ValueError:
            pass
    return tuple(candidates)


def _date_candidates(row: dict) -> list[date]:
    # In this sheet, "Date Last Added" is a workflow flag (e.g. "New
    # ASIN"), while its plain "Date" column is the real submission date.
    text = str(_value(row, "date", "date submitted", "date added") or "").strip()
    return list(_parse_date_candidates(text))


def _date_for_period(row: dict, start: date, end: date) -> date | None:
    candidates = _date_candidates(row)
    return next((item for item in candidates if start <= item < end), candidates[0] if candidates else None)


def _bounds(period: str, anchor: date) -> tuple[date, date, str]:
    if period == "month":
        start = anchor.replace(day=1)
        return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1), start.strftime("%B %Y")
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=7)
    last_day = end - timedelta(days=1)
    return start, end, f"{start.day} {start.strftime('%b')} – {last_day.day} {last_day.strftime('%b %Y')}"


def _percent(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def _read_sheet_rows() -> list[dict]:
    values = open_sheet(LEAD_SHEET_URL).worksheet(LEAD_SHEET_TAB).get_all_values()
    if not values:
        return []
    header = values[0]
    return [{header[i]: row[i] if i < len(row) else "" for i in range(len(header))} for row in values[1:] if any(row)]


def _sheet_rows(fresh: bool = False) -> list[dict]:
    """The Lead Sheet rows -- the last good read, refreshed in the background when stale. fresh=True reads it now."""
    if fresh:
        _VA_SHEET_CACHE.invalidate("rows")
    return _VA_SHEET_CACHE.get("rows", _read_sheet_rows)


def _report_data(rows: list[dict], start: date, end: date, working_days: int = 5) -> dict:
    leads = [row for row in rows if (submitted := _date_for_period(row, start, end)) and start <= submitted < end]
    qty = lambda row: _number(_value(row, "purchased qty", "purchased quantity"))
    cost = lambda row: _number(_value(row, "actual cog", "cog (unit)", "cost price", "cog"))
    profit = lambda row: _number(_value(row, "expected profit", "profit", "net profit"))
    submitted, bought = len(leads), sum(qty(row) > 0 for row in leads)
    channels = []
    for channel in CHANNELS:
        subset = [row for row in leads if _report_channel(row) == channel]
        bought_subset = sum(qty(row) > 0 for row in subset)
        channels.append({"name": channel, "submitted": len(subset), "submitted_percent": _percent(len(subset), submitted), "bought": bought_subset, "bought_percent": _percent(bought_subset, len(subset))})
    methods = {}
    for row in leads:
        method = _sourcing_method_used(row)
        bucket = methods.setdefault(method, {"name": method, "submitted": 0, "bought": 0, "units": 0.0, "spend": 0.0, "expected_profit": 0.0})
        bucket["submitted"] += 1
        if qty(row) > 0:
            bucket["bought"] += 1
        bucket["units"] += qty(row)
        bucket["spend"] += cost(row) * qty(row)
        bucket["expected_profit"] += profit(row) * qty(row)
    reasons = {}
    avoid_notes = []
    for row in leads:
        if _rating(row) == "Avoid":
            reason = str(_value(row, "reason", "rejection reason", "why not", "client notes") or "No reason recorded").strip()
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason != "No reason recorded":
                avoid_notes.append(reason)
    expected_profit = sum(profit(row) * qty(row) for row in leads)
    def drop_stats(group):
        statuses = [str(_value(row, 'Price drop (>5%)') or '').strip().lower() for row in group]
        yes, no = statuses.count('yes'), statuses.count('no')
        return {'yes': yes, 'known': yes + no, 'unknown': len(group) - yes - no,
                'percent': round(100 * yes / (yes + no), 1) if yes + no else None}
    daily = {}
    for row in leads:
        day = _date_for_period(row, start, end).isoformat()
        daily.setdefault(day, []).append(row)
    return {
        'price_drop': drop_stats(leads),
        'price_drop_daily': [{'date': day, **drop_stats(group)} for day, group in sorted(daily.items())],
        "leads": leads, "submitted": submitted, "bought": bought,
        "bought_percent": _percent(bought, submitted), "units_bought": sum(qty(row) for row in leads),
        "total_spend": sum(cost(row) * qty(row) for row in leads), "expected_profit": expected_profit,
        "bought_per_working_day": bought / working_days,
        "average_lead_value": expected_profit / submitted if submitted else 0,
        "ratings": {rating: sum(_rating(row) == rating for row in leads) for rating in RATINGS},
        "channels": channels, "methods": sorted(methods.values(), key=lambda item: (-item["submitted"], item["name"])), "avoid_notes": avoid_notes,
        "rejection_reasons": sorted(({"name": name, "count": count} for name, count in reasons.items()), key=lambda item: (-item["count"], item["name"])),
    }


def _trends(current: dict, previous: dict) -> dict:
    keys = ("submitted", "bought", "bought_percent", "units_bought", "total_spend", "expected_profit", "average_lead_value", "bought_per_working_day")
    return {key: {"change": current[key] - previous[key], "direction": "up" if current[key] > previous[key] else "down" if current[key] < previous[key] else "flat"} for key in keys}


def _saved_summary(start: date):
    db = SessionLocal()
    try:
        return db.query(WeeklyVaReportSummary).filter(WeeklyVaReportSummary.period_start == datetime.combine(start, datetime.min.time())).first()
    finally:
        db.close()


def _shift_month(month_start: date, offset: int) -> date:
    index = month_start.year * 12 + month_start.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


@router.get("/reports/va")
def va_report(request: Request, period: str = "week", date_value: str = ""):
    period = "month" if period == "month" else "week"
    try:
        anchor = date.fromisoformat(date_value) if date_value else date.today()
    except ValueError:
        anchor = date.today()
    sheet_error = None
    try:
        rows = _sheet_rows()
    except Exception:
        rows, sheet_error = [], True
    # A reporting page is most useful when it opens on the latest week with
    # real VA activity, rather than an empty current week. A user-selected
    # date is always respected.
    if not date_value and rows:
        dated_rows = [item for row in rows for item in _date_candidates(row) if item <= date.today()]
        if dated_rows:
            anchor = max(dated_rows)
    start, end, period_label = _bounds(period, anchor)
    data = _report_data(rows, start, end)
    previous = _report_data(rows, start - timedelta(days=7), start)
    active = [channel for channel in data["channels"] if channel["submitted"]]
    if sheet_error:
        commentary = ["The VA sheet could not be reached just now. Please refresh in a moment."]
    elif not data["submitted"]:
        commentary = ["No VA-sheet leads have a submitted date in this period."]
    else:
        largest, best = max(active, key=lambda item: item["submitted"]), max(active, key=lambda item: item["bought_percent"])
        commentary = [f"{largest['name']} made up {largest['submitted_percent']}% of VA submissions.", f"{best['name']} had the strongest buy rate at {best['bought_percent']}%."]
    return templates.TemplateResponse(request=request, name="va_report.html", context={"period": period, "anchor": start.isoformat(), "period_label": period_label, **data, "trends": _trends(data, previous), "previous_start": (start - timedelta(days=7)).isoformat(), "next_start": (start + timedelta(days=7)).isoformat(), "ai_summary": _saved_summary(start), "commentary": commentary, "sheet_error": sheet_error})


@router.post("/reports/va/summary")
def generate_va_summary(period_start: str = Form(...)):
    start = date.fromisoformat(period_start)
    rows = _sheet_rows(fresh=True)
    data = _report_data(rows, start, start + timedelta(days=7))
    notes = "\n".join(f"- {note}" for note in data["avoid_notes"][:80]) or "No Avoid comments were recorded."
    prompt = (
        "Write a concise weekly VA meeting summary from the Avoid comments below. "
        "Identify 2–4 recurring themes, mention counts where clear, and give one practical coaching focus. "
        "Do not quote individual comments or invent facts. Use short bullet points.\n\n"
        f"Avoid comments:\n{notes}"
    )
    try:
        from app.services.anthropic_client import _call_gemini_for_verdict
        summary_text = _call_gemini_for_verdict(prompt)
    except Exception as exc:
        return RedirectResponse(url=f"/reports/va?date_value={start.isoformat()}&summary_error=1", status_code=303)
    db = SessionLocal()
    try:
        record = db.query(WeeklyVaReportSummary).filter(WeeklyVaReportSummary.period_start == datetime.combine(start, datetime.min.time())).first()
        if record is None:
            record = WeeklyVaReportSummary(period_start=datetime.combine(start, datetime.min.time()))
            db.add(record)
        record.summary_text = summary_text
        record.generated_at = datetime.now()
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url=f"/reports/va?date_value={start.isoformat()}", status_code=303)


@router.get("/reports/va/monthly")
def va_monthly_report(request: Request, month: str = ""):
    try:
        selected = date.fromisoformat(f"{month}-01") if month else date.today().replace(day=1)
    except ValueError:
        selected = date.today().replace(day=1)
    try:
        rows = _sheet_rows()
        sheet_error = None
    except Exception:
        rows, sheet_error = [], True
    if not month and rows:
        available = [candidate for row in rows for candidate in _date_candidates(row) if candidate <= date.today()]
        if available:
            selected = max(available).replace(day=1)
    next_month = _shift_month(selected, 1)
    current = _report_data(rows, selected, next_month, working_days=20)
    previous = _report_data(rows, _shift_month(selected, -1), selected, working_days=20)
    months = []
    for offset in range(-11, 1):
        month_start = _shift_month(selected, offset)
        if month_start < VA_REPORT_START:
            continue
        month_data = _report_data(rows, month_start, _shift_month(month_start, 1), working_days=20)
        months.append({"label": month_start.strftime("%b %Y"), "month": month_start.strftime("%Y-%m"), **month_data})
    ytd_months = []
    for index in range(selected.month):
        month_start = date(selected.year, index + 1, 1)
        if month_start < VA_REPORT_START:
            continue
        ytd_months.append({"label": month_start.strftime("%b"), **_report_data(rows, month_start, _shift_month(month_start, 1), working_days=20)})
    ytd_start = max(date(selected.year, 1, 1), VA_REPORT_START)
    ytd_working_days = max(20, 20 * len(ytd_months))
    ytd = _report_data(rows, ytd_start, next_month, working_days=ytd_working_days)
    current["ytd"] = ytd
    chart_data = json.dumps({
        "labels": [item["label"] for item in months],
        "submitted": [item["submitted"] for item in months],
        "bought": [item["bought"] for item in months],
        "spend": [item["total_spend"] for item in months],
        "profit": [item["expected_profit"] for item in months],
        "ytd_labels": [item["label"] for item in ytd_months],
        "ytd_spend": [item["total_spend"] for item in ytd_months],
        "ytd_submitted": [item["submitted"] for item in ytd_months],
        "ytd_bought": [item["bought"] for item in ytd_months],
        "ytd_bought_per_day": [item["bought_per_working_day"] for item in ytd_months],
    })
    return templates.TemplateResponse(request=request, name="va_monthly_report.html", context={
        "month": selected.strftime("%Y-%m"), "month_label": selected.strftime("%B %Y"),
        **current, "trends": _trends(current, previous), "months": months,
        "chart_data": chart_data, "sheet_error": sheet_error,
    })
