from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository
from app.services.brand_scan_service import BrandScanService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.post("/watch/add")
def watch_add(asin: str = Form(...), title: str = Form(""), brand: str = Form(""),
              return_to: str = Form("/products")):
    ProductRepository.add_watch(asin, title=title, brand=brand)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/watch/remove")
def watch_remove(asin: str = Form(...), return_to: str = Form("/watchlist")):
    ProductRepository.remove_watch(asin)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/add")
def exclude_add(asin: str = Form(...), title: str = Form(""),
                return_to: str = Form("/products")):
    ProductRepository.add_exclusion(asin, title=title)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/remove")
def exclude_remove(asin: str = Form(...), return_to: str = Form("/exclusions")):
    ProductRepository.remove_exclusion(asin)
    return RedirectResponse(url=return_to, status_code=303)


@router.get("/watchlist")
def watchlist_page(request: Request, profitable_only: bool = True, force_rescan: bool = False):
    watched = ProductRepository.list_watched()
    result = None
    hidden_count = 0

    if watched:
        asins = [w.asin for w in watched]
        scanner = BrandScanService()
        result = scanner.scan("watchlist", limit=len(asins), force_rescan=force_rescan, asins=asins)

        if result and not result.get("error"):
            hidden_count = sum(
                1 for o in result["opportunities"]
                if o["product"]["profit"] <= 0 and o["product"]["profit_90d"] <= 0
            )

    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "request": request,
            "watched": watched,
            "result": result,
            "hidden_count": hidden_count,
            "profitable_only": profitable_only,
            "force_rescan": force_rescan,
        }
    )


@router.get("/exclusions")
def exclusions_page(request: Request):
    exclusions = ProductRepository.list_exclusions()

    return templates.TemplateResponse(
        request=request,
        name="exclusions.html",
        context={
            "request": request,
            "exclusions": exclusions,
        }
    )