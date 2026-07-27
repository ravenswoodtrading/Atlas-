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