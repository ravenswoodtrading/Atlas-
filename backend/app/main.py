import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles

from app.services.brand_scan_service import BrandScanService
from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.category_survey_service import CategorySurveyService
from app.services.scan_queue_service import ScanQueueService, TICK_INTERVAL_SECONDS
from app.services.seller_watch_service import SellerWatchService, RECLASSIFY_DAILY_CAP
from app.services.watchlist_service import WatchlistService
from app.services.replen_service import ReplenService
from app.services.review_queue_service import ReviewQueueService
from app.services.revisit_pool_service import RevisitPoolService
from app.services.scan_coordinator import ScanCoordinator
from app.services.lead_analysis_service import LeadAnalysisService
from app.services.discord_notifier import DiscordNotifier
from app.services.product_repository import ProductRepository
from app.services.signal_service import SignalService
from app.services.activity_log import ActivityLog
from app.services.verdict_run_service import VerdictRunService
from app.services.eu_a2a_freshness_service import recheck_pending_eu_a2a
from app.services.inventory_cleanup_service import InventoryCleanupService
from app.services.storage_fee_service import StorageFeeService

from app.database.base import Base
from app.database.database import engine
from app.database import models  # noqa: F401 -- registers ProductRecord with Base.metadata
from app.sp_api.client import get_sp_api_client

from app.routes import (
    dashboard, keepa, scan, analyse, opportunities_view, products, watchlist,
    categories, scan_queue, replen, competitors, review_queue, oa_lookup,
    verdict, leads, signals, oa_source_discovery, help as help_route,
    token_usage, criteria, shortlist, inventory_cleanup, storage_fee_watch,
    scan_intelligence, reports, actual_performance, va_performance,
)

# How often the background scan-queue scheduler makes one tick of
# progress (one Product Finder page for the next item in the
# round-robin rotation) -- see /scan-queue for the pause switch, if
# you want every token free for a manual session instead. A tick also
# always runs once immediately at startup, not just after the first
# interval.
#
# Deliberately short (was 30 minutes) so the queue behaves as
# genuinely continuous scanning that uses tokens as they refill,
# rather than sitting idle for long stretches between chances to
# spend a budget that's constantly topping up. This is safe to run
# this often because BrandScanService.scan() already checks the live
# token balance before spending anything on each call (MIN_TOKEN_
# BUFFER) -- a tick that finds too few tokens available just returns
# an "error" result immediately and tries again next minute, at
# effectively no cost.
#
# This value now lives in scan_queue_service.py as TICK_INTERVAL_
# SECONDS (imported above) -- it's also used there to estimate each
# brand's revisit cadence for the "is my queue too long?" banner on
# the Scan Queue page, so both places always agree on the real
# interval.
SCAN_QUEUE_INTERVAL_SECONDS = TICK_INTERVAL_SECONDS


async def _scan_queue_scheduler():
    while True:
        try:
            # BrandScanService.scan() makes blocking HTTP calls (via the
            # keepa library) -- run it off the event loop so it can't
            # stall every other request while a tick is in flight.
            from app.services.scan_schedule_service import refresh_weekly_reviews
            try:
                await asyncio.to_thread(refresh_weekly_reviews)
            except Exception as exc:
                print(f"Weekly scan review failed (scanning continues): {exc}")
            result = await asyncio.to_thread(ScanQueueService.run_next_tick)
            summary = result.get("skipped") or result.get("error") or (
                f"{result.get('brand', '?')}: {result.get('asins_scanned_this_tick', 0)} ASINs"
            )
            await asyncio.to_thread(ActivityLog.mark_tick, "scan_queue", SCAN_QUEUE_INTERVAL_SECONDS, summary)
        except Exception as exc:
            print(f"Scan queue tick failed: {exc}")

        await asyncio.sleep(SCAN_QUEUE_INTERVAL_SECONDS)


# How often the background competitor-watch scheduler checks tracked
# sellers' storefronts for new listings. Per-seller pausing is handled
# by TrackedSeller.active (see /competitors), not a separate global
# switch here. A check also always runs once immediately at startup,
# not just after the first interval.
#
# Changed from 2 HOURS 2026-09-04, Tamara's own explicit instruction --
# first "prioritise these, check regularly" (briefly set to 10 min),
# then corrected: "I don't need sellers checked so often... three times
# a day". 8h gives exactly 3 passes/day. Separately fixed the same day:
# SellerWatchService._fetch_storefronts now uses wait=True so each of
# those 3 passes reliably reaches every tracked seller (was silently
# starving whichever sellers came last in a fixed iteration order
# whenever tokens ran momentarily low) -- see that method's own
# docstring for the real bug this closed.
SELLER_WATCH_INTERVAL_SECONDS = 8 * 60 * 60


async def _seller_watch_scheduler():
    while True:
        try:
            # Same non-blocking "skip this tick if a manual scan is
            # already running" priority rule ScanQueueService's
            # automated tick uses (see ScanCoordinator) -- manual,
            # user-initiated scans (Discovery, Replen, Watchlist, or a
            # manual "Check now" on the Competitors page itself) always
            # win; this just tries again next interval instead of
            # competing with them for tokens.
            if ScanCoordinator.try_acquire_for_automated_tick():
                try:
                    # SellerWatchService.run_check() makes blocking Keepa
                    # calls -- run it off the event loop, same reason as
                    # the scan queue tick above.
                    result = await asyncio.to_thread(SellerWatchService.run_check)
                    summary = f"{result.get('checked', 0)} seller(s), {result.get('new_listings', 0)} new"
                    await asyncio.to_thread(
                        ActivityLog.mark_tick, "seller_watch", SELLER_WATCH_INTERVAL_SECONDS, summary,
                    )
                finally:
                    ScanCoordinator.release_after_automated_tick()
        except Exception as exc:
            print(f"Seller watch check failed: {exc}")

        await asyncio.sleep(SELLER_WATCH_INTERVAL_SECONDS)


