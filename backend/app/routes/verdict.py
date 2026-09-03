import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import BaseModel

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.verdict_service import VerdictService
from app.services.anthropic_client import generate_verdict
from app.services.keepa_priority import KeepaPriority, ScanBusyError, DEFAULT_WAIT_SECONDS
from app.services.product_service import KeepaTokensExhaustedError
from app.services.verdict_run_service import VerdictRunService
from app.services.verdict_service import resolve_source_marketplace

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
    # Single-quoted attribute deliberately -- r"...\"..." in a raw
    # string literal keeps the backslash IN the resulting string
    # (Python's raw-string rule only stops it from ending the string
    # early, it doesn't strip it), so a double-quoted version here
    # would insert a literal backslash before each quote in the actual
    # HTML output. Confirmed live 2026-08-24: every consumer of this
    # filter (Verdict Checker, Lead detail, the merged Review Queue)
    # was rendering `class=\"vr-figure\"` instead of `class="vr-figure"`.
    highlighted = _FIGURE_PATTERN.sub(r"<strong class='vr-figure'>\1</strong>", escaped)
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


def _run_verdict_check_inner(
    asin: str, cost_price: float | None, source_marketplace: str | None = None,
):
    """
    The two-pass pipeline for ONE ASIN (2026-08-23, sourcing-agent
    brief section 5) -- assumes the caller has already entered
    KeepaPriority.high_priority() (see _run_verdict_check for a single
    ASIN, _run_bulk_verdict_check for a whole batch sharing ONE
    high-priority window rather than one per ASIN).

    Pass 1 (always): today's existing cheap baseline check -- one
    Keepa call.
    Pass 2 (only if pass 1 came back BUY or WATCH): VerdictService.
    add_deep_dive bolts a free SP-API live price cross-check onto
    pass 1's ALREADY-FETCHED metrics -- no second Keepa call (see that
    method's docstring: as of 2026-08-26 nothing about the Keepa
    request differs between a routine check and a deep dive, so
    re-querying would just pay tokens twice for identical data). Only
    re-runs Claude when the SP-API call actually returned something;
    otherwise there's no new evidence and the baseline verdict stands.
    The deep-dive verdict REPLACES the baseline one when it runs; an
    AVOID at baseline never gets a second look, since there's no
    cheaper way to already know it's not promising.

    source_marketplace (an EU code -- DE/FR/ES/IT) adds ONE more Keepa
    call inside pass 1, verifying the lead is actually buyable on the
    marketplace it would be sourced from: Amazon or an FBA seller on
    the buy box, Amazon not out of stock. None (an OA lead, or a source
    Atlas could not resolve) skips that check and costs nothing extra.
    See VerdictService.check_source_marketplace.

    Returns (metrics, verdict, rationale, deep_dive_fired, error).
    error is a user-facing string on failure; metrics/verdict/rationale
    are None only when error is set to the "no Keepa data" case --
    a verdict-generation failure still returns the baseline metrics.
    """
    try:
        metrics = VerdictService.compute_metrics(
            asin, cost_price=cost_price, source_marketplace=source_marketplace,
        )
    except KeepaTokensExhaustedError as exc:
        return None, None, None, False, str(exc)

    if metrics is None:
        return None, None, None, False, "Keepa has no data for this ASIN."

    # Sourcing agent brief step 7 -- computed once per ASIN (nothing
    # they depend on changes between the baseline and deep-dive passes
    # below) and threaded into every generate_verdict call, then
    # stamped onto whichever metrics dict actually ends up persisted
    # so the Review Queue can show what the verdict was weighed against.
    #
    # Two separate signals, deliberately not merged (2026-08-27):
    # similar_rejections is THIS ASIN's own rejection history, and
    # brand_gating is a hard fact off the Gated Brands list. What used
    # to sit between them -- "some other ASIN from this brand was
    # rejected" -- was dropped as false signal; see
    # VerdictService.get_similar_rejections.
    similar_rejections = VerdictService.get_similar_rejections(asin)
    metrics["similar_rejections"] = similar_rejections

    brand_gating = VerdictService.get_brand_gating(
        metrics.get("brand"), metrics.get("category_name")
    )
    metrics["brand_gating"] = brand_gating

    try:
        verdict, rationale = generate_verdict(
            metrics, va_financials=None, similar_rejections=similar_rejections,
            brand_gating=brand_gating,
        )
    except Exception as exc:
        return metrics, None, None, False, f"Verdict generation failed: {exc}"

    deep_dive_fired = False

    if verdict in ("BUY", "WATCH"):
        VerdictService.add_deep_dive(metrics, asin)

        if metrics["deep_dive"]:
            try:
                deep_verdict, deep_rationale = generate_verdict(
                    metrics, va_financials=None, similar_rejections=similar_rejections,
                    brand_gating=brand_gating,
                )
                verdict, rationale = deep_verdict, deep_rationale
                deep_dive_fired = True
            except Exception as exc:
                # Deep-dive re-verdict failed -- keep the baseline
                # result rather than losing an otherwise-good check to
                # a second-pass hiccup (e.g. a transient Claude error).
                # metrics still carries the SP-API evidence even though
                # the rationale text wasn't regenerated with it.
                print(f"Deep-dive verdict failed for {asin}, keeping baseline result: {exc}")

    return metrics, verdict, rationale, deep_dive_fired, None


