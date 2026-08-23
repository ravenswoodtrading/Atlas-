import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
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

# Bulk submission volume guard (2026-08-23, sourcing-agent brief section
# 5) -- each ASIN in a bulk batch is checked sequentially (Keepa's
# client is synchronous, and a promising one costs a second Keepa call
# plus a second Claude call for its deep dive), so a single POST/api
# request's response time scales directly with batch size. Same
# pragmatic-cap reasoning as MAX_CONSIDER_LEADS elsewhere -- reject
# outright past this rather than let one request run for minutes.
MAX_BULK_ASINS = 30


def _run_verdict_check_inner(asin: str, cost_price: float | None):
    """
    The two-pass pipeline for ONE ASIN (2026-08-23, sourcing-agent
    brief section 5) -- assumes the caller has already entered
    KeepaPriority.high_priority() (see _run_verdict_check for a single
    ASIN, _run_bulk_verdict_check for a whole batch sharing ONE
    high-priority window rather than one per ASIN).

    Pass 1 (always): today's existing cheap baseline check.
    Pass 2 (only if pass 1 came back BUY or WATCH): re-checks with
    VerdictService.compute_metrics' deep_dive=True -- Keepa per-seller
    stock levels plus a free SP-API live price cross-check, see that
    docstring for why this is gated rather than always-on (a real,
    measured token-cost difference). The deep-dive verdict REPLACES
    the baseline one when it runs; an AVOID at baseline never gets a
    second look, since there's no cheaper way to already know it's not
    promising.

    Returns (metrics, verdict, rationale, deep_dive_fired, error).
    error is a user-facing string on failure; metrics/verdict/rationale
    are None only when error is set to the "no Keepa data" case --
    a verdict-generation failure still returns the baseline metrics.
    """
    metrics = VerdictService.compute_metrics(asin, cost_price=cost_price)

    if metrics is None:
        return None, None, None, False, "Keepa has no data for this ASIN."

    # Sourcing agent brief step 7 -- computed once per ASIN (brand/
    # category don't change between the baseline and deep-dive passes
    # below) and threaded into every generate_verdict call, then
    # stamped onto whichever metrics dict actually ends up persisted
    # so the Review Queue can show what the verdict was weighed against.
    similar_rejections = VerdictService.get_similar_rejections(
        asin, metrics.get("brand"), metrics.get("category_name")
    )
    metrics["similar_rejections"] = similar_rejections

    try:
        verdict, rationale = generate_verdict(metrics, va_financials=None, similar_rejections=similar_rejections)
    except Exception as exc:
        return metrics, None, None, False, f"Verdict generation failed: {exc}"

    deep_dive_fired = False

    if verdict in ("BUY", "WATCH"):
        deep_metrics = VerdictService.compute_metrics(asin, cost_price=cost_price, deep_dive=True)

        if deep_metrics is not None:
            deep_metrics["similar_rejections"] = similar_rejections
            try:
                deep_verdict, deep_rationale = generate_verdict(
                    deep_metrics, va_financials=None, similar_rejections=similar_rejections
                )
                metrics, verdict, rationale = deep_metrics, deep_verdict, deep_rationale
                deep_dive_fired = True
            except Exception as exc:
                # Deep-dive re-verdict failed -- keep the baseline
                # result rather than losing an otherwise-good check to
                # a second-pass hiccup (e.g. a transient Claude error).
                print(f"Deep-dive verdict failed for {asin}, keeping baseline result: {exc}")

    return metrics, verdict, rationale, deep_dive_fired, None


