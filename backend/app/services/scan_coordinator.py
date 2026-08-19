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
    """

    _lock = threading.Lock()

    @staticmethod
    def acquire_for_manual_scan():
        ScanCoordinator._lock.acquire()

    @staticmethod
    def release_after_manual_scan():
        ScanCoordinator._lock.release()

    @staticmethod
    def try_acquire_for_automated_tick() -> bool:
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