def _save_lead(
    asin: str, cost_price: float | None, metrics: dict, verdict: str, rationale: str,
    source_detail: str | None = None, source: str = "manual",
    source_marketplace: str | None = None,
) -> int:
    """
    Persists a Lead row, per spec section 3. Returns its id.

    source defaults to "manual" (the Verdict Checker's own single/bulk
    ASIN entry) but the Shortlist sweep (app/routes/shortlist.py)
    passes "shortlist" -- a third, distinct value from the original
    "manual" | "sheet" pair, so a shortlist-originated lead is
    traceable back to the pipeline that surfaced it rather than looking
    like something a human typed in by hand.

    A resolved source_marketplace also implies sourcing_type="A2A" -- a
    lead bought off an EU Amazon listing is A2A by definition, and the
    Verdict Checker form has no separate OA/A2A field to ask for. Left
    NULL otherwise rather than guessing "OA", since a manual check with
    no marketplace could be either.

    UPDATES an existing still-pending (decision IS NULL) Lead for the
    same (asin, source) in place rather than always inserting a new row
    (2026-09-02, fixing a real duplicate-Review-Queue bug: re-checking
    the same ASIN through Verdict Checker before the earlier check was
    ever reviewed just kept piling up new rows forever -- one real ASIN
    got checked 3 times in 9 minutes, leaving two stale WATCH duplicates
    sitting in the queue even after the newest check was already
    reviewed as BUY). Scoped to the SAME source deliberately -- a manual
    check and a sheet lead for the same ASIN are still treated as two
    independent leads, matching ReviewQueueService._flag_conflicts' own
    existing assumption that different sources can legitimately
    disagree.
    """
    db = SessionLocal()
    try:
        lead = (
            db.query(Lead)
            .filter(Lead.asin == asin, Lead.source == source, Lead.decision.is_(None))
            .order_by(Lead.id.desc())
            .first()
        )

        if lead is None:
            lead = Lead(asin=asin, source=source)
            db.add(lead)

        lead.source_marketplace = source_marketplace or None
        lead.sourcing_type = "A2A" if source_marketplace else None
        lead.va_cost_price = cost_price
        lead.status = "analyzed"
        lead.verdict = verdict
        lead.rationale = rationale
        lead.keepa_metrics = json.dumps(metrics)
        lead.analyzed_at = datetime.now(timezone.utc)
        lead.source_detail = source_detail or None

        db.commit()
        db.refresh(lead)
        return lead.id
    finally:
        db.close()


