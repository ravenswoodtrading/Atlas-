"""
Background runner for Verdict Checker bulk batches (2026-08-27).

Why this exists: POST /verdict/bulk used to do the entire batch inline
and render the results into that single response. Two consequences,
both of which the user hit:

1. "Nothing happened." Each ASIN is a Keepa call plus one-or-two Claude
   calls (~12s measured), and the whole batch first waits on
   ScanCoordinator's lock, which the automated scan queue holds for a
   full 100-ASIN brand scan. A 30-ASIN batch is therefore minutes of a
   blank spinning tab with no output whatsoever -- not even a log line,
   since uvicorn only writes its access line once a response is sent.
2. The results vanished on navigation, and there was no history: leads
   were saved individually as source="manual", indistinguishable from
   single checks, with nothing recording that a batch ever ran.

So a submission now creates a VerdictRun immediately, hands the work to
a daemon thread, and returns a redirect straight away. The results page
polls the run row. Same pattern as OaSourceRun/OaSourceCandidate, which
already solved this exact shape for OA Source Discovery.
"""

import threading
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import VerdictRun, VerdictRunItem
from app.services.keepa_priority import KeepaPriority, ScanBusyError, BATCH_WAIT_SECONDS
from app.services.product_service import KeepaTokensExhaustedError
from app.services.verdict_service import resolve_source_marketplace


