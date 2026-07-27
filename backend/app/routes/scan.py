from fastapi import APIRouter

from app.services.brand_scan_service import BrandScanService

router = APIRouter(
    prefix="/scan",
    tags=["Scanner"],
)


@router.get("/{brand}")
def scan_brand(brand: str):

    service = BrandScanService()

    return service.scan(brand)