def _run_verdict_check(
    asin: str, cost_price: float | None, source_detail: str | None = None,
    source_marketplace: str | None = None,
):
    """
    Single-ASIN entry point -- wraps _run_verdict_check_inner in its
    own KeepaPriority window and persists the result. Shared by the
    /verdict page and POST /api/verdict.

    source_marketplace is resolved from what the user actually
    supplied, falling back to source_detail -- pasting an amazon.de
    product URL into the "where did this cost come from" box is the
    normal way an A2A lead gets entered, so there is no reason to make
    them pick the country a second time from a dropdown.

    The KeepaPriority window is time-bounded (2026-08-27): this runs
    inside the user's own request, so waiting forever on an in-flight
    automated scan meant a check that simply never came back. A
    ScanBusyError becomes an ordinary user-facing error string, which
    the page already knows how to render.

    Returns (metrics, verdict, rationale, deep_dive_fired, error).
    """
    source_marketplace = resolve_source_marketplace(source_marketplace, source_detail)

    try:
        with KeepaPriority.high_priority(timeout=DEFAULT_WAIT_SECONDS):
            metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check_inner(
                asin, cost_price, source_marketplace=source_marketplace,
            )
    except ScanBusyError as exc:
        return None, None, None, False, str(exc)

    if error:
        return metrics, verdict, rationale, deep_dive_fired, error

    _save_lead(
        asin, cost_price, metrics, verdict, rationale,
        source_detail=source_detail, source_marketplace=source_marketplace,
    )
    return metrics, verdict, rationale, deep_dive_fired, None


def _run_bulk_verdict_check(
    items: list[tuple[str, float | None]], source_detail: str | None = None,
    source_marketplace: str | None = None,
) -> list[dict]:
    """
    SYNCHRONOUS bulk entry point, now used only by POST
    /api/verdict/bulk -- a programmatic caller that chose to wait. The
    browser-facing form goes through VerdictRunService.start_run
    instead (2026-08-27), because doing this inline is what made a
    bulk submission look like it did nothing: minutes of blocking with
    no output, and results that existed only in that one response.

    The whole batch shares ONE KeepaPriority window (per its own
    docstring: marking active once, not once per ASIN, is what
    actually bounds how long an in-flight low-priority scan waits
    before yielding). Returns one result dict per input ASIN, in order,
    each report-shaped for direct template/JSON use.

    source_detail, if given, applies to every lead in the batch -- a
    bulk submission is normally a set of ASINs someone found in one
    place (a single retailer's clearance page, one wholesaler list),
    so one shared note is more realistic than asking for one per line
    in a plain textarea.

    source_marketplace is shared across the batch for the same reason,
    and costs one extra Keepa call PER ASIN when set (see
    VerdictService.check_source_marketplace) -- so a 30-ASIN EU A2A
    batch is 30 extra calls, not one. That is the deliberate price of
    not shipping unbuyable leads into the review queue; leave it unset
    for OA batches, where the check does not apply anyway.
    """
    results = []
    source_marketplace = resolve_source_marketplace(source_marketplace, source_detail)

    with KeepaPriority.high_priority(timeout=DEFAULT_WAIT_SECONDS):
        for asin, cost_price in items:
            metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check_inner(
                asin, cost_price, source_marketplace=source_marketplace,
            )

            lead_id = None
            if not error:
                lead_id = _save_lead(
                    asin, cost_price, metrics, verdict, rationale,
                    source_detail=source_detail, source_marketplace=source_marketplace,
                )

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
            # Strip currency symbols (£/$/€) and thousands separators --
            # VA sheets get pasted in as-is and commonly carry a £ prefix.
            cleaned = re.sub(r"[£$€,]", "", parts[1])
            try:
                cost_price = float(cleaned)
            except ValueError:
                cost_price = None

        items.append((asin, cost_price))

    return items


@router.get("/verdict")
def verdict_page(
    request: Request, asin: str = "", cost_price: str = "", source_detail: str = "",
    source_marketplace: str = "", run_id: int = 0,
):
    """
    run_id selects which past bulk run to show below the forms.
    Defaults to the most recent one, so landing on /verdict after
    leaving mid-batch shows the batch still in progress rather than an
    empty page -- the specific gap that made a bulk scan feel like it
    had vanished. Same run-picker convention as /oa-discovery.
    """
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
            asin, parsed_cost,
            source_detail=source_detail.strip() or None,
            source_marketplace=source_marketplace.strip() or None,
        )

    runs = VerdictRunService.list_runs()
    selected_run, run_items = (None, [])

    # No explicit run_id -> show the newest run. Falling back to
    # runs[0] rather than nothing is what makes navigating back to
    # /verdict mid-batch land you on the running batch.
    target_run_id = run_id or (runs[0].id if runs else 0)

    if target_run_id:
        selected_run, run_items = VerdictRunService.get_run_with_items(target_run_id)

    return templates.TemplateResponse(
        request=request,
        name="verdict.html",
        context={
            "request": request,
            "asin": asin,
            "cost_price": cost_price,
            "source_detail": source_detail,
            "source_marketplace": source_marketplace,
            "metrics": metrics,
            "verdict": verdict,
            "rationale": rationale,
            "error": error,
            "max_bulk_asins": MAX_BULK_ASINS,
            "runs": runs,
            "selected_run": selected_run,
            "run_items": run_items,
        }
    )


