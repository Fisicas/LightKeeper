"""0.3.9: fixed exposure with back-off. A saturated slot steps the time down by backoff_step_ms (one step per slot, never
below backoff_min_ms); 30 slots in a row that would fit under TARGET_HIGH at the next longer time step it back up, never
above the run's fixed time. Every step gets a dark; resuming an older run with an amended plan widens both ladders."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lightkeeper import engine as E  # noqa: E402
from lightkeeper.schedule import RunPlan, dark_ladder_us, ladder_us  # noqa: E402

MAYA = (8_000, 60_000_000)


class FakeDev:
    def __init__(self, us):
        self.int_us = us

    def set_int(self, us):
        self.int_us = us
        return us


class BackoffTest(unittest.TestCase):
    def test_ladders(self):
        p = RunPlan(int_ms=50, backoff_step_ms=10, backoff_min_ms=10)
        self.assertEqual(ladder_us(p, *MAYA), [10_000, 20_000, 30_000, 40_000, 50_000])
        self.assertEqual(dark_ladder_us(p, *MAYA), [10_000, 20_000, 30_000, 40_000, 50_000, 3_000_000])
        self.assertEqual(ladder_us(RunPlan(int_ms=50), *MAYA), [50_000])                 # off by default (old plans)

    def test_steps(self):
        p = RunPlan(int_ms=50, backoff_step_ms=10, backoff_min_ms=10)
        e = E.Engine(); e.dev = FakeDev(50_000); e.ctx = dict(ladder=ladder_us(p, *MAYA), used_us=set())
        e._scan = lambda: None; e.log = lambda *a: None
        seen = []
        for _ in range(6):                                   # saturated every slot: 50 -> 40 -> 30 -> 20 -> 10, then stays
            e._backoff(61_000.0); seen.append(e.dev.int_us)
        self.assertEqual(seen, [40_000, 30_000, 20_000, 10_000, 10_000, 10_000])
        for _ in range(E.BACKOFF_UP_SLOTS - 1):              # 15k at 10 ms -> 30k predicted at 20 ms: fits, but not yet
            e._backoff(15_000.0)
        self.assertEqual(e.dev.int_us, 10_000)
        e._backoff(15_000.0)
        self.assertEqual(e.dev.int_us, 20_000)
        for _ in range(E.BACKOFF_UP_SLOTS - 1):
            e._backoff(30_000.0)                             # 45k predicted at 30 ms: too high, the count restarts
        e._backoff(35_000.0)
        self.assertEqual(e.dev.int_us, 20_000)
        e.dev.int_us = 50_000
        for _ in range(3 * E.BACKOFF_UP_SLOTS):              # never above the fixed time
            e._backoff(1_000.0)
        self.assertEqual(e.dev.int_us, 50_000)


if __name__ == "__main__":
    unittest.main()
