"""Hot and cold pixels found from the dark sets (simulator; no hardware). Run:
   python -X utf8 -m unittest discover -s tests -v   (from the LightKeeper folder)
Uses a temporary LIGHTKEEPER_HOME and run folder."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lightkeeper import hotpix  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402
from lightkeeper.engine import Engine  # noqa: E402
from lightkeeper.schedule import RunPlan  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402
from test_engine_sim import wait_for  # noqa: E402

SIM_HOT = [202, 449, 1646]


class TestFind(unittest.TestCase):
    def test_persistent_pixels_only(self):
        rng = np.random.default_rng(0)
        s = 4500 + rng.normal(0, 250, (5, 2068)) + np.linspace(0, 300, 2068)
        s[:, 100] += 3000                 # hot in every scan
        s[:4, 700] += 3000                # hot in 4 of 5 scans (80 %)
        s[:2, 900] += 3000                # a transient in 2 of 5 scans: not flagged
        s[:, 1500] -= 3000                # cold
        hot, cold = hotpix.find(s)
        self.assertEqual(hot, [100, 700])
        self.assertEqual(cold, [1500])

    def test_fill_interpolates_and_keeps_input(self):
        y = np.arange(10.0); y[4] = 999; valid = np.ones(10, bool); valid[4] = False
        f = hotpix.fill(y, valid)
        self.assertEqual(f[4], 4.0); self.assertEqual(y[4], 999)
        f2 = hotpix.fill(np.vstack([y, 2 * y]), valid)
        self.assertEqual(list(f2[:, 4]), [4.0, 8.0])


class TestRunAndReconnect(unittest.TestCase):
    def test_dark_sets_find_sim_hot_pixels_and_the_list_is_kept(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as td:
            old = os.environ.get("LIGHTKEEPER_HOME"); os.environ["LIGHTKEEPER_HOME"] = home
            try:
                eng = Engine(); eng.start(); ev = []
                eng.send("connect", SIM_ID, True, True)
                wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "IDLE", 10, ev)
                self.assertFalse([e for e in ev if e["type"] == "hot_pixels"])        # nothing saved yet
                now = dt.datetime.now(LOCAL_TZ)
                plan = RunPlan(label="hot pixels", out_dir=td, start_now=True, stop_local=(now + dt.timedelta(seconds=6)).isoformat(),
                               ladder_ms=[50, 100, 200], min_int_ms=50, max_int_ms=200, int_ms=100, dark_s_per_int=0.2,
                               burst_every_min=0, flush_s=2, rotate_min=60)
                eng.send("start", plan.to_dict())
                pre = wait_for(eng, lambda e: e["type"] == "hot_pixels", 30, ev)
                self.assertEqual(pre["hot"], SIM_HOT); self.assertEqual(pre["int_us"], 200_000)
                wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 10, ev)
                eng.send("continue")
                wait_for(eng, lambda e: e["type"] == "need_post_dark", 40, ev)
                eng.send("post_dark")
                closed = wait_for(eng, lambda e: e["type"] == "run_closed", 30, ev)
                run = Path(closed["dir"])
                meta = json.loads((run / "run_metadata.json").read_text())
                h = meta["hot_pixels"]
                self.assertEqual(h["hot"], SIM_HOT)
                self.assertEqual([s["label"] for s in h["sets"]], ["pre", "post"])
                for f in sorted(run.glob("spectra_*.h5")):
                    with h5py.File(f, "r") as hf:
                        self.assertEqual(json.loads(hf.attrs["hot_pixels"]), SIM_HOT, f.name)
                saved = hotpix.load("SIM0001")
                self.assertEqual(saved["hot"], SIM_HOT); self.assertEqual(saved["label"], "post")
                # a later connection (no run) announces the saved list for the live view
                ev.clear(); eng.send("disconnect"); eng.send("connect", SIM_ID, True, True)
                again = wait_for(eng, lambda e: e["type"] == "hot_pixels", 10, ev)
                self.assertEqual(again["hot"], SIM_HOT)
                eng.send("quit"); eng.join(10)
            finally:
                if old is None:
                    os.environ.pop("LIGHTKEEPER_HOME", None)
                else:
                    os.environ["LIGHTKEEPER_HOME"] = old


if __name__ == "__main__":
    unittest.main()
