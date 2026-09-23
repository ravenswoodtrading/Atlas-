"""
Amazon listing restrictions ("are we gated on this ASIN?") -- 2026-09-20.

Wraps SPAPIClient.get_listing_restrictions with a cache so the free but
~0.5s-a-call check can sit in front of paid work. Three layers use it (see
each caller's own comment):

  1. BEFORE any Keepa spend -- BrandScanService drops restricted ASINs from
     brand-search pages (Scan Queue, Discovery) and EuDropScanService drops
     them before its own price check, so no token is spent on a product this
     account can't sell.
  2. At scoring time -- BrandScanService tags a restricted ASIN gated for
     every other caller (Watchlist, Replen, Competitor Watch...), which the
     existing GATED status already keeps out of the Review Queue, Discord
     pings and the BUY/CONSIDER counters.
  3. Read side -- the Review Queue hides restricted ASINs whatever route they
     arrived by (VA sheet leads, competitor finds...), and sweep_review_
     candidates() keeps the cache filled for what's waiting in it.

Why ASIN-level: an audit on 2026-09-20 found 33 of 340 recent BUY/CONSIDER
leads (~10%) restricted, 32 of them for brands on no gated list (Philips
items included). A brand list can't capture that.

Semantics that matter:
  * True = restricted, False = the account can list it, None = couldn't
    check (no answer, or out of time budget). None is NEVER treated as
    restricted OR as fine-and-cached: it passes through unchanged and is
    retried later.
  * Restricted answers are re-checked after RESTRICTED_TTL (approvals can be
    granted, and the point is to notice); sellable ones after SELLABLE_TTL.
"""
import time
from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import Lead, ListingRestriction, ProductRecord, SellerNewListing
from app.sp_api.client import get_sp_api_client

RESTRICTED_TTL = timedelta(days=7)
SELLABLE_TTL = timedelta(days=14)

# The Review Queue asks for the restricted set on every page build.
_RESTRICTED_SET_TTL_SECONDS = 60
_restricted_set_cache = {"marketplace": None, "at": 0.0, "value": frozenset()}

