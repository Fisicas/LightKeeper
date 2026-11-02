"""0.3.8: fixed exposure by default, bursts at their own integration time (B1), darks at two times (B5), temperature
logging, run_summary.json and one version string everywhere. Simulator only; temporary run folder and LIGHTKEEPER_HOME.
Run:  python -X utf8 -m unittest discover -s tests -v"""
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
from lightkeeper import VERSION  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402
from lightkeeper.engine import Engine  # noqa: E402
from lightkeeper.schedule import RunPlan, burst_us, dark_ladder_us, estimates, ladder_us  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402
from test_engine_sim import wait_for  # noqa: E402

MAYA = (8000, 65_000_000)


class TestPlanDefaults(unittest.TestCase):
    def test_fixed_500_with_50_ms_bursts(self):
        """Handoff §5, B1: min_int 500 ms and a 50 ms burst -> bursts at 50 ms, and the darks include 50 ms."""
        p = RunPlan()
        self.assertFalse(p.auto_exposure); self.assertEqual(p.int_ms, 500)
        self.assertEqual(ladder_us(p, *MAYA), [500_000])
        self.assertEqual(burst_us(p, *MAYA), 50_000)
        self.assertEqual(dark_ladder_us(p, *MAYA), [50_000, 500_000, 3_000_000])        # B5: a second, long dark time
        p.stop_local = (dt.datetime.now(LOCAL_TZ) + dt.timedelta(hours=2)).isoformat()
        e = estimates(p, 2068, *MAYA)
        self.assertEqual(e["burst_int_ms"], 50); self.assertEqual(e["dark_ladder_ms"], [50, 500, 3000])

    def test_automatic_keeps_the_ladder_and_adds_the_burst_dark(self):
        p = RunPlan(auto_exposure=True)
        self.assertEqual(ladder_us(p, *MAYA), [500_000, 800_000, 1_000_000, 1_500_000, 2_000_000, 3_000_000])
        self.assertEqual(dark_ladder_us(p, *MAYA)[0], 50_000)

    def test_no_bursts_no_burst_dark(self):
        self.assertEqual(dark_ladder_us(RunPlan(burst_every_min=0), *MAYA), [500_000, 3_000_000])

    def test_old_plans_still_load(self):
        old = {k: v for k, v in RunPlan(auto_exposure=True).to_dict().items() if k != "long_dark_ms"}
        self.assertEqual(RunPlan.from_dict(old).long_dark_ms, 3000.0)


def run_once(td, home, temperature=True):
    """A short fixed-exposure run with one burst; returns the run folder and the events."""
    os.environ["LIGHTKEEPER_HOME"] = home
    os.environ["LIGHTKEEPER_SIM_TEMPERATURE"] = "1" if temperature else "0"
    eng = Engine(); eng.start(); ev = []
    try:
        eng.send("connect", SIM_ID, True, True)
        wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "IDLE", 10, ev)
        now = dt.datetime.now(LOCAL_TZ)
        b = now + dt.timedelta(seconds=12)
        plan = RunPlan(label="fixed burst", out_dir=td, start_now=True, stop_local=(now + dt.timedelta(seconds=24)).isoformat(),
                       int_ms=100, long_dark_ms=300, burst_max_int_ms=20, burst_every_min=60,
                       burst_offset_min=b.minute + b.second / 60.0, burst_s=3, dark_s_per_int=0.2, flush_s=2)
        eng.send("start", plan.to_dict())
        wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 30, ev)
        eng.send("continue")
        wait_for(eng, lambda e: e["type"] == "need_post_dark", 60, ev)
        eng.send("post_dark")
        closed = wait_for(eng, lambda e: e["type"] == "run_closed", 30, ev)
    finally:
        eng.send("quit"); eng.join(10)
    return Path(closed["dir"]), ev


class TestRun(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.home = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("LIGHTKEEPER_HOME", "LIGHTKEEPER_SIM_TEMPERATURE")}

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.td.cleanup(); self.home.cleanup()

    def read(self, run):
        out = dict(int_us=[], burst_us=[], dark_us=set(), temp=[], app=set())
        for f in sorted(run.glob("spectra_*.h5")):
            with h5py.File(f, "r") as h:
                out["app"].add(h.attrs["app"])
                if "blocks" in h:
                    out["int_us"] += list(h["blocks/int_us"][:])
                    if "temperature_c" in h["blocks"]:
                        out["temp"] += list(h["blocks/temperature_c"][:])
                if "burst" in h:
                    out["burst_us"] += list(h["burst/int_us"][:])
                if "dark_scans" in h:
                    out["dark_us"] |= {int(u) for u in h["dark_scans/int_us"][:]}
        return out

    def test_fixed_run_bursts_darks_temperature_summary_version(self):
        run, ev = run_once(self.td.name, self.home.name, temperature=True)
        d = self.read(run); meta = json.loads((run / "run_metadata.json").read_text())
        self.assertEqual(set(d["int_us"]), {100_000})                                  # fixed exposure only
        self.assertTrue(d["burst_us"]); self.assertEqual(set(d["burst_us"]), {20_000})   # B1: not snapped to 100 ms
        self.assertEqual(d["dark_us"], {20_000, 100_000, 300_000})                     # burst + fixed + long dark
        self.assertEqual(len(d["temp"]), len(d["int_us"])); self.assertTrue(np.isfinite(d["temp"]).all())
        self.assertEqual(meta["temperature_source"], "simulated")
        s = json.loads((run / "run_summary.json").read_text())
        self.assertEqual(s["exposure"]["mode"], "fixed"); self.assertEqual(s["integration_changes"], [])
        self.assertEqual(s["bursts"]["int_ms"], [20.0]); self.assertGreater(s["bursts"]["scans_per_s_min"], 20)
        self.assertEqual(s["dark_sets"]["integration_times_ms"], [20.0, 100.0, 300.0])
        self.assertTrue(s["temperature_c"]["min"] <= s["temperature_c"]["max"])
        self.assertIsNotNone(meta.get("post_darks_delay_min"))
        app = f"LightKeeper {VERSION}"                                                  # one version string everywhere
        self.assertEqual(meta["app"], app); self.assertEqual(d["app"], {app}); self.assertEqual(s["app"], app)
        self.assertEqual(json.loads((run / "run_state.json").read_text())["app"], app)
        src = (Path(__file__).resolve().parents[1] / "lightkeeper" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('setWindowTitle(f"{APP_NAME} {VERSION}")', src)
        print(f"\n  bursts {s['bursts']}, temperature {s['temperature_c']}")

    def test_no_sensor_no_temperature_column(self):
        run, _ = run_once(self.td.name, self.home.name, temperature=False)
        d = self.read(run); meta = json.loads((run / "run_metadata.json").read_text())
        self.assertEqual(d["temp"], []); self.assertTrue(meta["temperature_source"].startswith("none"))
        s = json.loads((run / "run_summary.json").read_text())
        self.assertFalse(s["temperature_c"]["logged"])


if __name__ == "__main__":
    unittest.main()