# How often the weekly safety-net scheduler ticks -- DAILY, not
# weekly, even though the actual staleness window it checks for is 7
# days (see WEEKLY_RECHECK_STALE_HOURS below). A daily tick with a
# 7-day filter is more robust than a bare weekly timer: a weekly timer
# can drift or miss its window entirely across app restarts, while a
# daily tick that finds nothing stale yet is just a cheap no-op.
WEEKLY_RECHECK_TICK_SECONDS = 24 * 60 * 60
WEEKLY_RECHECK_STALE_HOURS = 24 * 7

# OA to Investigate archive threshold (2026-09-07, Tamara) -- see
# SellerWatchService.archive_stale_oa_investigate's own docstring.
OA_ARCHIVE_STALE_DAYS = 14


async def _weekly_recheck_scheduler():
    while True:
        retry_delay = WEEKLY_RECHECK_TICK_SECONDS
        try:
            # Same non-blocking "skip if a manual scan is already
            # running" priority rule as the other automated ticks --
            # this is a safety net for whatever the user HASN'T
            # manually checked recently, so it should never compete
            # with something they're actively doing right now.
            acquired = ScanCoordinator.try_acquire_for_automated_tick()
            if not acquired:
                retry_delay = 60
            if acquired:
                try:
                    watch_result = await asyncio.to_thread(WatchlistService.check_stale, WEEKLY_RECHECK_STALE_HOURS)
                    try:
                        await asyncio.to_thread(ReplenService.import_uploaded_actuals)
                    except Exception:
                        print('STK replen import unavailable; continuing scheduled checks of existing items.')
                    replen_result = await asyncio.to_thread(ReplenService.check_stale, 24)
                    # Added 2026-09-04, same scheduler/window -- see
                    # ReviewQueueService.recheck_stale_items' own
                    # docstring for the full reasoning (Tamara's own
                    # instruction: recheck stale unreviewed items rather
                    # than silently clearing or leaving them forever).
                    review_recheck_result = await asyncio.to_thread(
                        ReviewQueueService.recheck_stale_items, WEEKLY_RECHECK_STALE_HOURS,
                    )
                    # Added 2026-09-05, same scheduler/window -- second,
                    # narrower safety net for the BORDERLINE/NEEDS_
                    # ATTENTION backlog specifically (Tamara: "the less
                    # good leads should go altogether after say 15
                    # days"). Replaces the one-off bulk-recheck script
                    # from the same day, which burned hours of real
                    # Keepa tokens in an unsupervised while-loop with
                    # barely any net progress -- see
                    # ReviewQueueService.recheck_weak_leads' own
                    # docstring for the full incident and why this is a
                    # single bounded batch per day instead.
                    weak_lead_result = await asyncio.to_thread(
                        ReviewQueueService.recheck_weak_leads,
                    )
                    # Opportunity Engine 2.0, Phase 3A (2026-09-04) --
                    # targeted Revisit Pool: historically-profitable
                    # ASINs untouched by ANYTHING for 30+ days (the
                    # ASIN Re-Entry Audit's own 736-ASIN population),
                    # profit-primary ranked, top 25/day. Same
                    # ScanCoordinator-gated, same-scheduler-tick
                    # convention as the three calls above -- see
                    # RevisitPoolService's own module docstring for why
                    # this is deliberately NOT a new scanning
                    # subsystem. Every outcome is logged to RevisitLog
                    # regardless of whether it recovered, which is the
                    # actual point (see that model's own docstring).
                    revisit_result = await asyncio.to_thread(RevisitPoolService.run_batch)
                    # Added 2026-09-05, same scheduler/window -- real,
                    # confirmed bug fix (Tamara's own report on
                    # B09446293Y): a competitor detection classified
                    # within days of a genuine EU price dip can record
                    # "0 priced days" even though the dip is real and
                    # inside SourcingClassifier.RECENT_WINDOW_DAYS --
                    # Keepa's own historical price series for the last
                    # few days isn't always fully settled yet at
                    # classification time. Without this, a listing
                    # caught at that unlucky moment stays wrongly
                    # tagged "OA / unclear" forever -- the manual
                    # /competitors reclassify-all button already
                    # existed for this, but nothing ever triggered it
                    # automatically. Capped at RECLASSIFY_DAILY_CAP
                    # (same convention/value as MAX_STALE_RECHECK_PER_
                    # RUN) -- see SellerWatchService.reclassify_all's
                    # own docstring for why this is a bounded daily
                    # batch, not one uncapped run against the whole
                    # backlog.
                    reclassify_result = await asyncio.to_thread(
                        SellerWatchService.reclassify_all, RECLASSIFY_DAILY_CAP,
                    )
                    summary = (
                        f"watchlist: {watch_result.get('checked', 0)}/{watch_result.get('stale', 0)} stale, "
                        f"replen: {replen_result.get('checked', 0)}/{replen_result.get('stale', 0)} stale, "
                        f"review queue: {review_recheck_result.get('rechecked', 0)}/{review_recheck_result.get('stale_found', 0)} stale, "
                        f"{review_recheck_result.get('expired', 0)} expired, "
                        f"weak leads: {weak_lead_result.get('rechecked', 0)}/{weak_lead_result.get('weak_found', 0)} rechecked, "
                        f"{weak_lead_result.get('rejected', 0)} rejected, "
                        f"revisit pool: {revisit_result.get('scanned', 0)} revisited, "
                        f"{revisit_result.get('recovered', 0)} recovered, "
                        f"reclassify: {reclassify_result.get('processed', 0)} processed, "
                        f"{reclassify_result.get('flipped', 0)} flipped, "
                        f"{reclassify_result.get('remaining', 0)} remaining"
                    )
                    await asyncio.to_thread(
                        ActivityLog.mark_tick, "weekly_recheck", WEEKLY_RECHECK_TICK_SECONDS, summary,
                    )
                finally:
                    ScanCoordinator.release_after_automated_tick()

            # Deliberately OUTSIDE the ScanCoordinator lock above --
            # this spends no Keepa tokens (pure DB reads/deletes), so
            # it has no reason to wait for or compete with a manual
            # scan the way the token-spending check above does. Runs
            # every tick regardless of whether the lock was free.
            # (WatchlistService.prune_stale_auto_adds logs its own
            # ActivityLog entry when it actually removes anything --
            # nothing further to log here.)
            await asyncio.to_thread(WatchlistService.prune_stale_auto_adds)
            # OA to Investigate housekeeping (2026-09-07, Tamara: recurring
            # daily archive of anything >14 days old and never actioned)
            # -- see SellerWatchService.archive_stale_oa_investigate's own
            # docstring. Same zero-Keepa-cost, no-lock-needed reasoning as
            # prune_stale_auto_adds just above.
            archived = await asyncio.to_thread(SellerWatchService.archive_stale_oa_investigate, OA_ARCHIVE_STALE_DAYS)
            if archived:
                await asyncio.to_thread(ActivityLog.record, "oa_archive", f"{archived} stale OA/unclear listing(s) archived")
        except Exception as exc:
            print(f"Weekly recheck tick failed: {exc}")
            retry_delay = 60

        await asyncio.sleep(retry_delay)