# What the sweep treats as "waiting in Atlas for a human decision".
_PENDING_SCAN_RECOMMENDATIONS = ("BUY", "CONSIDER", "LOW_CONFIDENCE", "LOW_SCORE", "PEAK_WINDOW")
_PENDING_SCAN_MAX_AGE_DAYS = 45


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RestrictionService:
    @staticmethod
    def details(asins: list, marketplace: str = "UK") -> dict:
        """{asin: {"restricted", "reason_code", "message", "checked_at"}} for
        the entries still fresh -- the same freshness rule as cached(), with
        the stored answer's detail and WHEN it was checked (a cache hit's
        checked_at is its original check, not now)."""
        if not asins:
            return {}

        now = _now()
        db = SessionLocal()

        try:
            fresh = {}
            for i in range(0, len(asins), 500):
                rows = (
                    db.query(ListingRestriction)
                    .filter(ListingRestriction.marketplace == marketplace,
                            ListingRestriction.asin.in_(asins[i:i + 500]))
                    .all()
                )
                for row in rows:
                    ttl = RESTRICTED_TTL if row.restricted else SELLABLE_TTL
                    if row.checked_at and now - row.checked_at < ttl:
                        fresh[row.asin] = dict(
                            restricted=bool(row.restricted), reason_code=row.reason_code or "",
                            message=row.message or "", checked_at=row.checked_at,
                        )
            return fresh

        finally:
            db.close()

    @staticmethod
    def cached(asins: list, marketplace: str = "UK") -> dict:
        """{asin: restricted(bool)} for the entries still fresh. Anything
        missing or expired is simply absent."""
        return {asin: d["restricted"] for asin, d in RestrictionService.details(asins, marketplace).items()}

    @staticmethod
    def _save(answers: dict, marketplace: str):
        """answers: {asin: {"restricted", "reason_code", "message"}}."""
        now = _now()
        db = SessionLocal()

        try:
            for asin, answer in answers.items():
                row = db.get(ListingRestriction, (asin, marketplace))
                if row is None:
                    row = ListingRestriction(asin=asin, marketplace=marketplace)
                    db.add(row)
                row.restricted = bool(answer["restricted"])
                row.reason_code = answer.get("reason_code") or ""
                row.message = answer.get("message") or ""
                row.checked_at = now
            db.commit()

        finally:
            db.close()

        _restricted_set_cache["at"] = 0.0  # the Review Queue's view is now stale

    @staticmethod
    def check(asins: list, marketplace: str = "UK", budget_seconds: float = None, sp_client=None) -> dict:
        """{asin: True (restricted) | False (can list) | None (couldn't check)}.
        Cache first; the API only for what's missing/expired, and only until
        `budget_seconds` runs out."""
        unique = list(dict.fromkeys(a for a in asins if a))
        result = {a: None for a in unique}
        result.update(RestrictionService.cached(unique, marketplace))
        missing = [a for a in unique if result[a] is None]

        if not missing:
            return result

        client = sp_client or get_sp_api_client()
        if client is None:
            return result

        deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None
        answers = {}

        for asin in missing:
            if deadline is not None and time.monotonic() >= deadline:
                break
            answer = client.get_listing_restrictions(asin, marketplace)
            if answer is None:
                continue
            answers[asin] = answer
            result[asin] = answer["restricted"]

        if answers:
            RestrictionService._save(answers, marketplace)

        return result

    @staticmethod
    def filter_unrestricted(asins: list, needed: int = None, budget_seconds: float = None,
                            marketplace: str = "UK", sp_client=None):
        """
        Returns (kept, restricted), both in the input order. `kept` is
        everything NOT known to be restricted -- including ASINs that
        couldn't be checked, which pass through unchanged.

        For paging callers: once `needed` ASINs are confirmed listable the
        API is no longer called (cached restricted ones are still dropped),
        so a page costs about as many calls as it will actually scan.
        """
        unique = list(dict.fromkeys(a for a in asins if a))
        known = RestrictionService.cached(unique, marketplace)
        client = sp_client or get_sp_api_client()
        deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None

        kept, restricted, answers = [], [], {}
        confirmed_listable = 0

        for asin in unique:
            status = known.get(asin)

            if (status is None and client is not None
                    and (needed is None or confirmed_listable < needed)
                    and (deadline is None or time.monotonic() < deadline)):
                answer = client.get_listing_restrictions(asin, marketplace)
                if answer is not None:
                    answers[asin] = answer
                    status = answer["restricted"]

            if status is True:
                restricted.append(asin)
                continue

            kept.append(asin)
            if status is False:
                confirmed_listable += 1

        if answers:
            RestrictionService._save(answers, marketplace)

        return kept, restricted

    @staticmethod
    def restricted_asin_set(marketplace: str = "UK") -> frozenset:
        """Every ASIN currently known to be restricted (fresh entries only),
        memoised for a minute -- the Review Queue asks on every page build."""
        cache = _restricted_set_cache

        if (cache["marketplace"] == marketplace and time.monotonic() - cache["at"] < _RESTRICTED_SET_TTL_SECONDS
                and cache["at"] > 0):
            return cache["value"]

        cutoff = _now() - RESTRICTED_TTL
        db = SessionLocal()

        try:
            rows = (
                db.query(ListingRestriction.asin)
                .filter(ListingRestriction.marketplace == marketplace,
                        ListingRestriction.restricted == True,  # noqa: E712
                        ListingRestriction.checked_at >= cutoff)
                .all()
            )
            value = frozenset(row[0] for row in rows)

        finally:
            db.close()

        cache.update(marketplace=marketplace, at=time.monotonic(), value=value)
        return value

    @staticmethod
    def sweep_review_candidates(limit: int = 60, budget_seconds: float = 120.0, marketplace: str = "UK") -> dict:
        """
        Fills the cache for ASINs waiting in Atlas for a human decision --
        unreviewed scan finds, undecided VA/manual leads, unreviewed
        competitor listings -- so the Review Queue's restricted-ASIN filter
        has an answer for them. Free (SP-API only), so it needs no scan lock.
        Never-checked ASINs come first because the cache-fresh ones are
        skipped; `limit` bounds one sweep.
        """
        cutoff = _now() - timedelta(days=_PENDING_SCAN_MAX_AGE_DAYS)
        candidates, seen = [], set()

        def add(rows):
            for (asin,) in rows:
                asin = (asin or "").strip().upper()
                if asin and asin not in seen:
                    seen.add(asin)
                    candidates.append(asin)

        db = SessionLocal()

        try:
            add(db.query(ProductRecord.asin)
                .filter(ProductRecord.review.is_(None),
                        ProductRecord.recommendation.in_(_PENDING_SCAN_RECOMMENDATIONS),
                        ProductRecord.scanned_at >= cutoff)
                .distinct().all())
            add(db.query(Lead.asin).filter(Lead.decision.is_(None), Lead.asin != "").distinct().all())
            add(db.query(SellerNewListing.asin)
                .filter(SellerNewListing.review.is_(None), SellerNewListing.dismissed == False)  # noqa: E712
                .distinct().all())

        finally:
            db.close()

        fresh = RestrictionService.cached(candidates, marketplace)
        todo = [a for a in candidates if a not in fresh]
        batch = todo[:limit]
        results = RestrictionService.check(batch, marketplace, budget_seconds=budget_seconds)

        return dict(
            waiting=len(candidates), already_known=len(fresh), checked=sum(1 for a in batch if results.get(a) is not None),
            restricted=sum(1 for a in batch if results.get(a) is True),
            couldnt_check=sum(1 for a in batch if results.get(a) is None), still_to_check=len(todo) - len(batch),
        )
