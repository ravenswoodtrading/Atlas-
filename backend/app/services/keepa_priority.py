import threading
from contextlib import contextmanager

from app.services.scan_coordinator import ScanCoordinator


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
    """

    _lock = threading.Lock()
    _active_count = 0

    @staticmethod
    def mark_active():
        with KeepaPriority._lock:
            KeepaPriority._active_count += 1

    @staticmethod
    def mark_done():
        with KeepaPriority._lock:
            KeepaPriority._active_count = max(0, KeepaPriority._active_count - 1)

    @staticmethod
    def has_pending() -> bool:
        with KeepaPriority._lock:
            return KeepaPriority._active_count > 0

    @staticmethod
    @contextmanager
    def high_priority():
        """
        Wraps a high-priority Keepa call site (manual verdict check, queued
        lead analysis) -- used instead of ScanCoordinator.acquire_for_manual_scan()
        directly, so the mark happens first and any in-flight scan yields
        before the (now bounded) blocking acquire below actually waits.
        """
        KeepaPriority.mark_active()
        try:
            ScanCoordinator.acquire_for_manual_scan()
            try:
                yield
            finally:
                ScanCoordinator.release_after_manual_scan()
        finally:
            KeepaPriority.mark_done()
