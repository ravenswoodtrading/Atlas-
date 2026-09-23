from contextlib import contextmanager

from app.services.scan_coordinator import ScanCoordinator


class ScanBusyError(RuntimeError):
    """
    Raised by high_priority(timeout=...) when an in-flight scan still
    held ScanCoordinator's lock after the wait expired. Distinct from
    KeepaTokensExhaustedError (which means Keepa itself said no) --
    this one is purely local contention and is always worth retrying
    shortly.
    """


# How long a high-priority caller waits for an in-flight low-priority
# scan to yield before giving up (2026-08-27). Marking active is
# supposed to bound this to about one Keepa call (see the class
# docstring below), but "about one Keepa call" is not a guarantee --
# a Product Finder page fetch can run long, and the Verdict Checker
# blocking behind it with no output was indistinguishable from the app
# being broken. Generous enough that a normal yield never trips it.
#
# This is the SYNCHRONOUS-caller figure: someone is watching a request
# and would rather see "try again in a minute" than a dead tab. A
# background batch should use BATCH_WAIT_SECONDS instead.
DEFAULT_WAIT_SECONDS = 90

# How long a BACKGROUND run waits (2026-08-28). Much longer, because
# the tradeoff is completely different once the work isn't inside the
# user's own request: nobody is staring at a hanging page, the results
# page just says "running", so waiting is nearly free while giving up
# costs the user their whole batch. Observed live: a 30-ASIN batch hit
# the 90s synchronous timeout and reported "0 of 30 checked" purely
# because an automated brand scan happened to be mid-tick -- the
# batch's own tokens and credits were fine.
#
# Still bounded rather than None so a genuinely stuck lock surfaces as
# a run-level error the user can see and re-run, instead of a thread
# parked forever. With the automated ticks now standing down for a
# waiting high-priority caller (see
# ScanCoordinator.try_acquire_for_automated_tick), the realistic wait
# is one in-flight chunk, not this.
BATCH_WAIT_SECONDS = 900


class KeepaPriority:
    """
    Cooperative "get out of the way" signal for the Keepa token budget --
    NOT a real scheduler. Keepa calls all go through one shared synchronous
    client (see app/keepa/client.py), so there's no way to literally split a
    token budget between two callers mid-flight. Instead: high-priority work
    (manual /verdict checks, queued lead analysis) marks itself active while
    it runs; the low-priority A2A scan pipeline
    (BrandScanService._fetch_in_chunks) checks has_pending() at every chunk
    boundary (<=10 ASINs) and yields -- stopping early via the exact same
    "ran out, don't advance the page, retry next tick" path it already uses
    for a low-token pause, so no new resume-state is needed.

    high_priority() also closes a gap in ScanCoordinator alone: a manual
    caller blocking on acquire_for_manual_scan() would otherwise wait for an
    ENTIRE in-flight scan (which, chunked, can run long) before starting.
    Marking active here BEFORE that blocking acquire means any in-flight
    scan's chunk loop sees the flag and exits within one more Keepa call --
    so the wait high_priority() actually incurs is bounded to about one
    Keepa HTTP call, not a whole scan.

    The marked-active flag is stored on ScanCoordinator (2026-08-28) so
    that try_acquire_for_automated_tick can also consult it and decline
    to START a tick while a high-priority caller is waiting -- without
    that, a non-blocking acquire barges straight past a blocked waiter
    and starves it. See ScanCoordinator's own docstring.
    """

    @staticmethod
    def mark_active():
        ScanCoordinator.mark_high_priority_waiting()

    @staticmethod
    def mark_done():
        ScanCoordinator.clear_high_priority_waiting()

    @staticmethod
    def has_pending() -> bool:
        return ScanCoordinator.has_high_priority_pending()

    @staticmethod
    @contextmanager
    def priority_slot(timeout: float | None = None):
        """
        Like high_priority(), but the "get out of the way" signal is raised ONLY while waiting for
        the lock and withdrawn the moment it is held. For work that runs its own BrandScanService
        scans (Competitor Watch): under high_priority() the caller's own scan would see
        has_pending() at every chunk boundary and stop itself early. While it waits, the automated
        ticks decline to start and any in-flight scan yields within one chunk; once it holds the
        lock it runs undisturbed and everyone else simply can't start.
        """
        KeepaPriority.mark_active()
        try:
            acquired = ScanCoordinator.acquire_for_manual_scan(timeout=timeout)
        finally:
            KeepaPriority.mark_done()
        if not acquired:
            raise ScanBusyError(
                "A Keepa scan is running right now and didn't finish in time. "
                "Nothing was checked -- try again shortly."
            )
        try:
            yield
        finally:
            ScanCoordinator.release_after_manual_scan()

    @staticmethod
    @contextmanager
    def high_priority(timeout: float | None = None):
        """
        Wraps a high-priority Keepa call site (manual verdict check, queued
        lead analysis) -- used instead of ScanCoordinator.acquire_for_manual_scan()
        directly, so the mark happens first and any in-flight scan yields
        before the (now bounded) blocking acquire below actually waits.

        timeout (seconds) caps that wait and raises ScanBusyError instead
        of blocking indefinitely. Synchronous callers should pass
        DEFAULT_WAIT_SECONDS, background runs BATCH_WAIT_SECONDS; leaving
        it None keeps the original wait-forever behaviour.

        mark_done() stays in the outer finally either way -- the yield
        signal must be withdrawn even when the acquire never succeeded,
        or the low-priority scan keeps politely standing aside for a
        caller that already gave up.
        """
        KeepaPriority.mark_active()
        try:
            if not ScanCoordinator.acquire_for_manual_scan(timeout=timeout):
                raise ScanBusyError(
                    "A Keepa scan is running right now and didn't finish in time. "
                    "Nothing was checked -- try again in a minute."
                )
            try:
                yield
            finally:
                ScanCoordinator.release_after_manual_scan()
        finally:
            KeepaPriority.mark_done()
