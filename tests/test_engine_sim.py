"""Engine + storage + calibration tests against the simulator (no hardware). Run:
   python -X utf8 -m unittest discover -s tests -v   (from the LightKeeper folder)
Uses a temporary directory; never touches a real spectrometer."""
import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lightkeeper.calibration import LAMPS, fit_calibration, reference_lines  # noqa: E402
from lightkeeper.devices import SIM_ID, SimDevice  # noqa: E402
from lightkeeper.engine import Engine  # noqa: E402
from lightkeeper.schedule import RunPlan, estimates  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402


def wait_for(engine, pred, timeout, sink):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            e = engine.evt.get(timeout=0.2)
        except Exception:
            continue
        sink.append(e)
        if pred(e):
            return e
    raise AssertionError(f"timeout; last events: {[x['type'] for x in sink[-15:]]}")


class TestEngine(unittest.TestCase):
    def test_full_run_with_pre_and_post_darks_and_burst(self):
        with tempfile.TemporaryDirectory() as td:
            eng = Engine(); eng.start(); ev = []
            eng.send("connect", SIM_ID, True, True)
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "IDLE", 10, ev)
            now = dt.datetime.now(LOCAL_TZ)
            plan = RunPlan(label="unit test", out_dir=td, start_now=True, stop_local=(now + dt.timedelta(seconds=22)).isoformat(),
                           ladder_ms=[8, 10, 20, 50, 100, 200], min_int_ms=8, max_int_ms=200, int_ms=100, dark_s_per_int=0.2,
                           burst_every_min=1, burst_offset_min=(now.minute + 1) % 60 if now.second > 45 else now.minute + 0, burst_s=3,
                           flush_s=5, rotate_min=1)
            # schedule the burst ~8 s after start regardless of the clock
            b = now + dt.timedelta(seconds=8)
            plan.burst_offset_min = b.minute + b.second / 60.0
            plan.burst_every_min = 60
            eng.send("start", plan.to_dict())
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 30, ev)
            eng.send("continue")
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "RECORDING", 10, ev)
            wait_for(eng, lambda e: e["type"] == "need_post_dark", 40, ev)
            eng.send("post_dark")
            closed = wait_for(eng, lambda e: e["type"] == "run_closed", 30, ev)
            eng.send("quit"); eng.join(10)
            run = Path(closed["dir"])
            meta = json.loads((run / "run_metadata.json").read_text())
            self.assertEqual(meta["format_version"], 2)
            self.assertEqual([c["when"] for c in meta["clock_checks"]][:1] + [meta["clock_checks"][-1]["when"]], ["start", "stop"])
            self.assertIn("diffuser_id", meta["plan"])
            blocks = 0; darks = []; burst = 0
            for f in sorted(run.glob("spectra_*.h5")):
                with h5py.File(f, "r") as h:
                    self.assertEqual(h.attrs["format_version"], 2)                 # 0.3.1
                    self.assertEqual(h.attrs["view_az_deg"], "null")             # field present, not set
                    if "blocks" in h:
                        blocks += h["blocks/mean"].shape[0]
                        self.assertEqual(h["blocks/mean"].shape[1], 2068)
                        self.assertIn("sd", h["blocks"])
                    if "dark_scans" in h:
                        darks += [x.decode() if isinstance(x, bytes) else x for x in h["dark_scans/label"][:]]
                    if "burst" in h:
                        burst += h["burst/spectrum"].shape[0]
            self.assertGreater(blocks, 5)
            self.assertIn("pre", darks); self.assertIn("post", darks)
            self.assertGreater(burst, 50)
            self.assertTrue((run / "events.csv").exists() and (run / "run_metadata.json").exists())
            print(f"\n  slots {blocks}, dark scans {len(darks)}, burst scans {burst}, files {len(list(run.glob('*.h5')))}")


