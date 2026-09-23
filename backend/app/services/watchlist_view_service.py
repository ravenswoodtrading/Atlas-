"""
Watchlist page data (2026-09-21, Tamara: "very slow ... a constant problem").

The page used to run a LIVE Keepa scan of every watched ASIN whenever its 5-minute cache had expired, holding the
request open behind the process-wide scan lock (minutes, and real Keepa tokens, just to look at the page). Every scan
already saves a ProductRecord per ASIN, so the page now READS those (instant, free) and shows how old they are.
Fresh prices are an explicit action -- the "Refresh prices" button -- which runs the same scan in a background thread,
under the same manual-scan lock, while the page carries on showing the stored numbers.

What the old page showed and what this shows differ only in age: the numbers are those of each ASIN's latest scan (the
weekly safety net re-checks anything older than a week, and every manual rescan / scan queue pass saves a record too).
The one field the scan result had that a stored record does not is `offers_now` (only used by the "Amazon OOS at
review" badge, which also accepts a live Buy Box price), so it is left at 0.
"""
import json
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import aliased

from app.database.database import SessionLocal
from app.database.models import ProductRecord

# A stored record older than this is called out on the page as stale.
STALE_AFTER_HOURS = 48

_RECORD_FIELDS = (
    "asin", "title", "brand", "best_source_marketplace", "best_source_cost_gbp", "buy_box_now", "buy_box_90d",
    "profit", "roi", "profit_90d", "roi_90d", "monthly_sales", "monthly_sales_as_of", "sales_drops_30d",
    "fba_fee", "referral_fee", "category", "category_name", "image",
)


def _naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _product_dict(record) -> dict:
    product = {f: getattr(record, f) for f in _RECORD_FIELDS}
    product["offers_now"] = 0
    for numeric in ("best_source_cost_gbp", "buy_box_now", "buy_box_90d", "profit", "roi", "profit_90d", "roi_90d",
                    "monthly_sales", "sales_drops_30d", "fba_fee", "referral_fee"):
        product[numeric] = product[numeric] or 0
    for text in ("title", "brand", "best_source_marketplace"):
        product[text] = product[text] or ""
    return product


def _report_dict(record) -> dict:
    report = {}
    if record.report_json:
        try:
            parsed = json.loads(record.report_json)
            if isinstance(parsed, dict):
                report = parsed
        except (ValueError, TypeError):
            report = {}
    # The columns are the source of truth for the two fields every row needs, whatever the JSON says.
    report["recommendation"] = record.recommendation or report.get("recommendation") or "IGNORE"
    report["score"] = record.score if record.score is not None else report.get("score", 0)
    return report


def latest_records(db, asins) -> dict:
    """asin -> newest ProductRecord (newest scanned_at, then newest id) for the given ASINs."""
    newer = aliased(ProductRecord)
    latest_id = (
        db.query(newer.id).filter(newer.asin == ProductRecord.asin)
        .order_by(newer.scanned_at.desc(), newer.id.desc()).limit(1).correlate(ProductRecord).scalar_subquery()
    )
    out = {}
    asins = list(asins)
    for start in range(0, len(asins), 400):
        chunk = asins[start:start + 400]
        for r in db.query(ProductRecord).filter(ProductRecord.id == latest_id, ProductRecord.asin.in_(chunk)).all():
            out[r.asin] = r
    return out


def stored_result(asins, now=None) -> dict:
    """
    A scan-result-shaped dict built from each ASIN's latest stored record -- no Keepa, no scan lock:
      opportunities        [{"product": {...}, "report": {...}}], best score first (same fields the page reads)
      count                how many watched ASINs have a stored scan
      missing              ASINs that have never been scanned (nothing to show)
      newest / oldest      scanned_at of the freshest / stalest record shown
      stale                how many are older than STALE_AFTER_HOURS
    """
    now = now or _naive_utc_now()
    asins = [a for a in dict.fromkeys(asins) if a]
    db = SessionLocal()
    try:
        records = latest_records(db, asins)
        db.expunge_all()
    finally:
        db.close()

    ordered = sorted(records.values(),
                     key=lambda r: (-(r.score or 0), -max(r.roi or 0, r.roi_90d or 0), r.asin))
    scanned = [r.scanned_at for r in ordered if r.scanned_at]
    stale = sum(1 for r in ordered if not r.scanned_at or (now - r.scanned_at).total_seconds() > STALE_AFTER_HOURS * 3600)
    return {
        "error": None,
        "count": len(ordered),
        "tokens_remaining": None,
        "skipped_recently_scanned": 0,
        "opportunities": [{"product": _product_dict(r), "report": _report_dict(r)} for r in ordered],
        "missing": [a for a in asins if a not in records],
        "newest": max(scanned) if scanned else None,
        "oldest": min(scanned) if scanned else None,
        "stale": stale,
    }


# --- background refresh ----------------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_STATE = {"running": False, "started_at": None, "finished_at": None, "force": False, "error": None,
          "checked": None, "tokens_remaining": None}


def refresh_state() -> dict:
    """A copy of the current/last refresh's state, for the page banner."""
    with _STATE_LOCK:
        return dict(_STATE)


def start_refresh(asins, force: bool = False) -> bool:
    """
    Starts ONE background rescan of `asins` (the same scan the page used to run inline: same scanner, same usage
    category, same manual-scan lock). Returns False, doing nothing, if one is already running.
    """
    asins = [a for a in dict.fromkeys(asins) if a]
    if not asins:
        return False
    with _STATE_LOCK:
        if _STATE["running"]:
            return False
        _STATE.update({"running": True, "started_at": _naive_utc_now(), "finished_at": None, "force": bool(force),
                       "error": None, "checked": None, "tokens_remaining": None})

    def work():
        from app.services.brand_scan_service import BrandScanService
        from app.services.scan_coordinator import ScanCoordinator

        error, result = None, None
        try:
            ScanCoordinator.acquire_for_manual_scan()
            try:
                result = BrandScanService(usage_category="watchlist").scan(
                    "watchlist", limit=len(asins), force_rescan=bool(force), asins=asins)
            finally:
                ScanCoordinator.release_after_manual_scan()
            if result and result.get("error"):
                error = str(result["error"])
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            print(f"Watchlist refresh failed: {error}")
        finally:
            with _STATE_LOCK:
                _STATE.update({
                    "running": False, "finished_at": _naive_utc_now(), "error": error,
                    "checked": (result or {}).get("count") if result else None,
                    "tokens_remaining": (result or {}).get("tokens_remaining") if result else None,
                })

    threading.Thread(target=work, name="watchlist-refresh", daemon=True).start()
    return True
