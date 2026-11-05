"""0.3.13: the file thread, whole-chunk writes, inline raw windows, longer darks, the burst minimum, expected size, weather.
Simulator only; temporary folders; no network. Run from the repository folder:
   python -X utf8 -m unittest tests.test_raw_windows_0313 -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lightkeeper import weather  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402
from lightkeeper.engine import Engine  # noqa: E402
from lightkeeper.schedule import RunPlan, burst_times, dark_scan_count, estimates, old_acquisition_defaults  # noqa: E402
from lightkeeper.storage import LOCAL_TZ, Events, Writer  # noqa: E402

T0 = int(dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def frame(i, n=64):
    return dict(t_wall_ns=np.int64(T0 + i * 10_000_000), int_us=np.int32(10_000), burst_id=np.int32(1),
                spectrum=np.arange(n, dtype=np.float32) + float(i))


def slot(i, n=64):
    return dict(t_wall_ns=np.int64(T0 + i * 1_000_000_000), n_scans=np.int16(100),
                mean=np.arange(n, dtype=np.float32) + i / 3, sd=np.full(n, 2.0 + i, dtype=np.float32))


def read(path, table, key):
    with h5py.File(path, "r") as h:
        return h[table][key][:]


def holes(path):
    stored = []
    with h5py.File(path, "r") as h:
        h.visititems(lambda n, o: stored.append(o.id.get_storage_size()) if isinstance(o, h5py.Dataset) else None)
    return 1 - sum(stored) / Path(path).stat().st_size


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def writer(self, **kw):
        return Writer(self.dir, np.linspace(200, 1100, 64), dict(a=1), 60, 30.0, **kw)

    def test_ordinary_write_stores_whole_chunks_and_a_forced_one_stores_the_rest(self):
        w = self.writer(block_rows=30)
        for i in range(70):
            w.add("blocks", slot(i))
        w.submit(); w.worker.drain(30)
        f = list(self.dir.glob("spectra_*.h5"))[0]
        self.assertEqual(read(f, "blocks", "mean").shape[0], 60)
        self.assertEqual(w.snapshot()[0]["blocks"], 60)
        w.flush()
        self.assertEqual(read(f, "blocks", "n_scans").shape[0], 70)
        self.assertEqual(w.snapshot()[0]["blocks"], 70)
        np.testing.assert_array_equal(read(f, "blocks", "t_wall_ns"), [T0 + i * 1_000_000_000 for i in range(70)])
        w.close()

    def test_a_short_first_write_does_not_create_a_file(self):
        w = self.writer(block_rows=30)
        for i in range(5):
            w.add("blocks", slot(i))
        w.submit(); w.worker.drain(30)
        self.assertEqual(list(self.dir.glob("spectra_*.h5")), [])
        w.close()
        self.assertEqual(len(list(self.dir.glob("spectra_*.h5"))), 1)

    def test_a_table_with_nothing_to_store_is_not_created_in_an_existing_file(self):
        w = self.writer(block_rows=2)
        for i in range(2):
            w.add("blocks", slot(i))
        w.add("burst", frame(0))
        w.submit(); w.worker.drain(30)
        with h5py.File(list(self.dir.glob("spectra_*.h5"))[0], "r") as h:
            self.assertIn("blocks", h); self.assertNotIn("burst", h)
        w.close()

    def test_repeated_small_flushes_leave_almost_no_holes(self):
        w = Writer(self.dir, np.linspace(200, 1100, 2068), dict(a=1), 60, 30.0, block_rows=30)
        rng = np.random.default_rng(1)
        for i in range(600):
            r = slot(i, 2068); r["mean"] = rng.normal(1000, 20, 2068).astype(np.float32); r["sd"] = rng.normal(20, 2, 2068).astype(np.float32)
            w.add("blocks", r)
            if i % 7 == 6:               # flushes that do not line up with the chunks
                w.submit()
        w.close()
        self.assertLess(holes(list(self.dir.glob("spectra_*.h5"))[0]), 0.02)

    def test_new_datasets_use_the_planned_filters(self):
        w = self.writer(block_rows=30)
        for i in range(30):
            w.add("blocks", slot(i))
        for i in range(800):
            w.add("burst", frame(i))
        w.close()
        with h5py.File(list(self.dir.glob("spectra_*.h5"))[0], "r") as h:
            m, s = h["blocks/mean"], h["burst/spectrum"]
            self.assertEqual((m.chunks, m.compression, m.shuffle), ((30, 64), "gzip", True))
            self.assertEqual((s.chunks, s.compression, s.shuffle), ((400, 16), "gzip", False))

    def test_stored_frames_read_back_exactly(self):
        w = self.writer()
        want = np.stack([frame(i)["spectrum"] for i in range(450)])
        for i in range(450):
            w.add("burst", frame(i))
        w.close()
        np.testing.assert_array_equal(read(list(self.dir.glob("spectra_*.h5"))[0], "burst", "spectrum"), want)

    def test_a_file_with_older_chunks_is_continued_on_its_own_chunks(self):
        f = self.dir / "spectra_20261105_0700.h5"
        with h5py.File(f, "w") as h:
            h.create_dataset("wavelength_nm_device", data=np.linspace(200, 1100, 64))
            d = h.require_group("blocks").create_dataset("mean", shape=(10, 64), maxshape=(None, 64), chunks=(64, 64), compression="gzip", dtype="f4")
            d[:] = 1.0
        w = self.writer(files=[str(f)], block_rows=30)
        for i in range(60):
            w.add("blocks", dict(t_wall_ns=np.int64(T0 + i), mean=np.full(64, 2.0, dtype=np.float32)))
        w.submit(); w.worker.drain(30)
        with h5py.File(f, "r") as h:
            self.assertEqual(h["blocks/mean"].chunks, (64, 64))
            self.assertEqual(h["blocks/mean"].shape[0], 64)              # filled up to its own chunk edge, the rest held
        w.close()
        self.assertEqual(read(f, "blocks", "mean").shape[0], 70)

    def test_failed_write_is_retried_without_duplicate_or_lost_rows(self):
        w = self.writer(block_rows=10)
        real = h5py.Dataset.__setitem__
        fails = []

        def flaky(ds, key, val):         # fail partway through a table's columns, once
            if ds.name.endswith("/sd") and not fails:
                fails.append(1)
                raise OSError("simulated write failure")
            return real(ds, key, val)
        with mock.patch("lightkeeper.storage.RETRY_S", 0.05), mock.patch.object(h5py.Dataset, "__setitem__", flaky):
            for i in range(20):
                w.add("blocks", slot(i))
            w.flush()
        self.assertTrue(fails)
        f = list(self.dir.glob("spectra_*.h5"))[0]
        with h5py.File(f, "r") as h:
            self.assertEqual({h["blocks"][k].shape[0] for k in h["blocks"]}, {20})
            np.testing.assert_array_equal(h["blocks/n_scans"][:], [100] * 20)
            np.testing.assert_array_equal(h["blocks/t_wall_ns"][:], [T0 + i * 1_000_000_000 for i in range(20)])
        self.assertTrue(any("write failed" in n for n in w.notes) and any("works again" in n for n in w.notes), w.notes)
        w.close()

    def test_a_missing_drive_waits_and_makes_no_part_files(self):
        gone = self.dir / "not_there_yet"
        w = Writer(gone, np.linspace(200, 1100, 64), {}, 60, 30.0, block_rows=5)
        with mock.patch("lightkeeper.storage.RETRY_S", 0.05):
            for i in range(10):
                w.add("blocks", slot(i))
            w.submit()
            time.sleep(0.3)
            self.assertGreater(w.backlog_bytes(), 0)                       # queued, not dropped
            gone.mkdir()
            self.assertTrue(w.worker.drain(10))
        self.assertEqual([p.name for p in gone.glob("*.h5")], ["spectra_20261105_0700.h5"])
        self.assertEqual(read(gone / "spectra_20261105_0700.h5", "blocks", "mean").shape[0], 10)
        w.close()

    def test_slow_disk_does_not_hold_up_the_caller(self):
        w = self.writer()
        real = w._append
        w._append = lambda *a: (time.sleep(0.4), real(*a))[1]
        for i in range(800):
            w.add("burst", frame(i))
        t = time.perf_counter(); w.submit(); spent = time.perf_counter() - t
        self.assertLess(spent, 0.1)
        w.close()
        self.assertEqual(read(list(self.dir.glob("spectra_*.h5"))[0], "burst", "spectrum").shape[0], 800)

    def test_order_of_rows_and_events_is_kept_and_closed_writer_writes_directly(self):
        ev = Events(self.dir / "events.csv"); w = self.writer(); ev.defer = w.defer
        ev("one"); w.add("burst", frame(0)); ev("two")
        w.close(); ev("three")
        rows = (self.dir / "events.csv").read_text().splitlines()[1:]
        self.assertEqual([r.split(",")[2] for r in rows], ["one", "two", "three"])

    def test_rows_of_an_hour_that_ended_are_stored_though_short(self):
        w = self.writer()
        for i in (0, 1):
            w.add("burst", frame(i))
        late = frame(2); late["t_wall_ns"] = np.int64(T0 + 3_700 * 10 ** 9)
        w.add("burst", late)
        w.submit(); w.worker.drain(30)
        self.assertEqual(read(self.dir / "spectra_20261105_0700.h5", "burst", "int_us").shape[0], 2)
        self.assertFalse((self.dir / "spectra_20261105_0800.h5").exists())
        w.close()
        self.assertEqual(read(self.dir / "spectra_20261105_0800.h5", "burst", "int_us").shape[0], 1)


class TestSleepingDrive(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def writer(self):
        return Writer(self.dir, np.linspace(200, 1100, 64), {}, 60, 30.0, block_rows=5)

    def test_no_wake_while_writes_are_recent(self):
        calls = []
        with mock.patch("lightkeeper.storage.os.fsync", side_effect=lambda fd: calls.append(fd)):
            w = self.writer()
            for i in range(10):
                w.add("blocks", slot(i)); w.submit(); w.worker.drain(10)
            w.close()
        self.assertEqual(calls, [])

    def test_a_quiet_spell_wakes_the_drive_first_and_leaves_nothing_behind(self):
        order = []
        w = self.writer(); real = w._append
        w._append = lambda *a: (order.append("write"), real(*a))[1]
        with mock.patch("lightkeeper.storage.WAKE_AFTER_S", -1.0), mock.patch("lightkeeper.storage.os.fsync", side_effect=lambda fd: order.append("wake")):
            for i in range(5):
                w.add("blocks", slot(i))
            w.submit(); w.worker.drain(10)
        self.assertEqual(order[:2], ["wake", "write"])
        w.close()
        self.assertEqual(list(self.dir.glob(".lightkeeper*")), [])

    def test_a_long_wake_is_logged_and_the_caller_does_not_wait(self):
        with mock.patch("lightkeeper.storage.WAKE_AFTER_S", -1.0), mock.patch("lightkeeper.storage.os.fsync", side_effect=lambda fd: time.sleep(1.2)):
            w = self.writer()
            for i in range(5):
                w.add("blocks", slot(i))
            t = time.perf_counter(); w.submit(); spent = time.perf_counter() - t
            self.assertLess(spent, 0.2)
            self.assertTrue(w.worker.drain(10))
        self.assertTrue(any("to wake" in n for n in w.notes), w.notes)
        self.assertEqual(read(list(self.dir.glob("spectra_*.h5"))[0], "blocks", "mean").shape[0], 5)
        w.close()

    def test_a_failed_wake_does_not_stop_the_write(self):
        with mock.patch("lightkeeper.storage.WAKE_AFTER_S", -1.0), mock.patch("lightkeeper.storage.os.fsync", side_effect=OSError("not ready")):
            w = self.writer()
            for i in range(5):
                w.add("blocks", slot(i))
            w.flush()
        self.assertEqual(read(list(self.dir.glob("spectra_*.h5"))[0], "blocks", "mean").shape[0], 5)
        w.close()


class TestPlanning(unittest.TestCase):
    def test_dark_count_is_no_longer_held_to_thirty(self):
        self.assertEqual(dark_scan_count(10_000, 5), 500)
        self.assertEqual(dark_scan_count(10_000, 30), 3000)
        self.assertEqual(dark_scan_count(3_000_000, 5), 5)              # long darks unchanged
        self.assertEqual(dark_scan_count(3_000_000, 30), 10)
        self.assertEqual(dark_scan_count(500_000, 5), 10)
        self.assertEqual(dark_scan_count(8000, 120), 12000)             # the bound

    def test_an_old_plan_keeps_the_old_gate_and_no_save_all(self):
        p = RunPlan.from_dict({k: v for k, v in RunPlan().to_dict().items() if k not in ("burst_min_counts", "save_all_frames")})
        self.assertEqual((p.burst_min_counts, p.save_all_frames), (600.0, False))

    def test_old_defaults_are_told_from_choices(self):
        old = dict(burst_every_min=60.0, burst_offset_min=30.0, burst_s=120.0, dark_s_per_int=5.0)
        self.assertTrue(old_acquisition_defaults("LightKeeper 0.3.11", old))
        self.assertFalse(old_acquisition_defaults("LightKeeper 0.3.13", old))
        self.assertFalse(old_acquisition_defaults("LightKeeper 0.3.11", dict(old, burst_s=60.0)))

    def test_recommended_schedule(self):
        t0 = dt.datetime(2026, 11, 5, 7, 0, tzinfo=LOCAL_TZ); t1 = dt.datetime(2026, 11, 5, 8, 0, tzinfo=LOCAL_TZ)
        b = burst_times(RunPlan(burst_every_min=15, burst_offset_min=7, burst_s=60), t0, t1)
        self.assertEqual([x.minute for x in b], [7, 22, 37, 52])

    def test_estimates_for_a_10_ms_day(self):
        now = dt.datetime.now(LOCAL_TZ)
        kw = dict(int_ms=10, min_int_ms=10, max_int_ms=10, long_dark_ms=3000, dark_s_per_int=30, burst_max_int_ms=50, start_now=False,
                  resolved_start_local=now.isoformat(), resolved_stop_local=(now + dt.timedelta(hours=12.6)).isoformat())
        e = estimates(RunPlan(burst_every_min=15, burst_offset_min=7, burst_s=60, **kw), 2068, 8000, 65_000_000)
        self.assertEqual(e["burst_int_ms"], 10.0)
        self.assertLess(e["data_gb_expected"], e["data_gb_uncompressed"])
        self.assertTrue(1.0 < e["data_gb_expected"] < 3.5, e["data_gb_expected"])
        a = estimates(RunPlan(save_all_frames=True, **kw), 2068, 8000, 65_000_000)
        self.assertEqual(a["bursts"], [])
        self.assertTrue(15 < a["data_gb_expected"] < 30, a["data_gb_expected"])


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


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.home = tempfile.TemporaryDirectory()
        self.env = os.environ.get("LIGHTKEEPER_HOME"); os.environ["LIGHTKEEPER_HOME"] = self.home.name
        self.eng = Engine(); self.eng.start(); self.ev = []
        self.eng.send("connect", SIM_ID, True, True)
        self.wait(lambda e: e["type"] == "state" and e["state"] == "IDLE", 10)

    def tearDown(self):
        self.eng.send("quit"); self.eng.join(15)
        os.environ.pop("LIGHTKEEPER_HOME", None) if self.env is None else os.environ.__setitem__("LIGHTKEEPER_HOME", self.env)
        self.td.cleanup(); self.home.cleanup()

    def wait(self, pred, timeout):
        return wait_for(self.eng, pred, timeout, self.ev)

    def plan(self, stop_s, burst_at_s=4.0, **kw):
        now = dt.datetime.now(LOCAL_TZ); b = now + dt.timedelta(seconds=burst_at_s)
        d = dict(label="raw", out_dir=self.td.name, start_now=True, stop_local=(now + dt.timedelta(seconds=stop_s)).isoformat(), int_ms=20,
                 long_dark_ms=0, pre_darks=False, post_darks=False, burst_every_min=60, burst_offset_min=b.minute + b.second / 60.0, burst_s=3,
                 burst_max_int_ms=50, burst_min_counts=-1e6, flush_s=2, auto_resume_logon=False)
        d.update(kw)
        return RunPlan(**d).to_dict()

    def tables(self, run):
        out = {}
        for f in sorted(Path(run).glob("spectra_*.h5")):
            with h5py.File(f, "r") as h:
                for t in ("blocks", "burst"):
                    if t in h:
                        self.assertEqual(len({h[t][k].shape[0] for k in h[t]}), 1, f"uneven columns in {f.name}/{t}")
                        for k in h[t]:
                            out.setdefault(t, {}).setdefault(k, []).append(h[t][k][:])
        return {t: {k: np.concatenate(v) for k, v in d.items()} for t, d in out.items()}


class TestInlineWindow(EngineCase):
    def test_slots_go_on_and_equal_the_saved_frames(self):
        self.eng.send("start", self.plan(11))
        closed = self.wait(lambda e: e["type"] == "run_closed", 40)
        d = self.tables(closed["dir"]); b, f = d["blocks"], d["burst"]
        self.assertTrue((np.diff(b["slot_index"]) == 1).all(), b["slot_index"])  # no gap in the 1 s record
        self.assertEqual(set(f["int_us"]), {20_000}); self.assertEqual(set(b["int_us"]), {20_000})
        n_checked = 0
        for i in range(len(b["t_wall_ns"])):                                     # each slot in the window is its own frames
            m = (f["t_wall_ns"] >= b["t_start_wall_ns"][i]) & (f["t_wall_ns"] <= b["t_end_wall_ns"][i])
            if m.any():
                self.assertEqual(int(m.sum()), int(b["n_scans"][i]))
                y = f["spectrum"][m].astype(np.float64)
                np.testing.assert_allclose(b["mean"][i], y.mean(0), rtol=1e-4, atol=1e-2)
                np.testing.assert_allclose(b["sd"][i], y.std(0, ddof=1), rtol=1e-3, atol=1e-2)
                n_checked += 1
        self.assertIn(n_checked, (3, 4))
        meta = json.loads((Path(closed["dir"]) / "run_metadata.json").read_text())
        self.assertEqual(meta["bursts_done"][0]["mode"], "inline")
        self.assertEqual(meta["burst_int_us"], 20_000); self.assertEqual(meta["burst_requested_int_us"], 50_000)
        ev = (Path(closed["dir"]) / "events.csv").read_text()
        self.assertIn("burst_start", ev); self.assertIn("burst_end", ev); self.assertNotIn("after burst", ev)
        s = json.loads((Path(closed["dir"]) / "run_summary.json").read_text())
        self.assertEqual(s["bursts"]["done"], 1); self.assertEqual(s["slots"]["recorded"], len(b["t_wall_ns"]))
        self.assertEqual(s["integration_changes"], [])

    def test_the_frame_stream_has_no_idle_time_at_slot_edges(self):
        self.eng.send("start", self.plan(10))
        closed = self.wait(lambda e: e["type"] == "run_closed", 40)
        d = self.tables(closed["dir"]); t = d["burst"]["t_wall_ns"]; b = d["blocks"]
        gaps = np.diff(t) / 1e6
        self.assertLess(np.percentile(gaps, 99), 45)                   # 20 ms frames: an idle wait at each slot edge would show
        self.assertTrue((b["t_end_wall_ns"] >= b["t_start_wall_ns"]).all())
        self.assertTrue((b["t_start_wall_ns"][1:] > b["t_end_wall_ns"][:-1]).all())   # a frame is in one slot only
        self.assertGreater(int(b["n_scans"][1:-1].min()), 40)          # full slots right through

    def test_no_exposure_command_during_the_window(self):
        calls = []
        real = self.eng.dev.set_int
        self.eng.send("start", self.plan(9))
        self.wait(lambda e: e["type"] == "state" and e["state"] == "RECORDING", 15)
        self.eng.dev.set_int = lambda us: (calls.append(us), real(us))[1]
        closed = self.wait(lambda e: e["type"] == "run_closed", 40)
        self.assertEqual(calls, [])
        self.assertGreater(len(self.tables(closed["dir"])["burst"]["int_us"]), 50)

    def test_the_planned_stop_ends_a_window(self):
        self.eng.send("start", self.plan(7, burst_at_s=4.0, burst_s=60))
        t = time.time()
        closed = self.wait(lambda e: e["type"] == "run_closed", 30)
        self.assertLess(time.time() - t, 14)
        meta = json.loads((Path(closed["dir"]) / "run_metadata.json").read_text())
        self.assertLess(meta["bursts_done"][0]["scans"], 300)

    def test_a_stop_request_ends_a_window_and_stores_every_frame(self):
        self.eng.send("start", self.plan(40, burst_at_s=3.0, burst_s=30))
        self.wait(lambda e: e["type"] == "state" and e["state"] == "BURST", 15)
        self.wait(lambda e: e["type"] == "slot", 5); self.wait(lambda e: e["type"] == "slot", 5)
        self.eng.send("stop")
        closed = self.wait(lambda e: e["type"] == "run_closed", 30)
        meta = json.loads((Path(closed["dir"]) / "run_metadata.json").read_text())
        d = self.tables(closed["dir"])
        self.assertEqual(meta["bursts_done"][0]["scans"], len(d["burst"]["int_us"]))
        self.assertEqual(meta["rows"]["burst"], len(d["burst"]["int_us"]))

    def test_interrupt_in_a_window_then_resume(self):
        self.eng.send("start", self.plan(22, burst_at_s=3.0, burst_s=30))
        run = Path(self.wait(lambda e: e["type"] == "run", 15)["dir"])
        self.wait(lambda e: e["type"] == "state" and e["state"] == "BURST", 15)
        self.wait(lambda e: e["type"] == "slot", 5)
        self.eng.send("interrupt")
        self.wait(lambda e: e["type"] == "run_interrupted", 15)
        n_burst = len(self.tables(run)["burst"]["int_us"]); self.assertGreater(n_burst, 0)
        self.eng.send("resume", str(run))
        self.wait(lambda e: e["type"] == "slot", 15); self.wait(lambda e: e["type"] == "slot", 5)
        self.assertEqual(self.eng.state, "RECORDING")                            # the window does not come back
        self.eng.send("stop")
        self.wait(lambda e: e["type"] == "run_closed", 30)
        d = self.tables(run)
        self.assertEqual(len(d["burst"]["int_us"]), n_burst)
        self.assertEqual(set(d["blocks"]["segment"]), {0, 1})

    def test_overlapping_bursts_merge_and_frames_are_saved_once(self):
        now = dt.datetime.now(LOCAL_TZ)
        p = self.plan(14, burst_s=4, burst_every_min=0.05, burst_offset_min=0)   # every 3 s, 4 s long
        self.eng.send("start", p)
        closed = self.wait(lambda e: e["type"] == "run_closed", 40)
        d = self.tables(closed["dir"])
        self.assertTrue((np.diff(d["burst"]["t_wall_ns"]) > 0).all())
        self.assertEqual(len(np.unique(d["burst"]["t_wall_ns"])), len(d["burst"]["t_wall_ns"]))
        self.assertIn("burst_merged", (Path(closed["dir"]) / "events.csv").read_text())
        self.assertLess((dt.datetime.now(LOCAL_TZ) - now).total_seconds(), 60)

    def test_save_every_frame(self):
        self.eng.send("start", self.plan(7, save_all_frames=True, burst_every_min=15))
        closed = self.wait(lambda e: e["type"] == "run_closed", 30)
        d = self.tables(closed["dir"])
        self.assertEqual(int(d["blocks"]["n_scans"].sum()), len(d["burst"]["int_us"]))   # every frame of every slot is there
        self.assertEqual(set(d["burst"]["burst_id"]), {0})
        self.assertNotIn("burst_start", (Path(closed["dir"]) / "events.csv").read_text())


class TestSlowDriveDuringARun(EngineCase):
    def test_a_quiet_standby_then_a_slow_wake_costs_no_frames(self):
        wakes = []

        def slow(fd):                    # the drive went to sleep during standby and takes 4 s to spin up
            wakes.append(time.monotonic()); time.sleep(4.0)
        now = dt.datetime.now(LOCAL_TZ)
        with mock.patch("lightkeeper.storage.WAKE_AFTER_S", 3.0), mock.patch("lightkeeper.storage.os.fsync", side_effect=slow):
            self.eng.send("start", self.plan(16, burst_at_s=9.0, burst_s=3, start_now=False, start_local=(now + dt.timedelta(seconds=5)).isoformat()))
            closed = self.wait(lambda e: e["type"] == "run_closed", 60)
        self.assertGreaterEqual(len(wakes), 1)
        d = self.tables(closed["dir"]); b = d["blocks"]
        self.assertTrue((np.diff(b["slot_index"]) == 1).all(), b["slot_index"])  # no slot lost while the file thread waited
        self.assertGreater(int(b["n_scans"][1:-1].min()), 40)
        self.assertGreaterEqual(len(b["n_scans"]), 9)
        self.assertLess(float(np.diff(d["burst"]["t_wall_ns"]).max()) / 1e6, 100)  # 20 ms frames
        self.assertIn("to wake", (Path(closed["dir"]) / "events.csv").read_text())


class TestOtherBurstPath(EngineCase):
    def test_a_burst_at_another_time_still_pauses_the_slots(self):
        self.eng.send("start", self.plan(11, int_ms=100, burst_max_int_ms=20))
        closed = self.wait(lambda e: e["type"] == "run_closed", 40)
        d = self.tables(closed["dir"])
        self.assertEqual(set(d["burst"]["int_us"]), {20_000}); self.assertEqual(set(d["blocks"]["int_us"]), {100_000})
        self.assertGreater(int(np.diff(d["blocks"]["slot_index"]).max()), 2)
        meta = json.loads((Path(closed["dir"]) / "run_metadata.json").read_text())
        self.assertNotIn("mode", meta["bursts_done"][0])

    def test_the_gate_uses_the_plan(self):
        self.eng.send("start", self.plan(8, burst_min_counts=1e9))
        closed = self.wait(lambda e: e["type"] == "run_closed", 30)
        self.assertIn("burst_skipped", (Path(closed["dir"]) / "events.csv").read_text())
        self.assertNotIn("burst", self.tables(closed["dir"]))


class TestDarks(EngineCase):
    def test_a_short_dark_gets_its_full_time(self):
        self.eng.send("start", self.plan(3, pre_darks=True, burst_every_min=0, int_ms=20, dark_s_per_int=2.0))
        self.wait(lambda e: e["type"] == "state" and e["state"] == "AWAIT_UNCOVER", 30)
        run = next(e["dir"] for e in self.ev if e["type"] == "run")
        n = 0
        for f in Path(run).glob("spectra_*.h5"):
            with h5py.File(f, "r") as h:
                if "dark_scans" in h:
                    n += h["dark_scans/spectrum"].shape[0]
        self.assertEqual(n, 100)
        self.eng.send("stop"); self.wait(lambda e: e["type"] == "run_closed", 20)


class TestWeather(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.run = Path(self.td.name) / "2026-11-04_x_v01"; self.run.mkdir()
        self.meta = dict(plan=dict(site_lat=38.89123, site_lon=-77.04123), planned_start_local="2026-11-04T05:16:39-05:00", recording_end_local="2026-11-04T18:45:07-05:00")
        (self.run / "run_metadata.json").write_text(json.dumps(self.meta))
        self.urls = []

    def tearDown(self):
        self.td.cleanup()

    def get(self, url):
        self.urls.append(url)
        if "points" in url:
            return json.dumps(dict(properties=dict(observationStations="https://api.weather.gov/stations-here"))).encode()
        if "stations-here" in url:
            return json.dumps(dict(features=[dict(properties=dict(stationIdentifier="KAAA", name="Example"), geometry=dict(coordinates=[-77.1, 38.9]))])).encode()
        if "asos" in url:
            return b"station,valid,tmpf,wxcodes\nAAA,2026-11-04 14:15,68.00,RA\nAAA,2026-11-04 15:15,70.00,\n"
        return json.dumps(dict(hourly=dict(time=["t"]))).encode()

    def test_writes_a_file_with_only_the_rounded_position(self):
        p = weather.fetch_for_run(self.run, get=self.get)
        self.assertEqual(p.name, "weather_v01.json")
        d = json.loads(p.read_text())
        self.assertTrue(all(s["ok"] for s in d["sources"].values()), d["sources"])
        self.assertEqual(d["position_sent"], dict(lat=38.89, lon=-77.04))
        self.assertEqual(d["sources"]["airport_observations"]["data"][1], dict(station="AAA", valid="2026-11-04 15:15", tmpf="70.00"))
        for u in self.urls:
            self.assertNotIn("38.891", u); self.assertNotIn("77.041", u)
        self.assertTrue(any("station=AAA" in u and "year1=2026" in u and "hour1=10" in u for u in self.urls), self.urls)   # 05:16 EST is 10:16 UTC

    def test_a_new_fetch_does_not_overwrite(self):
        self.assertEqual(weather.fetch_for_run(self.run, get=self.get).name, "weather_v01.json")
        self.assertEqual(weather.fetch_for_run(self.run, get=self.get).name, "weather_v02.json")

    def test_one_source_down_still_writes_the_others(self):
        def get(url):
            if "air-quality" in url or "weather.gov" in url:
                raise OSError("down")
            return self.get(url)
        d = json.loads(weather.fetch_for_run(self.run, get=get).read_text())
        self.assertTrue(d["sources"]["open_meteo"]["ok"]); self.assertFalse(d["sources"]["open_meteo_air_quality"]["ok"])
        self.assertNotIn("airport_observations", d["sources"])

    def test_a_busy_service_is_asked_again(self):
        import urllib.error
        tries = []

        def get(url):
            if "asos" in url and len(tries) < 2:
                tries.append(url)
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
            return self.get(url)
        with mock.patch.object(weather, "BUSY_WAIT_S", 0.01):
            d = json.loads(weather.fetch_for_run(self.run, get=get).read_text())
        self.assertTrue(d["sources"]["airport_observations"]["ok"]); self.assertEqual(len(tries), 2)

    def test_nothing_answering_writes_nothing(self):
        def get(url):
            raise OSError("no network")
        with self.assertRaises(RuntimeError):
            weather.fetch_for_run(self.run, get=get)
        self.assertEqual(list(self.run.glob("weather*")), [])

    def test_a_run_without_a_site_is_refused_without_sending_anything(self):
        (self.run / "run_metadata.json").write_text(json.dumps(dict(self.meta, plan=dict(site_lat=0.0, site_lon=0.0))))
        with self.assertRaises(ValueError):
            weather.fetch_for_run(self.run, get=self.get)
        self.assertEqual(self.urls, [])

    def test_missing_runs(self):
        (self.run / "run_state.json").write_text(json.dumps(dict(status="closed")))
        self.assertEqual(weather.missing_runs(self.td.name), [self.run])
        weather.fetch_for_run(self.run, get=self.get)
        self.assertEqual(weather.missing_runs(self.td.name), [])
        (self.run / "run_state.json").write_text(json.dumps(dict(status="interrupted")))
        (self.run / "weather_v01.json").unlink()
        self.assertEqual(weather.missing_runs(self.td.name), [])

    def test_the_engine_and_recorder_never_use_it(self):
        src = Path(__file__).resolve().parents[1] / "lightkeeper"
        for name in ("engine.py", "recorder.py", "storage.py", "supervisor.py"):
            self.assertNotIn("weather", (src / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
