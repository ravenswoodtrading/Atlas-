import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import BaseModel

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.verdict_service import VerdictService
from app.services.anthropic_client import generate_verdict
from app.services.keepa_priority import KeepaPriority

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Matches a £ money figure (£12.50) or a percentage (18.5%, 4%) so they can
# be visually bolded wherever they appear inside a longer sentence (the
# Verdict Checker's AI-written reasoning bullets in particular) -- these
# figures are the actual "key things" a user is scanning for, and used to
# render in identical weight/colour to the surrounding prose.
_FIGURE_PATTERN = re.compile(r"(£\d[\d,]*\.\d{2}|\d+(?:\.\d+)?%)")


def highlight_figures(text: str) -> Markup:
    """
    Jinja filter: wraps £/% figures in <strong> for use in
    _verdict_metrics.html. Escapes the source text first -- it's
    AI-generated content, not markup we wrote ourselves -- then
    re-inserts the highlight tags, so the result is safe to mark
    `| safe` (via Markup) in the template.
    """
    escaped = str(escape(text))
    highlighted = _FIGURE_PATTERN.sub(r"<strong class=\"vr-figure\">\1</strong>", escaped)
    return Markup(highlighted)


templates.env.filters["highlight_figures"] = highlight_figures


def _run_verdict_check(asin: str, cost_price: float | None):
    """
    Shared by the /verdict page and POST /api/verdict: pulls Keepa metrics
    (high priority -- see KeepaPriority), calls Claude for a verdict, and
    persists a Lead(source="manual") row, per spec section 3.

    Returns (metrics, verdict, rationale, error). error is a user-facing
    string on failure; metrics/verdict/rationale are None when it's set.
    """
    with KeepaPriority.high_priority():
        metrics = VerdictService.compute_metrics(asin, cost_price=cost_price)

    if metrics is None:
        return None, None, None, "Keepa has no data for this ASIN."

    try:
        verdict, rationale = generate_verdict(metrics, va_financials=None)
    except Exception as exc:
        return metrics, None, None, f"Verdict generation failed: {exc}"

    db = SessionLocal()
    try:
        lead = Lead(
            asin=asin,
            source="manual",
            va_cost_price=cost_price,
            status="analyzed",
            verdict=verdict,
            rationale=rationale,
            keepa_metrics=json.dumps(metrics),
            analyzed_at=datetime.now(timezone.utc),
        )
        db.add(lead)
        db.commit()
    finally:
        db.close()

    return metrics, verdict, rationale, None


@router.get("/verdict")
def verdict_page(request: Request, asin: str = "", cost_price: str = ""):
    asin = asin.strip().upper()

    metrics = None
    verdict = None
    rationale = None
    error = None
    parsed_cost = None

    if cost_price:
        try:
            parsed_cost = float(cost_price)
        except ValueError:
            parsed_cost = None

    if asin:
        metrics, verdict, rationale, error = _run_verdict_check(asin, parsed_cost)

    return templates.TemplateResponse(
        request=request,
        name="verdict.html",
        context={
            "request": request,
            "asin": asin,
            "cost_price": cost_price,
            "metrics": metrics,
            "verdict": verdict,
            "rationale": rationale,
            "error": error,
        }
    )


class VerdictRequest(BaseModel):
    asin: str
    cost_price: float | None = None


@router.post("/api/verdict")
def api_verdict(body: VerdictRequest):
    asin = body.asin.strip().upper()
    metrics, verdict, rationale, error = _run_verdict_check(asin, body.cost_price)

    if error:
        return {"asin": asin, "error": error}

    return {
        "asin": asin,
        "verdict": verdict,
        "rationale": rationale,
        "metrics": metrics,
    }
