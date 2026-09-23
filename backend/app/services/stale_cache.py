"""
Serve-stale-while-refreshing cache (2026-09-21, Tamara: slow pages are "a constant problem").

Atlas pages did their expensive work (a Google Sheets read, a full-table roll-up) on the request and kept the result for
a few minutes, so whoever opened the page just after it expired waited for the full rebuild (3-15 seconds, sometimes a
timeout). This keeps the last good result and, once it is older than `ttl_seconds`, hands it out immediately while ONE
background thread rebuilds it:

    cache = StaleCache(ttl_seconds=300)
    rows = cache.get("va-sheet", lambda: read_sheet())

  * nothing cached yet   -> computed inline, once (concurrent first callers wait for that one computation)
  * fresh                -> returned
  * stale                -> returned AT ONCE; a single background refresh starts (a second stale call while one is
                            running does not start another)
  * a refresh that fails -> the old value is kept (never replaced by an error) and retried after `retry_seconds`
  * a first, inline computation that fails raises to the caller and caches nothing

The value handed out is the cached object itself, so callers must not mutate it. Deliberately tiny: no size limit
(keys are a handful of page/param combinations) and no per-key TTL.
"""
import threading
import time


class StaleCache:
    def __init__(self, ttl_seconds: float, retry_seconds: float = 60.0, name: str = "cache", clock=time.monotonic):
        self.ttl = ttl_seconds
        self.retry = retry_seconds
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}       # key -> [value, built_at]
        self._inflight = set()   # keys with a background refresh running
        self._key_locks = {}     # key -> lock serialising the very first (inline) computation

    def get(self, key, compute):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if self._clock() - entry[1] < self.ttl:
                    return entry[0]
                stale = entry[0]
            else:
                stale = None
                key_lock = self._key_locks.setdefault(key, threading.Lock())
        if entry is not None:
            self.refresh_in_background(key, compute)
            return stale

        with key_lock:                                    # first ever computation for this key
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None:                         # another caller filled it while we waited
                return entry[0]
            value = compute()
            with self._lock:
                self._entries[key] = [value, self._clock()]
            return value

    def peek(self, key, default=None):
        """The cached value regardless of age, or `default` -- never computes or refreshes."""
        with self._lock:
            entry = self._entries.get(key)
        return default if entry is None else entry[0]

    def invalidate(self, key=None):
        """Forget one key (or all): the next get() computes inline again."""
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def refresh_in_background(self, key, compute) -> bool:
        """Start ONE background rebuild of `key`. False (doing nothing) if one is already running."""
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)

        def work():
            try:
                value = compute()
                with self._lock:
                    self._entries[key] = [value, self._clock()]
            except Exception as exc:
                print(f"StaleCache[{self.name}] refresh of {key!r} failed, keeping the last result: {exc}")
                with self._lock:
                    entry = self._entries.get(key)
                    if entry is not None:
                        entry[1] = self._clock() - self.ttl + self.retry    # due again in retry_seconds
            finally:
                with self._lock:
                    self._inflight.discard(key)

        threading.Thread(target=work, name=f"stale-cache-{self.name}", daemon=True).start()
        return True

    def refreshing(self, key=None) -> bool:
        with self._lock:
            return bool(self._inflight) if key is None else key in self._inflight
