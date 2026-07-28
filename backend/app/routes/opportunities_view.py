import re
import io

import pandas as pd
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.templating import Jinja2Templates

from app.services.brand_scan_service import BrandScanService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Matches Amazon ASINs: real ASINs overwhelmingly start with "B0"
# followed by 8 more alphanumeric characters. Requiring that prefix
# (rather than matching any 10-character alphanumeric token) avoids
# false-positive matches from random words or model numbers in a
# messy CSV's title column. Works whether the source is a plain
# one-per-line list, a comma-separated list, or a full CSV export
# with other columns/headers -- it just pulls out anything
# ASIN-shaped and ignores the rest.
ASIN_PATTERN = re.compile(r"\b[Bb]0[A-Za-z0-9]{8}\b")


def _extract_asins(text: str) -> list:
    found = ASIN_PATTERN.findall(text)

    seen = set()
    asins = []

    for asin in found:
        asin = asin.upper()

        if asin in seen:
            continue

        seen.add(asin)
        asins.append(asin)

    return asins


def _read_uploaded_text(filename: str, raw_bytes: bytes) -> str:
    """
    .xlsx/.xls are binary (zipped) formats, not plain text -- they
    need a real spreadsheet reader, not just decoding the bytes.
    Read every cell into one text blob (header=None so a genuine
    ASIN in row 1 isn't mistaken for a column header and dropped) and
    let the same ASIN regex pull out what it needs, same as CSV/txt.
    """
    name = (filename or "").lower()

    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(io.BytesIO(raw_bytes), header=None, dtype=str)
        values = df.values.flatten()
        return "\n".join(str(v) for v in values if pd.notna(v))

    return raw_bytes.decode("utf-8", errors="ignore")


def _apply_profit_filter(result, profitable_only):
    """
    No longer removes opportunities from the result -- the full list
    is always kept, and unprofitable ones are hidden CLIENT-SIDE via
    CSS/JS instead (see discovery.html). Re-scanning just to change
    what's displayed would waste tokens and could even get blocked by
    the recently-scanned cooldown. This just counts how many would be
    hidden initially, for the toggle's starting state and messaging.
    """
    hidden_count = 0

    if result and not result.get("error"):
        hidden_count = sum(
            1 for o in result["opportunities"]
            if o["product"]["profit"] <= 0 and o["product"]["profit_90d"] <= 0
        )

    return result, hidden_count


@router.get("/discovery")
def discovery(request: Request, brand: str = "", limit: int = 20,
              profitable_only: bool = True, force_rescan: bool = False):
    result = None
    hidden_count = 0

    if brand:
        scanner = BrandScanService()
        result = scanner.scan(brand, limit=limit, force_rescan=force_rescan)
        result, hidden_count = _apply_profit_filter(result, profitable_only)

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
            "is_upload": False,
        }
    )


@router.post("/discovery/upload")
async def discovery_upload(
    request: Request,
    file: UploadFile = File(...),
    list_name: str = Form(""),
    limit: int = Form(200),
    profitable_only: bool = Form(True),
    force_rescan: bool = Form(False),
):
    raw_bytes = await file.read()

    try:
        text = _read_uploaded_text(file.filename, raw_bytes)
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="discovery.html",
            context={
                "request": request,
                "brand": list_name or (file.filename or "uploaded_list"),
                "limit": limit,
                "profitable_only": profitable_only,
                "force_rescan": force_rescan,
                "hidden_count": 0,
                "result": {
                    "error": f"Couldn't read that file: {exc}. "
                             f"Expected a .txt, .csv, or .xlsx file.",
                    "count": 0, "opportunities": [],
                },
                "is_upload": True,
            }
        )

    asins = _extract_asins(text)

    label = list_name.strip() or (file.filename or "uploaded_list")

    result = None
    hidden_count = 0

    if asins:
        scanner = BrandScanService()
        result = scanner.scan(label, limit=limit, force_rescan=force_rescan, asins=asins)
        result, hidden_count = _apply_profit_filter(result, profitable_only)
    else:
        result = {
            "error": "No ASIN-shaped values found in that file. "
                     "Expected 10-character codes like B0EXAMPLE1, one per line or in a CSV column.",
            "count": 0, "opportunities": [],
        }

    return templates.TemplateResponse(
        request=request,
        name="discovery.html",
        context={
            "request": request,
            "brand": label,
            "limit": limit,
            "profitable_only": profitable_only,
            "force_rescan": force_rescan,
            "hidden_count": hidden_count,
            "result": result,
            "asins_found_in_upload": len(asins),
            "is_upload": True,
        }
    )