@router.post("/verdict/bulk")
def verdict_bulk_page(
    asins_text: str = Form(...), source_detail: str = Form(""),
    source_marketplace: str = Form(""),
):
    """
    Starts the batch in the background and redirects immediately --
    the request no longer waits for a single Keepa or Claude call.

    303 + redirect (rather than rendering here) also means the results
    view is a plain GET with a run_id in the URL: refreshable,
    bookmarkable, and reachable again after navigating away, which the
    old render-into-the-POST-response version was not.

    An empty submission redirects back to a bare /verdict rather than
    creating a pointless zero-item run.
    """
    items = _parse_bulk_input(asins_text)[:MAX_BULK_ASINS]

    if not items:
        return RedirectResponse(url="/verdict", status_code=303)

    run_id = VerdictRunService.start_run(
        items,
        asins_text=asins_text,
        source_detail=source_detail.strip() or None,
        source_marketplace=source_marketplace.strip() or None,
    )

    return RedirectResponse(url=f"/verdict?run_id={run_id}", status_code=303)


class VerdictRequest(BaseModel):
    asin: str
    cost_price: float | None = None
    source_detail: str | None = None
    # "DE" | "FR" | "ES" | "IT" for an EU A2A lead, or omitted. Also
    # inferred from source_detail when that's an Amazon EU URL -- see
    # resolve_source_marketplace.
    source_marketplace: str | None = None


@router.post("/api/verdict")
def api_verdict(body: VerdictRequest):
    asin = body.asin.strip().upper()
    metrics, verdict, rationale, deep_dive_fired, error = _run_verdict_check(
        asin, body.cost_price,
        source_detail=body.source_detail,
        source_marketplace=body.source_marketplace,
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
    source_marketplace: str | None = None


@router.post("/api/verdict/bulk")
def api_verdict_bulk(body: BulkVerdictRequest):
    """
    Stays synchronous -- a programmatic caller asked for results and
    can hold the connection. The browser form does NOT come through
    here any more; it starts a background VerdictRun instead.
    """
    items = [(i.asin.strip().upper(), i.cost_price) for i in body.items[:MAX_BULK_ASINS]]

    try:
        results = _run_bulk_verdict_check(
            items, source_detail=body.source_detail, source_marketplace=body.source_marketplace,
        )
    except ScanBusyError as exc:
        return {"error": str(exc), "results": []}

    return {"results": results}


@router.get("/api/verdict/run/{run_id}")
def api_verdict_run(run_id: int):
    """
    Progress for one bulk run. Exists so the results page can be
    checked without reloading it -- and so "did that batch I started
    an hour ago ever finish?" is answerable at all, which it wasn't
    when a run left no trace outside its own response.
    """
    run, items = VerdictRunService.get_run_with_items(run_id)

    if run is None:
        return {"error": f"No verdict run with id {run_id}."}

    return {
        "run_id": run.id,
        "status": run.status,
        "error": run.error or None,
        "asins_submitted": run.asins_submitted,
        "asins_completed": run.asins_completed,
        "buy_count": run.buy_count,
        "watch_count": run.watch_count,
        "avoid_count": run.avoid_count,
        "failed_count": run.failed_count,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "items": [
            {
                "asin": i.asin,
                "status": i.status,
                "verdict": i.verdict or None,
                "error": i.error or None,
                "deep_dive_fired": i.deep_dive_fired,
                "lead_id": i.lead_id,
                "title": i.title or None,
                "keepa_estimate_roi": i.keepa_estimate_roi,
                "keepa_estimate_profit": i.keepa_estimate_profit,
            }
            for i in items
        ],
    }
