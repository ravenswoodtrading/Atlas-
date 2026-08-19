from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/help")
def help_page(request: Request):
    """
    Shared glossary -- the jargon terms used across every page (A2A,
    OA, gated, score vs confidence, margin vs ROI, etc), defined once
    here rather than repeated inline in each page's own help modal
    (see app/templates/_help.html). Linked from the topbar on every
    page, plus from every per-page help modal's footer.
    """
    return templates.TemplateResponse(
        request=request,
        name="help.html",
        context={"request": request},
    )