# How often the lead-analysis background worker checks for VA-submitted
# (sheet) or manually-queued leads awaiting a Keepa+Claude verdict. Short
# interval, and no ScanCoordinator gating here -- this work IS high
# priority (see KeepaPriority); it's the automated scan pipeline that
# yields to it, not the other way around. A tick also always runs once
# immediately at startup, not just after the first interval.
LEAD_ANALYSIS_INTERVAL_SECONDS = 30


async def _lead_analysis_scheduler():
    while True:
        try:
            # LeadAnalysisService.process_queued_batch() makes blocking
            # Keepa + Claude calls -- run it off the event loop, same
            # reason as the other schedulers below.
            await asyncio.to_thread(LeadAnalysisService.process_queued_batch)
            await asyncio.to_thread(ActivityLog.mark_tick, "lead_analysis", LEAD_ANALYSIS_INTERVAL_SECONDS, "")
        except Exception as exc:
            print(f"Lead analysis tick failed: {exc}")

        await asyncio.sleep(LEAD_ANALYSIS_INTERVAL_SECONDS)


# VA Lead Sheet pull sync (2026-09-05) -- replaces the ngrok-dependent
# webhook design (see google_sheets_lead_sync.py's own module docstring).
# Configurable via .env rather than hardcoded (Tamara's explicit
# instruction) -- changing the schedule later is an .env edit, not a
# code change. Days: comma-separated 3-letter weekday codes (mon/tue/
# wed/thu/fri/sat/sun); hours: local 24h clock, start inclusive/end
# exclusive. Start hour moved 8->7 (Tamara, 2026-09-06) -- leads that
# came in overnight should be waiting first thing, not an hour later.
VA_LEAD_SYNC_INTERVAL_SECONDS = int(os.getenv("VA_LEAD_SYNC_INTERVAL_SECONDS", "3600"))
VA_LEAD_SYNC_DAYS = {d.strip().lower() for d in os.getenv("VA_LEAD_SYNC_DAYS", "mon,tue,wed,thu,fri").split(",") if d.strip()}
VA_LEAD_SYNC_START_HOUR = int(os.getenv("VA_LEAD_SYNC_START_HOUR", "7"))
VA_LEAD_SYNC_END_HOUR = int(os.getenv("VA_LEAD_SYNC_END_HOUR", "20"))
_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _va_lead_sync_should_run_now() -> bool:
    now = datetime.now()
    return (
        _WEEKDAY_CODES[now.weekday()] in VA_LEAD_SYNC_DAYS
        and VA_LEAD_SYNC_START_HOUR <= now.hour < VA_LEAD_SYNC_END_HOUR
    )