class TestCalibration(unittest.TestCase):
    def test_recovers_simulated_axis_error(self):
        sim = SimDevice(); sim.mode = "lamp"
        L = np.concatenate([__import__("lightkeeper.calibration", fromlist=["x"]).load_species(s) for s in LAMPS["UVC germicidal (Hg + Ar fill)"]])
        L = L[L["rel"] >= 0.02]; sim.lamp_lines = (L["wl"], L["rel"]); sim.set_int(50_000)
        lamp = np.mean([sim.scan() for _ in range(5)], 0); sim.mode = "dark"
        dark = np.mean([sim.scan() for _ in range(5)], 0)
        r = fit_calibration(sim.wavelengths, lamp - dark, "UVC germicidal (Hg + Ar fill)")
        self.assertTrue(r["ok"], r.get("message"))
        px = np.arange(2068); fit = np.polyval(np.array(r["coefficients_low_to_high"])[::-1], px)
        m = (sim.true_wl > 300) & (sim.true_wl < 1000)
        err = np.abs(fit[m] - sim.true_wl[m]).max()
        print(f"\n  lines used {r['n_used']}, rms {r['rms_nm']:.3f} nm, max axis error vs truth 300-1000 nm {err:.3f} nm")
        self.assertLess(err, 0.1)

    def test_every_lamp_has_reference_lines(self):
        for lamp in LAMPS:
            n = len(reference_lines(lamp, 200, 1120))
            if lamp.startswith("Sodium"):
                # Na D1/D2 are 0.6 nm apart (blend at this resolution) and other Na I lines are weak: Na alone cannot calibrate
                self.assertLess(n, 6, lamp); continue
            self.assertGreater(n, 0, lamp)


class TestPlan(unittest.TestCase):
    def test_estimates(self):
        now = dt.datetime.now(LOCAL_TZ)
        p = RunPlan(stop_local=(now + dt.timedelta(hours=10)).isoformat())
        e = estimates(p, 2068, 8000, 65_000_000)
        self.assertEqual(len(e["bursts"]), 10)
        self.assertTrue(60 < e["dark_session_s"] < 130)
        print(f"\n  10 h plan: {e['slots']} slots, {e['data_gb_uncompressed']:.2f} GB, dark set {e['dark_session_s']:.0f} s, ladder {e['ladder_ms']}")


class TestResumeAfterStop(unittest.TestCase):
    def test_interrupted_run_resumed_after_its_stop_goes_to_post_run_darks(self):
        """0.2.0: interrupt mid-recording, resume only after the scheduled stop -> POST_RUN, darks, closed, 1 segment."""
        with tempfile.TemporaryDirectory() as td:
            eng = Engine(); eng.start(); ev = []
            eng.send("connect", SIM_ID, True, True)
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "IDLE", 10, ev)
            now = dt.datetime.now(LOCAL_TZ)
            plan = RunPlan(label="resume after stop", out_dir=td, start_now=True, stop_local=(now + dt.timedelta(seconds=14)).isoformat(),
                           ladder_ms=[8, 20, 100], min_int_ms=8, max_int_ms=100, int_ms=100, dark_s_per_int=0.2, burst_every_min=0, flush_s=1)
            eng.send("start", plan.to_dict())
            run = Path(wait_for(eng, lambda e: e["type"] == "run", 20, ev)["dir"])
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 30, ev)
            eng.send("continue")
            wait_for(eng, lambda e: e["type"] == "slot" and e["n_slots"] >= 3, 15, ev)
            eng.send("interrupt")
            wait_for(eng, lambda e: e["type"] == "run_interrupted", 10, ev)
            st = json.loads((run / "run_state.json").read_text()); self.assertEqual(st["status"], "interrupted")
            while dt.datetime.now(LOCAL_TZ) < now + dt.timedelta(seconds=16):
                time.sleep(0.5)
            eng.send("resume", str(run))
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "POST_RUN", 20, ev)
            eng.send("post_dark")
            wait_for(eng, lambda e: e["type"] == "run_closed", 30, ev)
            eng.send("quit"); eng.join(10)
            st = json.loads((run / "run_state.json").read_text())
            meta = json.loads((run / "run_metadata.json").read_text())
            self.assertEqual(st["status"], "closed"); self.assertEqual(len(meta["resumes"]), 1)
            labels = []; segs = []
            for f in run.glob("spectra_*.h5"):
                with h5py.File(f, "r") as h:
                    labels += [x.decode() if isinstance(x, bytes) else x for x in h["dark_scans/label"][:]]
                    segs += list(h["blocks/segment"][:])
            self.assertEqual(set(segs), {0}); self.assertIn("post", labels)
            print(f"\n  resumed after stop: {len(segs)} slots (segment 0 only), recording_end {meta.get('recording_end_local')}")


