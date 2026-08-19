from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.oa_lookup_service import OaLookupService
from app.services import serpapi_client

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/oa-lookup")
def oa_lookup_page(request: Request, asin: str = "", query: str = ""):
    asin = asin.strip().upper()

    product = None
    category_name = ""
    suggested_query = ""
    candidates = None
    not_found = False

    if asin:
        product, category_name = OaLookupService.get_baseline(asin)

        if product is None:
            not_found = True
        else:
            suggested_query = OaLookupService.suggest_query(product.title)

            # Only actually search once the user has confirmed/edited
            # the query -- avoids burning a SerpApi call just from
            # entering an ASIN.
            if query:
                candidates = OaLookupService.search_candidates(product, category_name, query)

    account_status = serpapi_client.get_account_status()

    return templates.TemplateResponse(
        request=request,
        name="oa_lookup.html",
        context={
            "request": request,
            "asin": asin,
            "product": product,
            "category_name": category_name,
            "not_found": not_found,
            "query": query or suggested_query,
            "candidates": candidates,
            "searches_left": account_status.get("plan_searches_left"),
        }
    )