async def _va_lead_sheet_scheduler():
    """
    Real gap found and fixed (Tamara, 2026-09-06): the original version
    slept a fixed VA_LEAD_SYNC_INTERVAL_SECONDS from whenever it last
    ran, not aligned to the clock. Two consequences she didn't want:
    (1) the very first check after the window opens (7am) could land up
    to a full interval late, depending purely on what time Atlas
    happened to have been started the day before; (2) restarting Atlas
    loses no ground -- a fresh process always checks immediately on its
    first loop iteration, so "the first scan is at 7am, or whenever
    Atlas is (re)started if that's later" was already true for restarts,
    but NOT for a continuously-running server crossing into a new day.

    Fixed by aligning every sleep to the next wall-clock boundary that's
    a multiple of the interval since midnight (e.g. the next :00 for a
    3600s interval) instead of a fixed offset from last-run. Combined
    with the unconditional immediate check on every loop iteration
    (including the very first, right after Atlas starts), this means:
    a tick lands close to 7:00:00 on the dot once the window opens, and
    starting/restarting Atlas after 7am still checks immediately rather
    than waiting for the next aligned boundary.
    """
    while True:
        try:
            if _va_lead_sync_should_run_now():
                from app.services.google_sheets_lead_sync import pull_and_ingest_va_leads
                result = await asyncio.to_thread(pull_and_ingest_va_leads)
                await asyncio.to_thread(ActivityLog.mark_tick, "va_lead_sheet_sync", VA_LEAD_SYNC_INTERVAL_SECONDS, str(result))
        except Exception as exc:
            print(f"VA lead sheet sync tick failed: {exc}")

        now = datetime.now()
        seconds_since_midnight = now.hour * 3600 + now.minute * 60 + now.second
        seconds_to_next_boundary = VA_LEAD_SYNC_INTERVAL_SECONDS - (seconds_since_midnight % VA_LEAD_SYNC_INTERVAL_SECONDS)
        await asyncio.sleep(seconds_to_next_boundary)


# How often the background Signals scheduler automatically checks
# enabled signal queries for new candidates. Deliberately does NOT
# include price_spike yet -- its Keepa filter field was corrected
# 2026-08-21 (was the wrong field name AND the wrong sign; see
# ProductFinder.find_signal_candidates' docstring for the fix and the
# Keepa docs it's based on), but that fix is still only checked
# against Keepa's documentation, not a live result -- no Keepa/network
# access from the sandbox that made the fix. Run it once manually from
# /signals/queries' "Run now" button after restart and spot-check that
# the returned ASINs' Buy Box prices genuinely went UP (not down) on
# Keepa/SAS before adding "price_spike" to AUTOMATED_SIGNAL_TYPES below.
# stock_out (a confirmed Keepa field) and ceiling_recheck (no Product
# Finder call at all -- it just re-prices Atlas's own already-
# rejected ASINs) are both safe to automate now.
#
# 2h, matching SELLER_WATCH_INTERVAL_SECONDS -- same reasoning:
# storefronts/stock-outs don't need minute-by-minute polling, and
# this keeps the automated Signals checks token-cheap and consistent
# with the other "safety net" schedulers below rather than competing
# hard for tokens against manual work.
SIGNAL_SCHEDULER_INTERVAL_SECONDS = 2 * 60 * 60
AUTOMATED_SIGNAL_TYPES = ("stock_out", "ceiling_recheck")


async def _signal_scheduler():
    while True:
        try:
            # Same non-blocking "skip this tick if a manual scan is
            # already running" priority rule as seller_watch/weekly_
            # recheck above -- manual work (including a manual "Run
            # now" on /signals/queries, which uses the SAME
            # acquire_for_manual_scan lock) always wins; this just
            # tries again next interval instead of competing for
            # tokens.
            if ScanCoordinator.try_acquire_for_automated_tick():
                try:
                    queries = await asyncio.to_thread(ProductRepository.list_signal_queries, True)
                    checked = 0
                    total_new = 0

                    for query in queries:
                        if query.signal_type not in AUTOMATED_SIGNAL_TYPES:
                            continue

                        try:
                            # SignalService.run_check() makes blocking
                            # Keepa calls -- run it off the event loop,
                            # same reason as the other schedulers.
                            result = await asyncio.to_thread(SignalService().run_check, query.id)
                            checked += 1

                            if result.get("error"):
                                print(f"Signal query '{query.name}' failed: {result['error']}")
                            else:
                                total_new += result.get("new_matches", 0)
                                print(
                                    f"Signal query '{query.name}': "
                                    f"{result.get('new_matches', 0)} new match(es)."
                                )
                        except Exception as exc:
                            print(f"Signal query '{query.name}' raised: {exc}")

                    await asyncio.to_thread(
                        ActivityLog.mark_tick, "signals", SIGNAL_SCHEDULER_INTERVAL_SECONDS,
                        f"{checked} quer{'y' if checked == 1 else 'ies'} checked, {total_new} new match(es)",
                    )
                finally:
                    ScanCoordinator.release_after_automated_tick()
        except Exception as exc:
            print(f"Signal scheduler tick failed: {exc}")

        await asyncio.sleep(SIGNAL_SCHEDULER_INTERVAL_SECONDS)


# How often the EU A2A Review Queue freshness sweep runs (2026-08-29,
# Tamara's own instruction: "let's remove dead leads... regularly
# check this"). Daily, same reasoning as WEEKLY_RECHECK_TICK_SECONDS
# above -- a review queue's pending backlog doesn't need minute-by-
# minute polling, and this keeps SP-API calls (free, but still real
# wall-clock/rate-limited work) modest. No ScanCoordinator gating --
# spends zero Keepa tokens (SP-API only), so it has no reason to yield
# to a manual scan the way the token-spending automated ticks do.
EU_A2A_FRESHNESS_INTERVAL_SECONDS = 24 * 60 * 60