class TestBurstSkip(unittest.TestCase):
    """0.3.1: a burst whose predicted fill is < 1 % of full scale is skipped and logged, not run."""
    def _ctx(self, p995):
        logged = []
        ctx = dict(plan=RunPlan(), writer=None, burst_id=0, ladder=[50_000, 500_000, 1_000_000], last_p995=p995,
                   last_p995_us=500_000, m0=time.monotonic_ns(), slot_ns=1_000_000_000, k=0, stop_req=False,
                   events=lambda ev, d: logged.append((ev, d)) or f"{ev}: {d}")
        return ctx, logged

    def test_defaults(self):
        p = RunPlan()
        self.assertEqual((p.min_int_ms, p.max_int_ms, p.int_ms, p.burst_max_int_ms), (500.0, 3000.0, 500.0, 50.0))

    def test_dim_burst_is_skipped_with_prediction(self):
        eng = Engine(); eng.dev = SimDevice(); eng.dev.int_us = 500_000
        eng.ctx, logged = self._ctx(p995=5000.0)        # 5000 x 50/500 = 500 counts < 600
        eng._burst()
        self.assertEqual([e for e, _ in logged], ["burst_skipped"])
        self.assertIn("500 counts", logged[0][1])
        self.assertEqual(eng.dev.int_us, 500_000)       # exposure untouched


class TestReconnectRace(unittest.TestCase):
    """0.3.2: on 1 Nov the recorder queued a second connect behind 'start'; after the pre-run darks it dropped the armed
    run to IDLE (no scans, no recording at the start time). A connect while a run exists must be ignored."""
    def test_connect_queued_behind_start_keeps_run_armed(self):
        with tempfile.TemporaryDirectory() as td:
            eng = Engine(); eng.start(); ev = []
            eng.send("connect", SIM_ID, True, True)
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "IDLE", 10, ev)
            now = dt.datetime.now(LOCAL_TZ)
            plan = RunPlan(label="race", out_dir=td, start_now=False, start_local=(now + dt.timedelta(hours=3)).isoformat(),
                           stop_local=(now + dt.timedelta(hours=4)).isoformat(), ladder_ms=[8, 20], min_int_ms=8,
                           max_int_ms=20, int_ms=20, dark_s_per_int=0.1, burst_every_min=0)
            eng.send("start", plan.to_dict())
            eng.send("connect", SIM_ID, True, True)             # the late reconnect, queued behind start
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 30, ev)
            wait_for(eng, lambda e: e["type"] == "log" and "connect_ignored" in e["text"], 10, ev)
            eng.send("continue")
            wait_for(eng, lambda e: e["type"] == "state" and e["state"] == "STANDBY", 10, ev)
            n0 = sum(e["type"] == "scan" for e in ev)
            wait_for(eng, lambda e: e["type"] == "scan", 5, ev); wait_for(eng, lambda e: e["type"] == "scan", 5, ev)
            self.assertEqual(eng.state, "STANDBY"); self.assertIsNotNone(eng.ctx)
            self.assertGreater(sum(e["type"] == "scan" for e in ev), n0)   # live scans keep coming while armed
            self.assertNotIn("IDLE", [e["state"] for e in ev if e["type"] == "state"][1:])
            eng.send("stop"); wait_for(eng, lambda e: e["type"] == "need_post_dark", 20, ev)
            eng.send("post_dark"); wait_for(eng, lambda e: e["type"] == "run_closed", 20, ev)
            eng.send("quit"); eng.join(10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
