from datetime import datetime, timezone

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import ScanQueueItem, AutomationSettings
from app.services.brand_scan_service import BrandScanService, WEEKLY_SAFETY_NET_RESERVE
from app.services.scan_coordinator import ScanCoordinator
from app.services.activity_log import ActivityLog

# Placeholder for the legacy NOT NULL target_count column -- the queue
# no longer stops a brand once it hits a target (see class docstring
# below), so this value is never actually compared against anything.
# Kept only so existing rows / the DB schema don't need a migration to
# drop the column outright.
_LEGACY_TARGET_COUNT_PLACEHOLDER = 999_999

# Single source of truth for how often the background scheduler ticks
# (see main.py's _scan_queue_scheduler, which imports this rather than
# hardcoding its own copy) -- also used by estimate_revisit_minutes
# below so the "is my queue too long?" estimate always matches
# whatever the scheduler is actually doing.
TICK_INTERVAL_SECONDS = 60


class ScanQueueService:
    """
    Manages the automated scan queue: brands (optionally category-
    filtered) that the background scheduler cycles through
    continuously, round-robin, one Product Finder page at a time,
    using whatever Keepa token budget happens to be available on each
    check-in (see main.py's SCAN_QUEUE_INTERVAL_SECONDS -- deliberately
    short so this feels continuous rather than waiting long stretches
    between chances to spend refilled tokens).

    There's no "finished, stop" state any more. Once a brand's current
    page comes back with fewer than 100 raw results (genuinely out of
    NEW catalog to walk), next_page just wraps back to 0 and it starts
    another lap -- `exhausted` flips to True the first time that
    happens, as a "this brand has done at least one full lap" flag,
    not a stop sign. Looping back to page 0 constantly is cheap, not
    wasteful: BrandScanService's own RESCAN_COOLDOWN_HOURS skips
    re-querying (and re-spending tokens on) any ASIN it already has
    fresh data for, so a fast lap on a small/exhausted brand mostly
    costs nothing until real time has passed and prices are worth
    re-checking.

    `scanned_count` is repurposed as a lifetime running total (how
    many ASIN look-ups this item has ever caused), not a per-cycle
    progress counter toward `target_count` -- see
    ProductRepository.get_brand_performance for the more useful
    "how many of those actually turned into BUY/CONSIDER" breakdown
    the Scan Queue page shows instead of a progress bar.
    """

    @staticmethod
    def _get_settings(db) -> AutomationSettings:
        settings = db.get(AutomationSettings, 1)

        if settings is None:
            settings = AutomationSettings(id=1, paused=False)
            db.add(settings)
            db.commit()

        return settings

    @staticmethod
    def is_paused() -> bool:
        db = SessionLocal()

        try:
            return ScanQueueService._get_settings(db).paused
        finally:
            db.close()

    @staticmethod
    def set_paused(paused: bool):
        db = SessionLocal()

        try:
            settings = ScanQueueService._get_settings(db)
            settings.paused = paused
            db.commit()
        finally:
            db.close()

    @staticmethod
    def estimate_cadence(item_count: int) -> dict:
        """
        "Is my queue too long?" check for the Scan Queue page. The
        round-robin scheduler gives exactly one brand a turn per tick
        (see _next_round_robin_item), so with N brands queued, any
        given brand is revisited roughly every N ticks -- more brands
        doesn't risk overspending tokens (BrandScanService already
        gates that live), it just spreads each individual brand's
        attention thinner. This is a best-case estimate assuming every
        tick actually finds enough tokens to run -- competing
        background features (Competitor Watch, Lead Analysis) or your
        own manual scans can stretch the real-world gap further, which
        is called out in the returned note so this doesn't read as a
        precise guarantee.
        """
        revisit_minutes = (item_count * TICK_INTERVAL_SECONDS) / 60

        if item_count == 0:
            level, label = "secondary", "Queue is empty."
        elif revisit_minutes <= 60:
            level, label = "success", "Healthy cadence -- every brand gets revisited at least hourly."
        elif revisit_minutes <= 240:
            level, label = "warning", "Getting long -- each brand only gets revisited every few hours."
        else:
            level, label = "danger", "A lot of brands -- each one may only be revisited every several hours or more."

        return {
            "item_count": item_count,
            "revisit_minutes": round(revisit_minutes),
            "level": level,
            "label": label,
        }

    @staticmethod
    def list_items():
        db = SessionLocal()

        try:
            return (
                db.query(ScanQueueItem)
                .order_by(ScanQueueItem.position)
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def add_item(brand: str, category_ids: list = None):
        """
        Confirmed via direct testing that Keepa's Product Finder
        pagination is stable and non-overlapping (same page always
        returns the same ASINs, consecutive pages never repeat) --
        which means a BRAND NEW queue item for a brand/category
        combination that's been scanned before, starting at page 0
        like any fresh item, would walk straight back over the exact
        same top-of-catalogue products a previous item for that same
        brand already covered. Continuing from the highest next_page
        already reached by any past item for this exact brand +
        category combination avoids that -- new campaigns pick up
        where the last one left off instead of restarting from the
        top every time.
        """
        db = SessionLocal()

        try:
            # Normalized (strip + lowercase), not just stripped -- an
            # exact-string match against a differently-cased brand
            # (e.g. adding "playmobil" after an earlier "PLAYMOBIL")
            # would otherwise silently miss the previous campaign
            # below and reset to page 0 as if it were a brand new one.
            brand = brand.strip().lower()
            category_ids_str = ",".join(str(c) for c in category_ids) if category_ids else ""

            furthest_page = (
                db.query(func.max(ScanQueueItem.next_page))
                .filter(
                    ScanQueueItem.brand == brand,
                    ScanQueueItem.category_ids == category_ids_str,
                )
                .scalar()
            )

            existing_positions = [p[0] for p in db.query(ScanQueueItem.position).all()]
            next_position = (max(existing_positions) + 1) if existing_positions else 0

            item = ScanQueueItem(
                brand=brand,
                category_ids=category_ids_str,
                target_count=_LEGACY_TARGET_COUNT_PLACEHOLDER,
                position=next_position,
                next_page=furthest_page or 0,
            )
            db.add(item)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def delete_item(item_id: int):
        db = SessionLocal()

        try:
            item = db.get(ScanQueueItem, item_id)

            if item:
                db.delete(item)
                db.commit()
        finally:
            db.close()

    @staticmethod
    def move_item(item_id: int, direction: str):
        """
        direction: "up" or "down" -- swaps this item's position with
        its immediate neighbor in priority order. No-op at either end
        of the list.
        """
        db = SessionLocal()

        try:
            items = db.query(ScanQueueItem).order_by(ScanQueueItem.position).all()
            idx = next((i for i, it in enumerate(items) if it.id == item_id), None)

            if idx is None:
                return

            if direction == "up" and idx > 0:
                other_idx = idx - 1
            elif direction == "down" and idx < len(items) - 1:
                other_idx = idx + 1
            else:
                return

            items[idx].position, items[other_idx].position = (
                items[other_idx].position,
                items[idx].position,
            )
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _next_round_robin_item(db, settings) -> ScanQueueItem | None:
        """
        Picks the next item to run this tick, cycling through ALL
        queued items in position order instead of always restarting
        from the highest-priority one -- every brand gets a turn in
        the token budget each lap round the queue, regardless of how
        many pages any one brand's catalog needs.

        settings.last_scan_queue_item_id is the id of whichever item
        was picked (and attempted) on the previous tick. If it's no
        longer in the queue (deleted, or this is the very first tick),
        falls back to the highest-priority item.
        """
        items = (
            db.query(ScanQueueItem)
            .order_by(ScanQueueItem.position)
            .all()
        )

        if not items:
            return None

        if settings.last_scan_queue_item_id is not None:
            ids = [i.id for i in items]
            if settings.last_scan_queue_item_id in ids:
                last_index = ids.index(settings.last_scan_queue_item_id)
                return items[(last_index + 1) % len(items)]

        return items[0]

    @staticmethod
    def run_next_tick():
        """
        Called by the background scheduler on each interval. Runs ONE
        scan call (one Product Finder page) for the next item in the
        round-robin rotation, updates its progress, and wraps back to
        page 0 for another lap once the brand/category has genuinely
        run out of new results (a page came back with fewer than 100
        raw results -- see BrandScanService.scan). It never stops on
        its own -- pause the whole queue from this page if you want it
        to stand down, or delete individual brands you don't want
        tracked any more.

        Never raises -- scan() already swallows Keepa errors and
        returns an error/empty result instead of throwing. Returns a
        small dict describing what happened, for logging/status.
        """
        db = SessionLocal()

        try:
            settings = ScanQueueService._get_settings(db)

            if settings.paused:
                return {"skipped": "paused"}

            item = ScanQueueService._next_round_robin_item(db, settings)

            if item is None:
                return {"skipped": "queue empty"}

            # Record the cursor for next tick's rotation BEFORE any
            # early return below -- even a skipped/errored tick should
            # still advance the rotation past this item, or it would
            # keep getting re-picked every tick while contributing
            # nothing (e.g. stuck on a token error).
            settings.last_scan_queue_item_id = item.id
            db.commit()

            # Manual scans (Discovery, Replen "check now") always take
            # priority -- if one is already running, skip this tick
            # entirely rather than competing with it for tokens. The
            # scheduler just tries again next interval; nothing about
            # the item's progress is touched, so nothing is lost.
            if not ScanCoordinator.try_acquire_for_automated_tick():
                return {"item_id": item.id, "brand": item.brand, "skipped": "manual scan in progress"}

            try:
                category_ids = item.category_ids.split(",") if item.category_ids else None

                # token_reserve -- see WEEKLY_SAFETY_NET_RESERVE's
                # docstring in brand_scan_service.py: this is the one
                # caller ticking often enough (every
                # TICK_INTERVAL_SECONDS) to otherwise starve the
                # once-a-day Replen/Watchlist safety-net tick of tokens
                # before it ever gets a turn.
                scanner = BrandScanService(token_reserve=WEEKLY_SAFETY_NET_RESERVE, usage_category="scan_queue")
                result = scanner.scan(
                    item.brand,
                    limit=100,
                    page=item.next_page,
                    category_ids=category_ids,
                )

                if not result.get("gated_brand_skip") and not result.get("error"):
                    ActivityLog.record(
                        "brand_search",
                        f"{item.brand} (automated, {result.get('asins_scanned', 0)} ASINs)",
                    )

                if result.get("gated_brand_skip"):
                    # Brand is on the gated brands list -- scan() already
                    # refused to spend a single token searching for more
                    # of it (see BrandScanService.scan Step 1). Flag the
                    # item so the Scan Queue page shows why it's not
                    # progressing, instead of leaving it looking like a
                    # stuck/errored item forever. Left in the rotation
                    # (not auto-deleted) so removing the brand from Gated
                    # Brands on the Exclusions page lets it resume
                    # scanning on its next turn with no extra step.
                    item.status = "gated"
                    db.commit()
                    return {"item_id": item.id, "brand": item.brand, "gated_brand_skip": True}

                if result.get("error"):
                    # Token buffer too low to even start -- leave the item
                    # exactly as it was (same next_page). The scheduler
                    # retries this exact same page next tick once tokens
                    # refill, so no progress is lost and nothing gets
                    # skipped.
                    return {"item_id": item.id, "brand": item.brand, "error": result["error"]}

                item.scanned_count = (item.scanned_count or 0) + (result.get("asins_scanned") or 0)
                item.last_run_at = datetime.now(timezone.utc)
                item.status = "in_progress"

                uk_ran_out = result.get("uk_ran_out", False)

                if uk_ran_out:
                    # Tokens ran out partway through THIS page -- don't
                    # advance next_page (that would permanently skip
                    # whatever was left unscanned on it). Next tick
                    # retries the same page; the rescan cooldown means
                    # ASINs already scanned this round get filtered out
                    # for free, so it naturally picks up the rest of the
                    # page instead of re-paying for what's already done.
                    db.commit()
                    return {
                        "item_id": item.id,
                        "brand": item.brand,
                        "asins_scanned_this_tick": result.get("asins_scanned") or 0,
                        "scanned_count": item.scanned_count,
                        "status": item.status,
                        "ran_out_of_tokens": True,
                    }

                raw_page_count = result.get("raw_page_count")
                exhausted_this_page = raw_page_count is not None and raw_page_count < 100

                if exhausted_this_page:
                    # Completed a full lap of this brand's real catalog
                    # -- start the next lap from the top rather than
                    # requesting pages Keepa has nothing left to return
                    # for.
                    item.next_page = 0
                    item.exhausted = True
                else:
                    item.next_page += 1

                db.commit()

                return {
                    "item_id": item.id,
                    "brand": item.brand,
                    "asins_scanned_this_tick": result.get("asins_scanned") or 0,
                    "opportunities_found": result.get("count") or 0,
                    "scanned_count": item.scanned_count,
                    "status": item.status,
                    "lap_completed": exhausted_this_page,
                }
            finally:
                ScanCoordinator.release_after_automated_tick()

        finally:
            db.close()
