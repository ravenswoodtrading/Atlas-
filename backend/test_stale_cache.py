"""StaleCache: fresh / stale / first-call behaviour, single-flight refresh, failures keep the last good value."""
import threading
import time
import unittest

from app.services.stale_cache import StaleCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def wait_until(predicate, timeout=5.0):
    end = time.time() + timeout
    while not predicate() and time.time() < end:
        time.sleep(0.005)
    return predicate()


class StaleCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = StaleCache(ttl_seconds=100, retry_seconds=10, name="test", clock=self.clock)
        self.calls = 0

    def compute(self, value="v", gate=None, fail=False):
        def run():
            self.calls += 1
            if gate is not None:
                gate.wait(5)
            if fail:
                raise RuntimeError("boom")
            return value
        return run

    def settle(self):
        self.assertTrue(wait_until(lambda: not self.cache.refreshing()), "background refresh did not finish")

    def test_the_first_call_computes_inline_and_a_fresh_value_is_reused(self):
        self.assertEqual(self.cache.get("k", self.compute("one")), "one")
        self.clock.advance(99)
        self.assertEqual(self.cache.get("k", self.compute("two")), "one")
        self.assertEqual(self.calls, 1)

    def test_a_stale_value_is_returned_at_once_and_refreshed_behind_the_caller(self):
        self.cache.get("k", self.compute("old"))
        self.clock.advance(101)
        gate = threading.Event()
        started = time.time()
        served = self.cache.get("k", self.compute("new", gate=gate))
        self.assertLess(time.time() - started, 1.0)              # did not wait for the (blocked) rebuild
        self.assertEqual(served, "old")
        self.assertTrue(self.cache.refreshing("k"))
        gate.set()
        self.settle()
        self.assertEqual(self.cache.get("k", self.compute("unused")), "new")

    def test_only_one_refresh_runs_at_a_time(self):
        self.cache.get("k", self.compute("old"))
        self.clock.advance(101)
        gate = threading.Event()
        for _ in range(10):
            self.cache.get("k", self.compute("new", gate=gate))
        gate.set()
        self.settle()
        self.assertEqual(self.calls, 2)                          # the first build plus exactly one refresh

    def test_a_failed_refresh_keeps_the_last_good_value_and_retries_after_the_retry_delay(self):
        self.cache.get("k", self.compute("good"))
        self.clock.advance(101)
        self.assertEqual(self.cache.get("k", self.compute(fail=True)), "good")
        self.settle()
        self.assertEqual(self.cache.peek("k"), "good")           # not replaced by an error
        self.clock.advance(5)                                    # inside the retry delay: served, no new attempt
        self.cache.get("k", self.compute("later"))
        self.settle()
        self.assertEqual(self.calls, 2)
        self.clock.advance(6)                                    # past it: one more attempt
        self.cache.get("k", self.compute("later"))
        self.settle()
        self.assertEqual(self.calls, 3)
        self.assertEqual(self.cache.get("k", self.compute("unused")), "later")

    def test_a_failing_first_computation_raises_and_caches_nothing(self):
        with self.assertRaises(RuntimeError):
            self.cache.get("k", self.compute(fail=True))
        self.assertIsNone(self.cache.peek("k"))
        self.assertEqual(self.cache.get("k", self.compute("ok")), "ok")

    def test_concurrent_first_callers_share_one_computation(self):
        gate = threading.Event()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.cache.get("k", self.compute("v", gate=gate)))) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(results, ["v"] * 6)
        self.assertEqual(self.calls, 1)

    def test_keys_are_independent(self):
        self.cache.get("a", self.compute("A"))
        self.assertEqual(self.cache.get("b", self.compute("B")), "B")
        self.clock.advance(101)
        gate = threading.Event()
        self.cache.get("a", self.compute("A2", gate=gate))
        self.assertTrue(self.cache.refreshing("a"))
        self.assertFalse(self.cache.refreshing("b"))
        gate.set()
        self.settle()

    def test_invalidate_forces_the_next_get_to_compute_inline(self):
        self.cache.get("k", self.compute("one"))
        self.cache.invalidate("k")
        self.assertEqual(self.cache.get("k", self.compute("two")), "two")
        self.cache.invalidate()
        self.assertIsNone(self.cache.peek("k"))

    def test_peek_never_computes(self):
        self.assertEqual(self.cache.peek("nothing", "default"), "default")
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