def _save_lead(
    asin: str, cost_price: float | None, metrics: dict, verdict: str, rationale: str,
    source_detail: str | None = None, source: str = "manual",
) -> int:
    """
    Persists a Lead row, per spec section 3. Returns its id.

    source defaults to "manual" (the Verdict Checker's own single/bulk
    ASIN entry) but the Shortlist sweep (app/routes/shortlist.py)
    passes "shortlist" -- a third, distinct value from the original
    "manual" | "sheet" pair, so a shortlist-originated lead is
    traceable back to the pipeline that surfaced it rather than looking
    like something a human typed in by hand.
    """
    db = SessionLocal()
    try:
        lead = Lead(
            asin=asin,
            source=source,
            va_cost_price=cost_price,
            status="analyzed",
            verdict=verdict,
            rationale=rationale,
            keepa_metrics=json.dumps(metrics),
            analyzed_at=datetime.now(timezone.utc),
            source_detail=source_detail or None,
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead.id
    finally:
        db.close()


def _run_verdict_check(asin: str, cost_price: float | None, source_detail: str | None = None):
    """
    Single-ASIN entry point -- wraps _run_verdict_check_inner in its
    own KeepaPriority window and persists the result. Shared by the
    /verdict page and POST /api/verdict.

    Returns (metrics, verdict, rationale, deep_dive_fired, error).
    """
    with KeepaPriority.high_priority():
        metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check_inner(asin, cost_price)

    if error:
        return metrics, verdict, rationale, deep_dive_fired, error

    _save_lead(asin, cost_price, metrics, verdict, rationale, source_detail=source_detail)
    return metrics, verdict, rationale, deep_dive_fired, None


def _run_bulk_verdict_check(items: list[tuple[str, float | None]], source_detail: str | None = None) -> list[dict]:
    """
    Bulk entry point -- the whole batch shares ONE KeepaPriority window
    (per its own docstring: marking active once, not once per ASIN, is
    what actually bounds how long an in-flight low-priority scan waits
    before yielding). Returns one result dict per input ASIN, in order,
    each report-shaped for direct template/JSON use.

    source_detail, if given, applies to every lead in the batch -- a
    bulk submission is normally a set of ASINs someone found in one
    place (a single retailer's clearance page, one wholesaler list),
    so one shared note is more realistic than asking for one per line
    in a plain textarea.
    """
    results = []

    with KeepaPriority.high_priority():
        for asin, cost_price in items:
            metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check_inner(asin, cost_price)

            lead_id = None
            if not error:
                lead_id = _save_lead(asin, cost_price, metrics, verdict, rationale, source_detail=source_detail)

            results.append({
                "asin": asin,
                "verdict": verdict,
                "rationale": rationale,
                "deep_dive_fired": deep_dive_fired,
                "error": error,
                "lead_id": lead_id,
                "title": metrics.get("title") if metrics else None,
                "keepa_estimate_roi": metrics.get("keepa_estimate_roi") if metrics else None,
                "keepa_estimate_profit": metrics.get("keepa_estimate_profit") if metrics else None,
            })

    return results


def _parse_bulk_input(text: str) -> list[tuple[str, float | None]]:
    """
    One ASIN per line -- "ASIN" alone, or "ASIN,cost"/"ASIN cost" for a
    known cost price (comma or whitespace separated; whichever's
    present). Blank lines skipped. A line with an unparseable cost
    keeps the ASIN with cost_price=None rather than dropping the whole
    line -- same "don't lose a lead to a formatting slip" instinct as
    everywhere else user-typed input feeds this app.
    """
    items = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = [p for p in re.split(r"[,\s]+", line) if p]
        asin = parts[0].strip().upper()

        cost_price = None
        if len(parts) > 1:
            try:
                cost_price = float(parts[1])
            except ValueError:
                cost_price = None

        items.append((asin, cost_price))

    return items


@router.get("/verdict")
def verdict_page(request: Request, asin: str = "", cost_price: str = "", source_detail: str = ""):
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
        metrics, verdict, rationale, _deep_dive_fired, error = _run_verdict_check(
            asin, parsed_cost, source_detail=source_detail.strip() or None
        )

    return templates.TemplateResponse(
        request=request,
        name="verdict.html",
        context={
            "request": request,
            "asin": asin,
            "cost_price": cost_price,
            "source_detail": source_detail,
            "metrics": metrics,
            "verdict": verdict,
            "rationale": rationale,
            "error": error,
            "bulk_results": None,
            "max_bulk_asins": MAX_BULK_ASINS,
        }
    )


@router.post("/verdict/bulk")
def verdict_bulk_page(request: Request, asins_text: str = Form(...), source_detail: str = Form("")):
    items = _parse_bulk_input(asins_text)[:MAX_BULK_ASINS]

    bulk_results = _run_bulk_verdict_check(items, source_detail=source_detail.strip() or None) if items else []

    return templates.TemplateResponse(
        request=request,
        name="verdict.html",
        context={
            "request": request,
            "asin": "",
            "cost_price": "",
            "source_detail": "",
            "metrics": None,
            "verdict": None,
            "rationale": None,
            "error": None,
            "bulk_results": bulk_results,
            "max_bulk_asins": MAX_BULK_ASINS,
        }
    )


class VerdictRequest(BaseModel):
    asin: str
    cost_price: float | None = None
    source_detail: str | None = None


@router.post("/api/verdict")
def api_verdict(body: VerdictRequest):
    asin = body.asin.strip().upper()
    metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check(
        asin, body.cost_price, source_detail=body.source_detail
    )

    if error:
        return {"asin": asin, "error": error}

    return {
        "asin": asin,
        "verdict": verdict,
        "rationale": rationale,
        "deep_dive_fired": deep_dive_fired,
        "metrics": metrics,
    }


class BulkVerdictItem(BaseModel):
    asin: str
    cost_price: float | None = None


class BulkVerdictRequest(BaseModel):
    items: list[BulkVerdictItem]
    source_detail: str | None = None


@router.post("/api/verdict/bulk")
def api_verdict_bulk(body: BulkVerdictRequest):
    items = [(i.asin.strip().upper(), i.cost_price) for i in body.items[:MAX_BULK_ASINS]]
    return {"results": _run_bulk_verdict_check(items, source_detail=body.source_detail)}
