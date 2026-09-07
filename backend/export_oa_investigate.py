"""
CLI wrapper for the "OA to Investigate" Excel export (2026-09-07,
Tamara: "I want to be able to export the list of OA items to
investigate via an excel sheet"). The actual row-building/formatting
logic lives in app/services/oa_export_service.py, shared with the
in-app "Download Excel" button on the Review Queue's OA to Investigate
view (app/routes/review_queue.py) so there is exactly one place this
logic lives.

Run with `python export_oa_investigate.py [output_path]`.
"""
import sys

from app.services.oa_export_service import build_oa_investigate_workbook

OUTPUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "oa_to_investigate.xlsx"

buf, row_count = build_oa_investigate_workbook()
with open(OUTPUT_PATH, "wb") as f:
    f.write(buf.getvalue())

print(f"Wrote {row_count} rows to {OUTPUT_PATH}")
