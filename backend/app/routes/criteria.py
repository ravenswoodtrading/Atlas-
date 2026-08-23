from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates

from app.services.verdict_service import VerdictService
from app.services.anthropic_client import CRITERIA_DOC_PATH, propose_criteria_amendments

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Where approved proposals get written -- see criteria.md's own
# "Judgment notes" section for why markers rather than fuzzy text
# matching (a placeholder sentence could drift, these can't).
_MARKER_START = "<!-- BEGIN JUDGMENT NOTES -->"
_MARKER_END = "<!-- END JUDGMENT NOTES -->"


def _read_criteria_doc() -> str:
    if not CRITERIA_DOC_PATH.exists():
        return ""
    return CRITERIA_DOC_PATH.read_text(encoding="utf-8")


@router.get("/criteria/review")
def criteria_review_page(request: Request):
    """
    Sourcing agent brief step 8, part 2 -- on-demand (not scheduled,
    per user's explicit choice 2026-08-23) pattern review over past
    Lead rejections. GET just shows the current doc and how much
    rejection history exists to analyze; POST actually runs it.
    """
    rejections = VerdictService.get_all_reasoned_rejections()

    return templates.TemplateResponse(
        request=request,
        name="criteria_review.html",
        context={
            "request": request,
            "doc_text": _read_criteria_doc(),
            "rejection_count": len(rejections),
            "proposals": None,
            "analyzed": False,
            "approved_count": None,
        },
    )


@router.post("/criteria/review")
def criteria_review_run(request: Request):
    """
    Runs propose_criteria_amendments over every reasoned rejection on
    file and shows the proposals for approval -- nothing is written to
    criteria.md by this step; see criteria_review_approve.
    """
    rejections = VerdictService.get_all_reasoned_rejections()
    proposals = propose_criteria_amendments(rejections)

    return templates.TemplateResponse(
        request=request,
        name="criteria_review.html",
        context={
            "request": request,
            "doc_text": _read_criteria_doc(),
            "rejection_count": len(rejections),
            "proposals": proposals,
            "analyzed": True,
            "approved_count": None,
        },
    )


@router.post("/criteria/review/approve")
def criteria_review_approve(request: Request, approved: list[str] = Form(default=[])):
    """
    The ONLY place criteria.md ever gets written by this feature --
    appends whatever the user explicitly checked on the review page
    into the Judgment notes section, between the marker comments.
    Never called automatically; a rejected/unchecked proposal is simply
    dropped (the user can always re-run the analysis later).
    """
    if approved:
        doc_text = _read_criteria_doc()

        if _MARKER_START in doc_text and _MARKER_END in doc_text:
            before, _, rest = doc_text.partition(_MARKER_START)
            existing, _, after = rest.partition(_MARKER_END)

            new_lines = "".join(f"- {line}\n" for line in approved)
            existing = existing.strip("\n")
            merged = (existing + "\n" + new_lines) if existing else new_lines

            doc_text = f"{before}{_MARKER_START}\n{merged}{_MARKER_END}{after}"
            CRITERIA_DOC_PATH.write_text(doc_text, encoding="utf-8")

    rejections = VerdictService.get_all_reasoned_rejections()

    return templates.TemplateResponse(
        request=request,
        name="criteria_review.html",
        context={
            "request": request,
            "doc_text": _read_criteria_doc(),
            "rejection_count": len(rejections),
            "proposals": None,
            "analyzed": False,
            "approved_count": len(approved),
        },
    )
