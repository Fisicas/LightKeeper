r"""Daylight saving: a run armed on EDT the evening before the 1 Nov 2026 fall-back starts and stops on EST. Run:
   python -X utf8 -m unittest tests.test_dst_2026_11_01 -v"""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lightkeeper.schedule import RunPlan, burst_times, estimates, next_sun_event  # noqa: E402
from lightkeeper.storage import LOCAL_TZ, Writer  # noqa: E402

EDT, EST = dt.timedelta(hours=-4), dt.timedelta(hours=-5)
ARMED = dt.datetime(2026, 10, 31, 21, 0, tzinfo=LOCAL_TZ)        # Sat evening, EDT
PLAN = RunPlan(start_now=False, start_event="astronomical dawn (-18°)", start_offset_min=-20,
               stop_event="astronomical dusk (-18°)", stop_offset_min=20,
               site_lat=33.749, site_lon=-84.388)    # Atlanta, GA: Eastern time, public reference site


def resolve(plan, now):
    """Same arithmetic as RunPlan.start_dt / stop_dt, with 'now' fixed."""
    t0 = next_sun_event(plan.start_event, now - dt.timedelta(minutes=plan.start_offset_min), plan.site_lat, plan.site_lon) \
        + dt.timedelta(minutes=plan.start_offset_min)
    t1 = next_sun_event(plan.stop_event, t0 - dt.timedelta(minutes=plan.stop_offset_min), plan.site_lat, plan.site_lon) \
        + dt.timedelta(minutes=plan.stop_offset_min)
    return t0, t1


class TestDST(unittest.TestCase):
    def test_armed_edt_resolves_to_est_times(self):
        self.assertEqual(ARMED.utcoffset(), EDT)
        t0, t1 = resolve(PLAN, ARMED)
        print(f"\n  start {t0:%a %d %b %H:%M:%S %Z}, stop {t1:%a %d %b %H:%M:%S %Z}")
        self.assertEqual((t0.date(), t1.date()), (dt.date(2026, 11, 1), dt.date(2026, 11, 1)))
        self.assertEqual((t0.utcoffset(), t1.utcoffset()), (EST, EST))
        self.assertEqual(t0.tzname(), "EST")
        # ~05:12 EST start, ~19:30 EST stop (NOAA solar calculator, Atlanta)
        self.assertLess(abs((t0.replace(tzinfo=None) - dt.datetime(2026, 11, 1, 5, 14)).total_seconds()), 5 * 60)
        self.assertLess(abs((t1.replace(tzinfo=None) - dt.datetime(2026, 11, 1, 19, 32)).total_seconds()), 5 * 60)

    def test_countdown_and_duration_use_real_elapsed_time(self):
        t0, t1 = resolve(PLAN, ARMED)
        true_s = t0.timestamp() - ARMED.timestamp()                       # 9 h 14 min, not the wall-clock 8 h 14 min
        self.assertAlmostEqual(true_s / 3600, 9 + 14 / 60, delta=0.1)
        # the window's countdown: run times arrive as ISO strings, 'now' is a zone-aware datetime
        rt0 = dt.datetime.fromisoformat(t0.isoformat())
        self.assertAlmostEqual((rt0 - ARMED).total_seconds(), true_s, delta=1)
        # estimates() for an armed run from EDT now to EST stop
        e = estimates(PLAN, 2068, 7200, 60_000_000, t0=ARMED, t1=t1)
        self.assertAlmostEqual(e["duration_h"], (t1.timestamp() - ARMED.timestamp()) / 3600, delta=0.01)

    def test_bursts_stay_on_local_half_hours(self):
        t0, t1 = resolve(PLAN, ARMED)
        b = burst_times(PLAN, t0, t1)
        self.assertTrue(b and all(x.minute == 30 and x.utcoffset() == EST for x in b))

    def test_hourly_file_names_unique_across_repeated_hour(self):
        with tempfile.TemporaryDirectory() as td:
            w = Writer(Path(td), np.linspace(200, 1100, 8), {}, 60, 30.0)
            utc = [dt.datetime(2026, 11, 1, h, 30, tzinfo=dt.timezone.utc) for h in (4, 5, 6, 7, 10)]  # 00:30 EDT .. 05:30 EST
            names = [w._path(int(t.timestamp() * 1e9)).name for t in utc]
            print("\n  " + ", ".join(names))
            self.assertEqual(len(set(names)), len(names))
            self.assertEqual(names, sorted(names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
