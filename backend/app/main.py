import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

from app.services.brand_scan_service import BrandScanService
from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.category_survey_service import CategorySurveyService
from app.services.scan_queue_service import ScanQueueService, TICK_INTERVAL_SECONDS
from app.services.seller_watch_service import SellerWatchService
from app.services.watchlist_service import WatchlistService
from app.services.replen_service import ReplenService
from app.services.scan_coordinator import ScanCoordinator
from app.services.lead_analysis_service import LeadAnalysisService
from app.services.discord_notifier import DiscordNotifier
from app.services.product_repository import ProductRepository
from app.services.signal_service import SignalService
from app.services.activity_log import ActivityLog

from app.database.base import Base
from app.database.database import engine
from app.database import models  # noqa: F401 -- registers ProductRecord with Base.metadata

from app.routes import (
    dashboard, keepa, scan, analyse, opportunities_view, products, watchlist,
    categories, scan_queue, replen, competitors, review_queue, oa_lookup,
    verdict, leads, signals, oa_source_discovery, help as help_route,
    token_usage,
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
            result = await asyncio.to_thread(ScanQueueService.run_next_tick)
            summary = result.get("skipped") or result.get("error") or (
                f"{result.get('brand', '?')}: {result.get('asins_scanned_this_tick', 0)} ASINs"
            )
            await asyncio.to_thread(ActivityLog.mark_tick, "scan_queue", SCAN_QUEUE_INTERVAL_SECONDS, summary)
        except Exception as exc:
            print(f"Scan queue tick failed: {exc}")

        await asyncio.sleep(SCAN_QUEUE_INTERVAL_SECONDS)


# How often the background competitor-watch scheduler checks tracked
# sellers' storefronts for new listings -- storefronts don't change
# minute-to-minute, so this stays token-cheap. Per-seller pausing is
# handled by TrackedSeller.active (see /competitors), not a separate
# global switch here. A check also always runs once immediately at
# startup, not just after the first interval.
SELLER_WATCH_INTERVAL_SECONDS = 2 * 60 * 60


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


async def _weekly_recheck_scheduler():
    while True:
        try:
            # Same non-blocking "skip if a manual scan is already
            # running" priority rule as the other automated ticks --
            # this is a safety net for whatever the user HASN'T
            # manually checked recently, so it should never compete
            # with something they're actively doing right now.
            if ScanCoordinator.try_acquire_for_automated_tick():
                try:
                    watch_result = await asyncio.to_thread(WatchlistService.check_stale, WEEKLY_RECHECK_STALE_HOURS)
                    replen_result = await asyncio.to_thread(ReplenService.check_stale, WEEKLY_RECHECK_STALE_HOURS)
                    summary = (
                        f"watchlist: {watch_result.get('checked', 0)}/{watch_result.get('stale', 0)} stale, "
                        f"replen: {replen_result.get('checked', 0)}/{replen_result.get('stale', 0)} stale"
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
        except Exception as exc:
            print(f"Weekly recheck tick failed: {exc}")

        await asyncio.sleep(WEEKLY_RECHECK_TICK_SECONDS)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    scan_queue_task = asyncio.create_task(_scan_queue_scheduler())
    seller_watch_task = asyncio.create_task(_seller_watch_scheduler())
    weekly_recheck_task = asyncio.create_task(_weekly_recheck_scheduler())
    lead_analysis_task = asyncio.create_task(_lead_analysis_scheduler())
    signal_task = asyncio.create_task(_signal_scheduler())
    yield
    scan_queue_task.cancel()
    seller_watch_task.cancel()
    weekly_recheck_task.cancel()
    lead_analysis_task.cancel()
    signal_task.cancel()


app = FastAPI(title="Atlas", lifespan=lifespan)

# Creates any tables that don't exist yet (e.g. product_records) without
# touching existing ones. The old 'products' table (incompatible legacy
# schema) is left alone -- product_records is a separate table.
Base.metadata.create_all(bind=engine)


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
app.include_router(help_route.router)
app.include_router(token_usage.router)


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