class VerdictRunService:

    @staticmethod
    def start_run(
        items: list[tuple[str, float | None]], asins_text: str = "",
        source_detail: str | None = None, source_marketplace: str | None = None,
    ) -> int:
        """
        Persists the run and its pending items, starts the worker, and
        returns the run id for the caller to redirect to. Returns
        immediately -- by the time the user's browser follows that
        redirect, typically not a single ASIN has been checked yet, and
        that's the point: the page can render "0 of 30 done" instead of
        showing nothing for six minutes.

        source_marketplace is resolved HERE rather than in the worker so
        the value stored on the run is the one actually used, including
        when it was inferred from an amazon.de URL pasted into
        source_detail (see resolve_source_marketplace).
        """
        resolved_marketplace = resolve_source_marketplace(source_marketplace, source_detail) or ""

        db = SessionLocal()

        try:
            run = VerdictRun(
                status="running",
                source_detail=source_detail or "",
                source_marketplace=resolved_marketplace,
                asins_text=asins_text,
                asins_submitted=len(items),
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id

            for position, (asin, cost_price) in enumerate(items):
                db.add(VerdictRunItem(
                    run_id=run_id,
                    position=position,
                    asin=asin,
                    cost_price=cost_price,
                    status="pending",
                ))

            db.commit()
        finally:
            db.close()

        # daemon=True so a run in flight never keeps the server from
        # shutting down -- an interrupted run is recoverable (its items
        # stay "pending" and the leads it already saved are real), a
        # server that won't stop is not.
        thread = threading.Thread(
            target=VerdictRunService._execute_run,
            args=(run_id,),
            name=f"verdict-run-{run_id}",
            daemon=True,
        )
        thread.start()

        return run_id

    @staticmethod
    def _execute_run(run_id: int):
        """
        The worker body. Runs in its own thread with its own Session --
        SQLite is opened check_same_thread=False in WAL mode (see
        app/database/database.py), so concurrent reads from the polling
        page are fine alongside this thread's writes.

        The whole batch shares ONE KeepaPriority window, for the reason
        _run_bulk_verdict_check always did: marking active once, not
        once per ASIN, is what actually bounds how long an in-flight
        low-priority scan waits before yielding. Marking it also stands
        the automated ticks down for the duration (see
        ScanCoordinator.try_acquire_for_automated_tick) -- which is what
        makes a user-started batch actually take priority rather than
        merely asking politely.

        BATCH_WAIT_SECONDS, not the synchronous DEFAULT_WAIT_SECONDS:
        nobody is watching a request here, so waiting is cheap and
        giving up costs the user the whole batch. A 30-ASIN run was
        lost to exactly that on 2026-08-28.
        """
        # Deferred import: the two-pass per-ASIN pipeline lives in
        # app/routes/verdict.py, which imports this service. Importing
        # it at module scope would be a cycle; importing it here, once
        # per run, costs nothing measurable next to a Keepa call.
        from app.routes.verdict import _run_verdict_check_inner, _save_lead

        db = SessionLocal()

        try:
            run = db.get(VerdictRun, run_id)

            if run is None:
                return

            source_detail = run.source_detail or None
            source_marketplace = run.source_marketplace or None

            items = (
                db.query(VerdictRunItem)
                .filter(VerdictRunItem.run_id == run_id)
                .order_by(VerdictRunItem.position)
                .all()
            )

            try:
                with KeepaPriority.high_priority(timeout=BATCH_WAIT_SECONDS):
                    for item in items:
                        VerdictRunService._check_one(
                            db, run, item,
                            source_detail=source_detail,
                            source_marketplace=source_marketplace,
                            run_check=_run_verdict_check_inner,
                            save_lead=_save_lead,
                        )
            except ScanBusyError as exc:
                # Never got a turn -- nothing was checked. Recorded on
                # the run so the results page can say exactly that and
                # offer a re-run, rather than showing 30 rows stuck on
                # "pending" with no explanation.
                run.status = "error"
                run.error = str(exc)
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
                return

            run.status = "done"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            # Anything genuinely unexpected. Per-ASIN failures are
            # already handled inside _check_one and don't reach here;
            # this is the backstop that stops a run sitting on
            # "running" forever after the thread dies.
            print(f"Verdict run {run_id} failed: {exc}")

            try:
                run = db.get(VerdictRun, run_id)

                if run is not None:
                    run.status = "error"
                    run.error = f"Run failed: {exc}"
                    run.completed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()

    @staticmethod
    def _check_one(
        db, run: VerdictRun, item: VerdictRunItem,
        source_detail: str | None, source_marketplace: str | None,
        run_check, save_lead,
    ):
        """
        Checks one ASIN and commits its result plus the run's running
        tallies together, so the polling page never reads a state where
        an item is done but the counter hasn't caught up.

        A failure here is recorded on the item and the loop continues --
        one dead ASIN (Keepa has no data, tokens ran out mid-batch)
        must not cost the user the other 29.
        """
        try:
            metrics, verdict, rationale, deep_dive_fired, error = run_check(
                item.asin, item.cost_price, source_marketplace=source_marketplace,
            )
        except KeepaTokensExhaustedError as exc:
            metrics, verdict, rationale, deep_dive_fired, error = None, None, None, False, str(exc)
        except Exception as exc:
            metrics, verdict, rationale, deep_dive_fired, error = None, None, None, False, f"Check failed: {exc}"

        if error:
            item.status = "failed"
            item.error = error
            run.failed_count += 1
        else:
            item.status = "done"
            # Cleared explicitly, not just left alone: reconcile_interrupted_runs
            # stamps "Not checked -- Atlas restarted." onto every still-pending
            # item, and if it runs while this worker is mid-ASIN (a restart
            # racing a live batch) that text otherwise survives onto a row that
            # then completes perfectly well -- seen live 2026-08-28, an item
            # showing status=done, verdict=AVOID, and "Not checked" side by side.
            item.error = ""
            item.verdict = verdict or ""
            item.rationale = rationale or ""
            item.deep_dive_fired = deep_dive_fired
            item.title = (metrics or {}).get("title") or ""
            item.keepa_estimate_roi = (metrics or {}).get("keepa_estimate_roi")
            item.keepa_estimate_profit = (metrics or {}).get("keepa_estimate_profit")

            try:
                item.lead_id = save_lead(
                    item.asin, item.cost_price, metrics, verdict, rationale,
                    source_detail=source_detail, source_marketplace=source_marketplace,
                )
            except Exception as exc:
                # The verdict itself is good -- only persisting the Lead
                # failed. Keep the result visible on the run (just
                # without a "Details" link) rather than discarding a
                # check the user already paid Keepa and Claude for.
                print(f"Saving lead for {item.asin} failed, keeping run result: {exc}")

            if verdict == "BUY":
                run.buy_count += 1
            elif verdict == "WATCH":
                run.watch_count += 1
            else:
                run.avoid_count += 1

        item.completed_at = datetime.now(timezone.utc)
        run.asins_completed += 1
        db.commit()

    @staticmethod
    def reconcile_interrupted_runs() -> int:
        """
        Called once at startup. The worker threads are daemons, so a
        server restart kills any batch mid-flight and leaves its run
        row saying "running" forever -- which the results page believes,
        so it keeps auto-refreshing a batch that will never advance.
        That is the same "looks like it's working, isn't" failure this
        whole feature exists to remove, so close it here instead.

        Only the run and its still-pending items are touched. Items
        already marked done keep their verdicts, and the Leads they
        created are untouched -- an interrupted batch is partial, not
        void. Returns how many runs were closed out.
        """
        db = SessionLocal()

        try:
            stale = db.query(VerdictRun).filter(VerdictRun.status == "running").all()

            for run in stale:
                run.status = "error"
                run.error = (
                    "Atlas restarted while this batch was running, so it stopped early. "
                    "Anything already checked is below and its leads were saved -- "
                    "re-submit the rest if you still need them."
                )
                run.completed_at = datetime.now(timezone.utc)

                (
                    db.query(VerdictRunItem)
                    .filter(
                        VerdictRunItem.run_id == run.id,
                        VerdictRunItem.status == "pending",
                    )
                    .update({"status": "failed", "error": "Not checked -- Atlas restarted."})
                )

            if stale:
                db.commit()
                print(f"Closed out {len(stale)} verdict run(s) interrupted by a restart.")

            return len(stale)
        finally:
            db.close()

    @staticmethod
    def get_run(run_id: int) -> VerdictRun | None:
        db = SessionLocal()

        try:
            return db.get(VerdictRun, run_id)
        finally:
            db.close()

    @staticmethod
    def get_run_with_items(run_id: int) -> tuple[VerdictRun | None, list[VerdictRunItem]]:
        """
        Both in one Session so the objects are fully loaded before it
        closes -- returning a detached VerdictRun and then touching
        run.items in the template would raise.
        """
        db = SessionLocal()

        try:
            run = db.get(VerdictRun, run_id)

            if run is None:
                return None, []

            items = (
                db.query(VerdictRunItem)
                .filter(VerdictRunItem.run_id == run_id)
                .order_by(VerdictRunItem.position)
                .all()
            )

            db.expunge_all()
            return run, items
        finally:
            db.close()

    @staticmethod
    def list_runs(limit: int = 25) -> list[VerdictRun]:
        """Most recent first -- the run picker on /verdict."""
        db = SessionLocal()

        try:
            runs = (
                db.query(VerdictRun)
                .order_by(VerdictRun.started_at.desc())
                .limit(limit)
                .all()
            )
            db.expunge_all()
            return runs
        finally:
            db.close()