async def _eu_a2a_freshness_scheduler():
    while True:
        try:
            # recheck_pending_eu_a2a makes blocking SP-API HTTP calls --
            # run it off the event loop, same reason as the other
            # schedulers above.
            result = await asyncio.to_thread(recheck_pending_eu_a2a)
            summary = result.get("message") or (
                f"{result.get('checked', 0)} checked, {result.get('removed', 0)} removed as stale"
            )
            await asyncio.to_thread(
                ActivityLog.mark_tick, "eu_a2a_freshness", EU_A2A_FRESHNESS_INTERVAL_SECONDS, summary,
            )
        except Exception as exc:
            print(f"EU A2A freshness sweep failed: {exc}")

        await asyncio.sleep(EU_A2A_FRESHNESS_INTERVAL_SECONDS)


# How often the Out of Stock Cleanup sweep runs (2026-09-01, Tamara: "as
# automated as possible"). Daily, same reasoning as EU_A2A_FRESHNESS_
# INTERVAL_SECONDS just above -- a sold-out listing doesn't need
# minute-by-minute polling, and this is SP-API only (zero Keepa tokens),
# so no ScanCoordinator gating: it has no reason to yield to a manual
# scan the way the token-spending automated ticks do. Detection only --
# deletion always requires a human on the /inventory-cleanup page, never
# this scheduler (see InventoryCleanupService's own docstring).
INVENTORY_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


async def _inventory_cleanup_scheduler():
    while True:
        try:
            # detect_out_of_stock_candidates() makes blocking SP-API HTTP
            # calls (including a Reports API poll loop) -- run it off the
            # event loop, same reason as the other schedulers above.
            result = await asyncio.to_thread(InventoryCleanupService.detect_out_of_stock_candidates)
            summary = result.get("message") or (
                f"{result.get('checked', 0)} checked, {result.get('flagged', 0)} flagged, "
                f"{result.get('graduated', 0)} graduated, {result.get('restocked_cleared', 0)} cleared"
            )
            await asyncio.to_thread(
                ActivityLog.mark_tick, "inventory_cleanup", INVENTORY_CLEANUP_INTERVAL_SECONDS, summary,
            )
        except Exception as exc:
            print(f"Inventory cleanup sweep failed: {exc}")

        await asyncio.sleep(INVENTORY_CLEANUP_INTERVAL_SECONDS)


# Same reasoning as INVENTORY_CLEANUP_INTERVAL_SECONDS above -- daily is
# plenty for a report that's a monthly snapshot anyway (see
# SPAPIClient.get_storage_fee_charges), this is read-only reporting with
# no delete action ever (see StorageFeeWatch's own docstring), so no
# ScanCoordinator gating either.
STORAGE_FEE_WATCH_INTERVAL_SECONDS = 24 * 60 * 60


