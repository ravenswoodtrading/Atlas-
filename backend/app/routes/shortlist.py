from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.shortlist_service import ShortlistService, DEFAULT_SHORTLIST_SIZE
from app.services.keepa_priority import KeepaPriority
from app.routes.verdict import _run_verdict_check_inner, _save_lead

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


def _already_pending_asins(asins: list[str]) -> set[str]:
    """
    ASINs already sitting unreviewed in the Lead Queue -- skipped so a
    sweep doesn't create a second active duplicate for something
    already awaiting a decision (the exact clutter cleaned out of the
    Lead Queue earlier today). An ASIN that was already reviewed
    (approved/rejected) is NOT skipped -- re-checking it live is fine,
    and get_similar_rejections will surface that history as context.
    """
    if not asins:
        return set()

    db = SessionLocal()
    try:
        rows = (
            db.query(Lead.asin)
            .filter(Lead.asin.in_(asins), Lead.status == "analyzed", Lead.decision.is_(None))
            .all()
        )
        return {row[0] for row in rows}
    finally:
        db.close()


def _run_shortlist_sweep(limit: int) -> list[dict]:
    """
    Deduped, ranked candidates from Discovery + Competitor Watch (see
    ShortlistService), the top `limit` re-checked LIVE through the same
    two-pass verdict pipeline the Verdict Checker uses (baseline, then
    an automatic deep dive on anything BUY/WATCH) -- this is what makes
    a candidate's staleness a non-issue: whatever gets promoted here is
    priced/scored fresh, not off the original scan's numbers.
    """
    targets = ShortlistService.get_shortlist_targets(limit)

    if not targets:
        return []

    skip_asins = _already_pending_asins([t["asin"] for t in targets])

    results = []

    with KeepaPriority.high_priority():
        for candidate in targets:
            asin = candidate["asin"]

            if asin in skip_asins:
                results.append({
                    "asin": asin,
                    "title": candidate["title"],
                    "skipped": True,
                    "verdict": None, "rationale": None, "deep_dive_fired": False,
                    "error": None, "lead_id": None,
                    "recommendation": candidate["recommendation"],
                    "seen_via_competitor_watch": candidate["seen_via_competitor_watch"],
                    "keepa_estimate_roi": None, "keepa_estimate_profit": None,
                })
                continue

            cost_price = candidate["best_source_cost_gbp"] or None
            source_detail = candidate["best_source_marketplace"] or None

            metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check_inner(asin, cost_price)

            lead_id = None
            if not error:
                lead_id = _save_lead(
                    asin, cost_price, metrics, verdict, rationale,
                    source_detail=source_detail, source="shortlist",
                )

            results.append({
                "asin": asin,
                "title": metrics.get("title") if metrics else candidate["title"],
                "skipped": False,
                "verdict": verdict,
                "rationale": rationale,
                "deep_dive_fired": deep_dive_fired,
                "error": error,
                "lead_id": lead_id,
                "recommendation": candidate["recommendation"],
                "seen_via_competitor_watch": candidate["seen_via_competitor_watch"],
                "keepa_estimate_roi": metrics.get("keepa_estimate_roi") if metrics else None,
                "keepa_estimate_profit": metrics.get("keepa_estimate_profit") if metrics else None,
            })

    return results


@router.get("/shortlist")
def shortlist_page(request: Request):
    pool_size = len(ShortlistService.get_candidate_pool())

    return templates.TemplateResponse(
        request=request,
        name="shortlist.html",
        context={
            "request": request,
            "pool_size": pool_size,
            "default_size": DEFAULT_SHORTLIST_SIZE,
            "results": None,
        },
    )


@router.post("/shortlist/run")
def shortlist_run(request: Request, batch_size: int = Form(DEFAULT_SHORTLIST_SIZE)):
    batch_size = max(1, min(batch_size, 100))

    results = _run_shortlist_sweep(batch_size)
    pool_size = len(ShortlistService.get_candidate_pool())

    return templates.TemplateResponse(
        request=request,
        name="shortlist.html",
        context={
            "request": request,
            "pool_size": pool_size,
            "default_size": DEFAULT_SHORTLIST_SIZE,
            "results": results,
        },
    )
