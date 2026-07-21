"""RateLimiter: sliding-window allow/deny behavior + periodic eviction of idle
buckets (A6) -- once real per-IP/per-token keys flow (A1/A3), a growing set of
distinct keys must not accumulate in this dict forever."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import RateLimiter  # noqa: E402


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit_then_denies(self):
        rl = RateLimiter()
        for _ in range(3):
            self.assertTrue(rl.allow("k", 3, 60, now=1000.0))
        self.assertFalse(rl.allow("k", 3, 60, now=1000.0))

    def test_window_sliding_out_allows_again(self):
        rl = RateLimiter()
        for _ in range(3):
            rl.allow("k", 3, 60, now=1000.0)
        self.assertFalse(rl.allow("k", 3, 60, now=1010.0))
        self.assertTrue(rl.allow("k", 3, 60, now=1061.0), "the first 3 hits are now outside the window")

    def test_distinct_keys_are_independent(self):
        rl = RateLimiter()
        for _ in range(3):
            rl.allow("k1", 3, 60, now=1000.0)
        self.assertTrue(rl.allow("k2", 3, 60, now=1000.0), "k2's budget is untouched by k1's")

    def test_idle_key_is_evicted_after_a_sweep(self):
        rl = RateLimiter()
        rl.allow("stale-key", 5, 60, now=1000.0)
        self.assertIn("stale-key", rl._hits)
        # Advance well past both EVICTION_INTERVAL and STALE_AFTER, then touch a
        # DIFFERENT key -- any allow() call can trigger the periodic sweep.
        rl.allow("other-key", 5, 60, now=1000.0 + RateLimiter.STALE_AFTER + 1)
        self.assertNotIn("stale-key", rl._hits, "an idle-past-STALE_AFTER bucket must be swept")

    def test_active_key_survives_a_sweep(self):
        rl = RateLimiter()
        rl.allow("busy-key", 5, 60, now=1000.0)
        # A sweep runs (interval elapsed) but busy-key was just hit again recently.
        rl.allow("busy-key", 5, 60, now=1000.0 + RateLimiter.EVICTION_INTERVAL + 1)
        self.assertIn("busy-key", rl._hits)

    def test_sweep_does_not_run_more_often_than_the_interval(self):
        rl = RateLimiter()
        rl.allow("k", 5, 60, now=1000.0)  # first call always sets _last_sweep
        rl.allow("idle-key", 5, 60, now=1000.0)
        # Advance past STALE_AFTER but NOT past EVICTION_INTERVAL since the last sweep.
        rl.allow("k", 5, 60, now=1000.0 + RateLimiter.EVICTION_INTERVAL - 1)
        self.assertIn("idle-key", rl._hits, "no sweep should have run yet")

    def test_a_sweep_does_not_evict_a_key_within_stale_after(self):
        # A sweep triggering (EVICTION_INTERVAL elapsed) must not evict a key
        # whose hits are still within STALE_AFTER, even though STALE_AFTER is far
        # longer than that key's own (much shorter) rate-limit `window`.
        rl = RateLimiter()
        rl.allow("k", 3, 60, now=1000.0)
        trigger_sweep_at = 1000.0 + RateLimiter.EVICTION_INTERVAL + 1
        rl.allow("other-key", 5, 60, now=trigger_sweep_at)  # triggers a sweep
        self.assertIn("k", rl._hits, "well within STALE_AFTER -- must survive the sweep")


if __name__ == "__main__":
    unittest.main()