async def _storage_fee_scheduler():
    while True:
        try:
            # StorageFeeService.refresh() makes blocking SP-API HTTP
            # calls (two Reports API poll loops) -- run it off the
            # event loop, same reason as the other schedulers above.
            result = await asyncio.to_thread(StorageFeeService.refresh)
            summary = result.get("message") or (
                f"{result.get('checked', 0)} checked, {result.get('flagged_surcharge', 0)} "
                f"carrying a surcharge, {result.get('flagged_shift', 0)} flagged to shift"
            )
            await asyncio.to_thread(
                ActivityLog.mark_tick, "storage_fee_watch", STORAGE_FEE_WATCH_INTERVAL_SECONDS, summary,
            )
        except Exception as exc:
            print(f"Storage fee watch sweep failed: {exc}")

        await asyncio.sleep(STORAGE_FEE_WATCH_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A Verdict Checker bulk batch runs in a daemon thread, so a
    # restart kills it mid-run and leaves the run row claiming to still
    # be running. Close those out before serving anything, or the
    # results page auto-refreshes a batch that will never advance.
    try:
        VerdictRunService.reconcile_interrupted_runs()
    except Exception as exc:
        print(f"Verdict run reconciliation failed (non-fatal): {exc}")

    # A local preview should be able to load the real UI and database
    # without immediately starting every Keepa, Sheets and SP-API job.
    # This is opt-in via preview.bat; normal run.bat leaves the variable
    # unset, so Atlas automation keeps its existing behaviour.
    automation_enabled = os.getenv("ATLAS_AUTOMATION_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    background_tasks = []
    if automation_enabled:
        background_tasks = [
            asyncio.create_task(_scan_queue_scheduler()),
            asyncio.create_task(_seller_watch_scheduler()),
            asyncio.create_task(_weekly_recheck_scheduler()),
            asyncio.create_task(_lead_analysis_scheduler()),
            asyncio.create_task(_va_lead_sheet_scheduler()),
            asyncio.create_task(_signal_scheduler()),
            asyncio.create_task(_eu_a2a_freshness_scheduler()),
            asyncio.create_task(_inventory_cleanup_scheduler()),
            asyncio.create_task(_storage_fee_scheduler()),
        ]
    else:
        print("Atlas preview mode: background automation is paused.")
    yield
    for task in background_tasks:
        task.cancel()


app = FastAPI(title="Atlas", lifespan=lifespan)

# Real gap found live, 2026-09-07 (Tamara: "a logo on the tab so we can
# see atlas") -- app/static/css and app/static/images already existed as
# empty scaffolding directories, but nothing ever actually mounted them;
# base.html's new favicon <link> would have 404'd without this.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Creates any tables that don't exist yet (e.g. product_records) without
# touching existing ones. The old 'products' table (incompatible legacy
# schema) is left alone -- product_records is a separate table.
Base.metadata.create_all(bind=engine)
from app.database.reporting_schema import migrate_reporting_schema
migrate_reporting_schema(engine)


@app.middleware("http")
async def inject_sidebar_badges(request, call_next):
    """
    Computes the small "N new" counters shown next to a few sidebar
    links (currently just Signals -- see base.html) and stashes them
    on request.state, so base.html can read
    request.state.new_signals_count directly without every single
    route handler needing to add it to its own template context.
    Works because every route already passes "request" into its
    TemplateResponse context (confirmed across all of them) -- this
    reaches every page for free, with zero changes needed to any
    existing route file.

    Wrapped in try/except -- a DB hiccup computing a sidebar badge
    must never break the page it's decorating.
    """
    try:
        request.state.new_signals_count = ProductRepository.count_new_signal_matches()
    except Exception as exc:
        print(f"Sidebar badge count failed (non-fatal): {exc}")
        request.state.new_signals_count = 0

    return await call_next(request)

# NOTE: routes/add_product.py is still NOT registered -- it targets the
# old incompatible 'products' table. Manual add-a-product isn't wired
# up yet; only scan-based persistence via product_records is.
app.include_router(dashboard.router)
app.include_router(keepa.router)
app.include_router(scan.router)
app.include_router(analyse.router)
app.include_router(opportunities_view.router)
app.include_router(products.router)
app.include_router(watchlist.router)
app.include_router(categories.router)
app.include_router(scan_queue.router)
app.include_router(replen.router)
app.include_router(competitors.router)
app.include_router(review_queue.router)
app.include_router(oa_lookup.router)
app.include_router(verdict.router)
app.include_router(leads.router)
app.include_router(signals.router)
app.include_router(oa_source_discovery.router)
app.include_router(scan_intelligence.router)
app.include_router(reports.router)
app.include_router(actual_performance.router)
app.include_router(va_performance.router)
app.include_router(help_route.router)
app.include_router(token_usage.router)
app.include_router(criteria.router)
app.include_router(shortlist.router)
app.include_router(inventory_cleanup.router)
app.include_router(storage_fee_watch.router)


@app.get("/opportunities/{brand}")
def opportunities_for_brand(brand: str, limit: int = 20, force_rescan: bool = False):
    """
    Full A2A pipeline: find a brand's ASINs, price them across UK/DE/FR/ES/IT,
    apply fees, score with OpportunityEngine, and return ranked opportunities.
    By default, ASINs scanned recently for this brand are skipped to
    save tokens -- pass force_rescan=true to check everything anyway.
    """
    scanner = BrandScanService(usage_category="manual_api")
    return scanner.scan(brand, limit=limit, force_rescan=force_rescan)


@app.get("/categories/{brand}")
def categories_for_brand(brand: str, limit: int = 100):
    """
    Cheap survey of what categories a brand's catalog spans, using
    only UK lookups (1x token cost per ASIN, not 5x). Use this to
    decide what to add to app/config/exclusions.py BEFORE running
    full /opportunities scans.
    """
    survey = CategorySurveyService()
    return survey.survey(brand, limit=limit)


@app.get("/debug/{brand}")
def debug_brand(brand: str):
    """
    Returns the raw Keepa product in a JSON-safe format.
    """

    finder = ProductFinder()
    service = ProductService()

    asins = finder.find_brand(brand, limit=1, usage_category="debug")

    if not asins:
        return {"error": "No products found"}

    products = service.get_products([asins[0]], "UK", usage_category="debug")

    if not products:
        return {"error": "No Keepa product returned"}

    return jsonable_encoder(products[0])


@app.get("/debug/verify-sort/{brand}")
def debug_verify_sort(brand: str, category_ids: str = ""):
    """
    One-time sanity check for ProductFinder's current_SALES ascending
    sort (see ProductFinder.find_brand's docstring) -- NOT part of any
    normal app flow, only for eyeballing after first deploying the
    sort change. Visit e.g. /debug/verify-sort/philips in a browser
    once, check that current_SALES values in the response look
    low-and-increasing down the list rather than scattered or
    trending the wrong way, then there's no need to keep using this.
    """
    ids = [c.strip() for c in category_ids.split(",") if c.strip()] or None

    try:
        result = ProductFinder.verify_sales_rank_sort(brand, category_ids=ids)
        # jsonable_encoder here (inside the try) rather than just
        # returning result -- a value FastAPI can't natively JSON-
        # encode (e.g. a leftover numpy type from the keepa library)
        # would otherwise fail during Starlette's response step,
        # AFTER this function has already returned, which bypasses
        # this try/except entirely and produces a bare "Internal
        # Server Error" no matter what this route does.
        return jsonable_encoder(result)
    except Exception as exc:
        # Debug-only endpoint -- surface the real error in the JSON
        # response instead of a bare "Internal Server Error", since
        # this is meant to be checked from a browser with no access
        # to the server console/log.
        import traceback
        return {"error": str(exc), "trace": traceback.format_exc()}


@app.get("/debug/oa/mpn-check")
def debug_mpn_check(limit: int = 5, asins: str = ""):
    """
    ONE-TIME diagnostic for the OA Source Discovery module (see
    oa_source_discovery_service.py): does Keepa's raw UK product
    response actually include a manufacturer part number / model
    field? Nothing in Atlas has ever parsed one before this --
    KeepaParser only extracts ean() (see its own docstring: "NOT a
    barcode lookup... shown for the user's own manual cross-check").
    The matching-hierarchy logic reads raw.get("model")/raw.get
    ("partNumber") as a best guess (see run_batch) -- this route lets
    you check that guess against real live data before trusting it.

    Visit /debug/oa/mpn-check in a browser once (two path segments,
    deliberately -- a single-segment /debug/mpn-check collides with
    the /debug/{brand} route above, which is registered earlier and
    would otherwise intercept it as a brand lookup -- same hazard
    /debug/discord/test's own docstring already documents). Pass
    ?asins=B0X,B0Y to check specific ASINs, otherwise it pulls a
    handful of real "OA / unclear" competitor detections automatically.
    Not part of any normal app flow -- safe to ignore/delete once
    you've checked.
    """
    if asins:
        asin_list = [a.strip().upper() for a in asins.split(",") if a.strip()]
    else:
        detections = SellerWatchService.list_detections(sourcing_tag="OA / unclear", limit=limit)
        asin_list = [d["listing"].asin for d in detections]

    if not asin_list:
        return {
            "error": "No 'OA / unclear' competitor detections in the DB yet. "
                     "Pass ?asins=B0XXXXXXX,B0YYYYYYY to check specific ASINs instead, "
                     "or run a Competitor Watch check first."
        }

    service = ProductService()
    raw_products = service.get_products(asin_list, "UK", full=True, usage_category="debug")

    candidate_fields = ["model", "partNumber", "manufacturerPartNumber", "mpn", "productGroup"]
    results = []

    for raw in raw_products:
        found = {f: raw.get(f) for f in candidate_fields if raw.get(f)}
        results.append({
            "asin": raw.get("asin"),
            "title": (raw.get("title") or "")[:80],
            "ean_list": raw.get("eanList") or [],
            "mpn_candidate_fields_found": found,
            "all_top_level_keys": sorted(raw.keys()),
        })

    return jsonable_encoder({
        "checked": len(results),
        "note": "Look at 'mpn_candidate_fields_found' per ASIN -- if every result is {}, "
                "check 'all_top_level_keys' for anything else that might hold a part number, "
                "and tell Claude either way so the matching hierarchy can be finalized.",
        "results": results,
    })


@app.get("/debug/sp-api/inventory-check")
def debug_sp_inventory_check(marketplace: str = "UK", limit: int = 5):
    """
    ONE-TIME diagnostic (2026-09-01) for Out of Stock Cleanup's new FBA
    Inventory API call (see SPAPIClient.get_inventory_summaries) -- same
    "confirm the real shape before trusting it" discipline as
    /debug/oa/mpn-check above, since that method's field names were read
    off Amazon's docs, not a live response.

    Visit /debug/sp-api/inventory-check once after setting SP_API_CLIENT_
    ID/SECRET/REFRESH_TOKEN in .env (the SP-API app needs the "Product
    Listing" role authorized -- confirmed 2026-09-02 against Amazon's own
    docs: getInventorySummaries accepts either "Amazon Fulfillment" or
    "Product Listing", and deleteListingsItem needs "Product Listing"
    specifically, so selecting just that one role covers both calls) and
    check the returned SKUs/quantities look right against what Seller
    Central's Manage Inventory page actually shows. Not part of any
    normal app flow -- safe to ignore once you've checked.
    """
    sp_client = get_sp_api_client()
    if not sp_client:
        return {"error": "SP-API not configured -- set SP_API_CLIENT_ID/SECRET/REFRESH_TOKEN in .env."}

    inventory = sp_client.get_inventory_summaries(marketplace)
    if inventory is None:
        return {"error": "getInventorySummaries call failed -- check server logs for the real HTTP error."}

    zero_stock = {
        sku: v for sku, v in inventory.items()
        if v["fulfillable"] + v["inbound_working"] + v["inbound_shipped"] + v["inbound_receiving"] + v["reserved"] <= 0
    }

    return jsonable_encoder({
        "marketplace": marketplace,
        "total_skus": len(inventory),
        "zero_stock_skus": len(zero_stock),
        "sample_zero_stock": dict(list(zero_stock.items())[:limit]),
        "note": "Cross-check 'sample_zero_stock' against Seller Central's Manage Inventory page. "
                "Next, visit /debug/sp-api/sales-check to verify the fulfilled-shipments report "
                "returns real data for a couple of these SKUs before trusting Out of Stock Cleanup's "
                "Ready to review list.",
    })


@app.get("/debug/sp-api/sales-check")
def debug_sp_sales_check(marketplace: str = "UK", lookback_days: int = 30, limit: int = 5):
    """
    ONE-TIME diagnostic (2026-09-01) for the FBA Fulfilled Shipments
    report call (see SPAPIClient.get_fba_fulfilled_shipments_units) --
    this is what distinguishes "sold out" from "never stocked", so it's
    worth confirming before trusting any Delete button. Report
    generation is async and can take a couple of minutes; this route
    just waits for it (up to REPORT_POLL_TIMEOUT_SECONDS).

    Visit /debug/sp-api/sales-check once and spot-check a SKU you KNOW
    has sold before against units_shipped/last_shipment_date here.
    """
    sp_client = get_sp_api_client()
    if not sp_client:
        return {"error": "SP-API not configured -- set SP_API_CLIENT_ID/SECRET/REFRESH_TOKEN in .env."}

    sales = sp_client.get_fba_fulfilled_shipments_units(marketplace, lookback_days)
    if sales is None:
        return {"error": "Fulfilled shipments report failed -- check server logs for the real error."}

    return jsonable_encoder({
        "marketplace": marketplace,
        "lookback_days": lookback_days,
        "skus_with_confirmed_sales": len(sales),
        "sample": dict(list(sales.items())[:limit]),
    })


@app.get("/debug/sp-api/storage-fee-check")
def debug_sp_storage_fee_check(marketplace: str = "UK"):
    """
    ONE-TIME diagnostic (2026-09-02) for Storage Fee Watch's two new
    Reports API calls (GET_FBA_STORAGE_FEE_CHARGES_DATA and GET_FBA_
    FULFILLMENT_LONGTERM_STORAGE_FEE_CHARGES_DATA) -- same "confirm the
    real shape before trusting it" discipline as /debug/sp-api/
    inventory-check and /debug/sp-api/sales-check above, except this
    time BOTH the required SP-API role AND the column names/current
    aged-inventory-surcharge thresholds are genuinely unconfirmed (see
    SPAPIClient.get_storage_fee_charges and .get_longterm_storage_fee_
    charges' own docstrings for the pre-research this route exists to
    check). Do NOT assume the "Product Listing" role already pending
    for Out of Stock Cleanup covers this -- these are finance/FBA
    reports, almost certainly a different role.

    Deliberately calls the private _run_report() helper directly rather
    than the two get_* methods above, so the RAW header row prints
    regardless of whether those methods' guessed column names are
    right -- confirming that is the whole point of this route. Report
    generation is async and can take a couple of minutes per report;
    this route waits for both in turn (up to 2x REPORT_POLL_TIMEOUT_
    SECONDS worst case).

    Visit /debug/sp-api/storage-fee-check once. If either report call
    fails, "error" reflects that the raw HTTP status + response body
    Amazon actually sent was printed to the server log (not returned
    here directly, to avoid ever leaking a full error body into a
    browser response) -- check server logs for it. If Amazon's error
    names a specific required role, that's the real answer to "what
    role does Tamara need to request" -- more reliable than any of this
    file's own pre-research comments.
    """
    sp_client = get_sp_api_client()
    if not sp_client:
        return {"error": "SP-API not configured -- set SP_API_CLIENT_ID/SECRET/REFRESH_TOKEN in .env."}

    now = datetime.now(timezone.utc)
    result = {"marketplace": marketplace}

    # GET_FBA_STORAGE_FEE_CHARGES_DATA -- Amazon's docs say dataStartTime
    # must be >= 72h before now, dataEndTime = now (see get_storage_fee_
    # charges' own docstring for the source).
    storage_text = sp_client._run_report(
        "GET_FBA_STORAGE_FEE_CHARGES_DATA", marketplace, now - timedelta(hours=76), now
    )
    if storage_text is None:
        result["storage_fee_report"] = {
            "error": "Report call failed -- check server logs for the real HTTP status/error body Amazon returned.",
        }
    else:
        lines = storage_text.splitlines()
        result["storage_fee_report"] = {
            "header_row": lines[0].split("\t") if lines else [],
            "sample_row": lines[1].split("\t") if len(lines) > 1 else None,
            "total_rows": max(len(lines) - 1, 0),
        }

    # GET_FBA_FULFILLMENT_LONGTERM_STORAGE_FEE_CHARGES_DATA -- Amazon's
    # docs say the dataStartTime/dataEndTime span should equal one month
    # (see get_longterm_storage_fee_charges' own docstring for the
    # source).
    ltsf_text = sp_client._run_report(
        "GET_FBA_FULFILLMENT_LONGTERM_STORAGE_FEE_CHARGES_DATA", marketplace, now - timedelta(days=30), now
    )
    if ltsf_text is None:
        result["longterm_storage_fee_report"] = {
            "error": "Report call failed -- check server logs for the real HTTP status/error body Amazon returned.",
        }
    else:
        lines = ltsf_text.splitlines()
        result["longterm_storage_fee_report"] = {
            "header_row": lines[0].split("\t") if lines else [],
            "sample_row": lines[1].split("\t") if len(lines) > 1 else None,
            "total_rows": max(len(lines) - 1, 0),
        }

    result["note"] = (
        "Compare each report's 'header_row' against the column names guessed in "
        "SPAPIClient.get_storage_fee_charges/get_longterm_storage_fee_charges' docstrings -- "
        "fix the parsing there if they differ. Look for any age/'days in FC' column neither "
        "method currently uses. If either report shows an 'error' instead, check the server "
        "log for the raw error body and whether it names a specific required role."
    )

    return jsonable_encoder(result)


@app.get("/debug/discord/test")
def debug_test_discord():
    """
    Sends one sample Discord message via DiscordNotifier.send_test_ping
    -- visit this in a browser once after setting DISCORD_WEBHOOK_URL
    (and optionally DISCORD_MENTION_USER_ID) in .env, to confirm the
    setup actually works without waiting for a real scan to find a
    genuine opportunity. Not part of any normal app flow.

    Two path segments after /debug/ (not just /debug/test-discord)
    deliberately -- the existing /debug/{brand} route above (single
    segment) would otherwise intercept a one-segment path first,
    matching "test-discord" as if it were a brand name to look up
    (which is exactly what happened the first time this was tried --
    it returned that route's own "No products found" error instead of
    ever reaching this one). Routes are matched in registration order,
    and /debug/{brand} is registered earlier in this file.
    """
    return DiscordNotifier.send_test_ping()
