import threading


class ScanCoordinator:
    """
    Coordinates access between the automated scan-queue scheduler and
    manual, user-initiated scans (Discovery, Replen "check now") so
    the two never spend Keepa tokens at the same time -- manual scans
    always take priority.

    Manual scans acquire the lock (blocking) before running, so if an
    automated tick happens to already be mid-scan they simply wait for
    it to finish first (brief, since one tick is a single scan call)
    rather than racing it for tokens. The automated scheduler only
    ever makes a non-blocking attempt -- if a manual scan is already
    running it skips this tick entirely and tries again next interval,
    rather than making the user's Discovery/Replen request wait on it.

    Deliberately NOT the same thing as AutomationSettings.paused --
    that's a persistent setting the user controls; this is a transient
    in-process gate that never changes it, so a manual scan finishing
    can't accidentally un-pause automation the user turned off on
    purpose.

    HIGH-PRIORITY WAITERS (2026-08-28). "Skip this tick if a manual
    scan is already RUNNING" turned out not to be enough, because a
    non-blocking acquire can barge past a thread that is already
    blocked waiting for the same lock. Observed live: a 30-ASIN
    Verdict Checker batch waited on the lock, an automated brand-scan
    tick fired every 60s and won the release race each time, and the
    batch was starved out entirely -- 0 of 30 checked. So an automated
    tick now also declines to start while any high-priority caller is
    merely WAITING, not just holding.

    The waiter count lives here rather than in KeepaPriority (which is
    what actually marks it, and is the API the rest of the app calls)
    purely to keep the imports one-way: keepa_priority imports this
    module, so this module cannot import it back.
    """

    _lock = threading.Lock()

    # Guards _high_priority_waiting only -- NOT the scan lock above.
    _waiting_lock = threading.Lock()
    _high_priority_waiting = 0

    @staticmethod
    def mark_high_priority_waiting():
        """Called by KeepaPriority.mark_active -- see has_high_priority_pending."""
        with ScanCoordinator._waiting_lock:
            ScanCoordinator._high_priority_waiting += 1

    @staticmethod
    def clear_high_priority_waiting():
        """Called by KeepaPriority.mark_done."""
        with ScanCoordinator._waiting_lock:
            ScanCoordinator._high_priority_waiting = max(
                0, ScanCoordinator._high_priority_waiting - 1
            )

    @staticmethod
    def has_high_priority_pending() -> bool:
        with ScanCoordinator._waiting_lock:
            return ScanCoordinator._high_priority_waiting > 0

    @staticmethod
    def acquire_for_manual_scan(timeout: float | None = None) -> bool:
        """
        Blocks forever by default -- the original behaviour every
        Discovery/Replen/Watchlist call site relies on, where waiting
        for an in-flight tick is always better than refusing to scan.

        timeout (seconds) makes the wait bounded and returns False if
        it expired, for callers that would rather fail fast with a
        "try again in a minute" than hang. Added 2026-08-27 for the
        Verdict Checker: an automated scan-queue tick holds this lock
        for a whole 100-ASIN brand scan, and a verdict check waiting
        on it looked to the user exactly like the app doing nothing at
        all -- no response, no error, no log line (uvicorn only writes
        its access line once a response is actually sent).

        Returns True when the lock was acquired -- ONLY then may the
        caller go on to call release_after_manual_scan().
        """
        if timeout is None:
            ScanCoordinator._lock.acquire()
            return True

        return ScanCoordinator._lock.acquire(timeout=timeout)

    @staticmethod
    def release_after_manual_scan():
        ScanCoordinator._lock.release()

    @staticmethod
    def try_acquire_for_automated_tick() -> bool:
        """
        Every automated Keepa-spending tick goes through here (scan
        queue, seller watch, weekly recheck, signals), so the
        high-priority guard lives here rather than being repeated at
        each of those four call sites.

        Returns False -- i.e. "skip this tick, try again next interval"
        -- when a high-priority caller is waiting for the lock, even
        though the lock itself may be free this instant. That is the
        whole point: whoever is waiting should get it, not us.
        """
        if ScanCoordinator.has_high_priority_pending():
            return False

        return ScanCoordinator._lock.acquire(blocking=False)

    @staticmethod
    def release_after_automated_tick():
        ScanCoordinator._lock.release()

    @staticmethod
    def is_busy() -> bool:
        """
        True if ANY scan (manual or automated) is actively running
        right now. threading.Lock has no clean "peek" -- this works by
        attempting a non-blocking acquire and immediately releasing it
        if successful, which tests availability without holding onto
        it or affecting whoever's turn is next.
        """
        acquired = ScanCoordinator._lock.acquire(blocking=False)

        if acquired:
            ScanCoordinator._lock.release()
            return False

        return True
