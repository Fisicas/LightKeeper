"""End-to-end test of the background recorder with the simulator (no hardware; temporary folders only).

A real supervisor + recorder process pair is started exactly as the window starts them. The test then
  1. attaches like a window, sends Continue after the pre-run darks, detaches and re-attaches (history must survive);
  2. KILLS the recorder process mid-recording: the supervisor must resume the run as segment 1 automatically;
  3. lets the run reach its scheduled stop, records post-run darks, and checks the files:
     blocks from segments 0 and 1, equal column lengths, pre + post darks, 'resumed' in events.csv, run closed,
     session file and open-run registry entry removed, supervisor gone;
  4. a second run is INTERRUPTED from the client (exit code 10, run left open) and resumed by hand to completion.
Run:  python -X utf8 -m unittest tests.test_recorder_resume -v       (QT_QPA_PLATFORM=offscreen is set automatically)
"""
import datetime as dt
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import h5py
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def kill_hard(pid):
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


class Client:
    def __init__(self, procs, serial, timeout=40):
        from lightkeeper.link import RecorderLink
        t0 = time.time(); self.link = None
        while time.time() - t0 < timeout:
            ss = [s for s in procs.live_sessions() if s.get("serial") == serial]
            if ss:
                try:
                    self.link = RecorderLink(ss[0]); break
                except Exception:
                    pass
            time.sleep(0.3)
        assert self.link, "no recorder session appeared"
        self.events = []
        self.snapshot = self.link.evt.get(timeout=5)
        assert self.snapshot["type"] == "snapshot"

    def wait(self, pred, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                e = self.link.evt.get(timeout=0.3)
            except Exception:
                continue
            self.events.append(e)
            if pred(e):
                return e
        raise AssertionError(f"timeout; last: {[(x['type'], x.get('state', '')) for x in self.events[-12:]]}")

    def close(self):
        self.link.close()


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        os.environ["LIGHTKEEPER_HOME"] = str(Path(self.td.name) / "home")
        import importlib
        import lightkeeper.procs as procs
        self.procs = importlib.reload(procs)
        self.out = Path(self.td.name) / "runs"; self.out.mkdir()

    def tearDown(self):
        for s in self.procs.live_sessions():
            kill_hard(s["pid"])
        self.td.cleanup()

    def _plan(self, stop_s, label):
        from lightkeeper.schedule import RunPlan
        from lightkeeper.storage import LOCAL_TZ
        now = dt.datetime.now(LOCAL_TZ)
        b = now + dt.timedelta(seconds=14)
        p = RunPlan(auto_exposure=True, label=label, out_dir=str(self.out), start_now=True, stop_local=(now + dt.timedelta(seconds=stop_s)).isoformat(),
                    ladder_ms=[8, 10, 20, 50, 100, 200], min_int_ms=8, max_int_ms=200, int_ms=100, dark_s_per_int=0.2, flush_s=2, rotate_min=60,
                    burst_every_min=60, burst_offset_min=b.minute + b.second / 60.0, burst_s=3, auto_resume_logon=False)
        f = self.procs.app_home() / "pending" / f"plan_{label}.json"
        self.procs.write_json_atomic(f, p.to_dict())
        return f

    def _start(self, *args):
        return self.procs.spawn_detached(self.procs.entry_args("--supervisor", *args))

    def test_kill_resume_and_close(self):
        sup = self._start("--plan", str(self._plan(55, "kill_test")), "--serial", "SIM0001")
        c = Client(self.procs, "SIM0001")
        c.wait(lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 40)
        c.link.send("continue")
        c.wait(lambda e: e["type"] == "state" and e["state"] == "RECORDING", 15)
        c.wait(lambda e: e["type"] == "slot" and e["n_slots"] >= 6, 20)
        # a window that closes and comes back: recording continues, history is replayed
        c.close(); time.sleep(2.0)
        c = Client(self.procs, "SIM0001")
        self.assertIn(c.snapshot["state"], ("RECORDING", "BURST"))
        self.assertGreaterEqual(len(c.snapshot["history"]), 6)
        self.assertIsNotNone(c.snapshot["run"]); run_dir = Path(c.snapshot["run"]["dir"])
        c.wait(lambda e: e["type"] == "slot" and e["n_slots"] >= 14, 30)       # includes the burst at ~+14 s
        time.sleep(2.5)                                                        # let a flush land
        pid0 = c.snapshot["pid"]; c.close()
        kill_hard(pid0)                                                        # hard crash of the recorder
        t_kill = time.time()
        c = Client(self.procs, "SIM0001", timeout=60)
        self.assertNotEqual(c.snapshot["pid"], pid0)
        run_ev = c.snapshot["run"] or c.wait(lambda e: e["type"] == "run", 30)
        if not run_ev.get("resumed"):
            run_ev = c.wait(lambda e: e["type"] == "run", 30)
        self.assertTrue(run_ev["resumed"]); self.assertEqual(run_ev["segment"], 1)
        c.wait(lambda e: e["type"] == "slot", 30)
        print(f"\n  recorder restarted and recording again {time.time() - t_kill:.1f} s after the kill")
        c.wait(lambda e: e["type"] == "state" and e["state"] == "POST_RUN", 70)
        c.link.send("post_dark")
        c.wait(lambda e: e["type"] == "run_closed", 40)
        c.close()
        self.assertEqual(sup.wait(30), 0)
        self._check_files(run_dir, segments={0, 1})
        ev = (run_dir / "events.csv").read_text()
        self.assertIn("resumed", ev)
        self.assertFalse(self.procs.session_path("SIM0001").exists())
        self.assertEqual(self.procs.open_runs(), [])
        self.assertFalse((run_dir / "supervisor.json").exists())

    def test_interrupt_then_resume_by_hand(self):
        sup = self._start("--plan", str(self._plan(40, "interrupt_test")), "--serial", "SIM0001")
        c = Client(self.procs, "SIM0001")
        c.wait(lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 40)
        c.link.send("continue")
        c.wait(lambda e: e["type"] == "slot" and e["n_slots"] >= 4, 20)
        runs = [e for e in c.events if e["type"] == "run"]
        run_dir = Path(c.snapshot["run"]["dir"]) if c.snapshot["run"] else Path(runs[-1]["dir"])
        c.link.send("interrupt")
        c.wait(lambda e: e["type"] == "run_interrupted", 20)
        c.close()
        self.assertEqual(sup.wait(30), 0)                         # supervisor ends: exit 10 is final
        st = self.procs.read_json(run_dir / "run_state.json")
        self.assertEqual(st["status"], "interrupted")
        self.assertEqual([r["run_dir"] for r in self.procs.open_runs()], [str(run_dir)])
        time.sleep(3)
        sup = self._start("--resume", str(run_dir))
        c = Client(self.procs, "SIM0001")
        c.wait(lambda e: e["type"] == "slot", 30)
        c.wait(lambda e: e["type"] == "state" and e["state"] == "POST_RUN", 60)
        c.link.send("post_dark")
        c.wait(lambda e: e["type"] == "run_closed", 40)
        c.close()
        self.assertEqual(sup.wait(30), 0)
        self._check_files(run_dir, segments={0, 1})
        self.assertEqual(self.procs.read_json(run_dir / "run_state.json")["status"], "closed")

    def _check_files(self, run_dir, segments):
        seg = []; labels = []; burst = 0
        for f in sorted(run_dir.glob("spectra_*.h5")):
            with h5py.File(f, "r") as h:
                for t in ("blocks", "dark_scans", "burst"):
                    if t in h:
                        lens = {h[t][k].shape[0] for k in h[t]}
                        self.assertEqual(len(lens), 1, f"{f.name}/{t} column lengths {lens}")
                if "blocks" in h:
                    seg += list(h["blocks/segment"][:])
                    t = h["blocks/t_wall_ns"][:]
                    self.assertTrue(np.all(np.diff(t) > 0), "slot times must increase")
                if "dark_scans" in h:
                    labels += [x.decode() if isinstance(x, bytes) else x for x in h["dark_scans/label"][:]]
                if "burst" in h:
                    burst += h["burst/spectrum"].shape[0]
        self.assertEqual(set(seg), segments)
        self.assertIn("pre", labels); self.assertIn("post", labels)
        meta = self.procs.read_json(run_dir / "run_metadata.json")
        self.assertEqual(len(meta.get("resumes", [])), 1)
        print(f"  {run_dir.name}: slots per segment { {s: seg.count(s) for s in sorted(set(seg))} }, burst scans {burst}, "
              f"gap {meta['resumes'][0]['gap_since_last_slot_s']:.1f} s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
