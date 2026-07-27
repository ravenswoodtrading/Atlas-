from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.brand_scan_service import BrandScanService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/discovery")
def discovery(request: Request, brand: str = "", limit: int = 20,
              profitable_only: bool = True, force_rescan: bool = False):
    result = None
    hidden_count = 0

    if brand:
        scanner = BrandScanService()
        result = scanner.scan(brand, limit=limit, force_rescan=force_rescan)

        # Everything is still computed and saved to the database
        # regardless -- this only affects what's shown on this page,
        # since an unprofitable item today might be worth another
        # look later if prices/fees change.
        if result and not result.get("error") and profitable_only:
            all_opportunities = result["opportunities"]
            filtered = [o for o in all_opportunities if o["product"]["profit"] > 0]
            hidden_count = len(all_opportunities) - len(filtered)
            result = {**result, "opportunities": filtered, "count": len(filtered)}

    return templates.TemplateResponse(
        request=request,
        name="discovery.html",
        context={
            "request": request,
            "brand": brand,
            "limit": limit,
            "profitable_only": profitable_only,
            "force_rescan": force_rescan,
            "hidden_count": hidden_count,
            "result": result,
        }
    )
