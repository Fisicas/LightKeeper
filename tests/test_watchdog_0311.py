"""0.3.11: the watchdog resumes a run whose supervisor and recorder died together, and leaves every other run alone.

The rules are checked directly. The end-to-end test uses a REAL scheduled task, named for its temporary home, and the
simulator: it kills the whole process tree mid-recording and waits for the task to bring the run back as a new segment,
then lets the run close and checks that the task removed itself and that the recorder log has content.
Run:  python -X utf8 -m unittest tests.test_watchdog_0311 -v
"""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_recorder_resume import Client  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.td.name) / "home"
        os.environ["LIGHTKEEPER_HOME"] = str(self.home)
        os.environ.pop("LIGHTKEEPER_WATCHDOG", None)
        import importlib
        import lightkeeper.procs as procs
        import lightkeeper.watchdog as dog
        self.procs = importlib.reload(procs)
        self.dog = importlib.reload(dog)
        self.out = Path(self.td.name) / "runs"; self.out.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def fake_run(self, name="r1", status="open", stop_h=1.0, serial="SIM0001"):
        d = self.out / name; d.mkdir()
        stop = dt.datetime.now().astimezone() + dt.timedelta(hours=stop_h)
        self.procs.write_json_atomic(d / "run_state.json", dict(status=status, serial=serial, planned_stop_local=stop.isoformat(), last_slot_local="x"))
        self.procs.register_open_run(d, serial)
        return d


class Rules(Base):
    def setUp(self):
        super().setUp()
        self.spawned = []
        self.dog.spawn_detached = lambda args, log=None: self.spawned.append(args)
        self.dog.set_watchdog_task = lambda on: f"task {on}"

    def test_dead_open_run_is_resumed(self):
        d = self.fake_run()
        self.assertEqual(len(self.dog.run_once()), 1)
        self.assertEqual(self.spawned[0][-3:], ["--supervisor", "--resume", str(d)])

    def test_nothing_open_removes_the_task(self):
        self.assertEqual(self.dog.run_once(), ["task False"])
        self.assertEqual(self.spawned, [])

    def test_interrupted_run_is_left_alone(self):
        self.fake_run(status="interrupted")
        self.assertEqual(self.dog.run_once(), ["task False"])
        self.assertEqual(self.spawned, [])

    def test_switched_off_run_is_left_alone_until_cleared(self):
        d = self.fake_run()
        self.procs.watchdog_off(d, "supervisor gave up")
        self.assertEqual(self.dog.run_once(), ["task False"])
        self.assertEqual(self.spawned, [])
        self.procs.watchdog_clear(d)
        self.dog.run_once()
        self.assertEqual(len(self.spawned), 1)

    def test_grace_after_the_stop(self):
        self.fake_run("recent", stop_h=-0.5)                     # still resumed: it needs its post-run darks
        self.fake_run("old", stop_h=-1.5)                        # long past: only by hand
        self.dog.run_once()
        self.assertEqual([Path(a[-1]).name for a in self.spawned], ["recent"])

    def test_live_supervisor_or_recorder_is_left_alone(self):
        d = self.fake_run()
        self.procs.write_json_atomic(d / "supervisor.json", dict(pid=os.getpid()))
        self.assertEqual(self.dog.run_once(), [])
        (d / "supervisor.json").unlink()
        self.procs.write_json_atomic(self.procs.session_path("SIM0001"), dict(pid=os.getpid()))
        self.assertEqual(self.dog.run_once(), [])
        self.assertEqual(self.spawned, [])

    def test_stale_supervisor_and_session_do_not_block(self):
        d = self.fake_run()
        self.procs.write_json_atomic(d / "supervisor.json", dict(pid=999999))
        self.procs.write_json_atomic(self.procs.session_path("SIM0001"), dict(pid=999999))
        self.dog.run_once()
        self.assertEqual(len(self.spawned), 1)

    def test_window_with_the_device_defers(self):
        self.fake_run()
        self.procs.set_window_holds(True)
        self.assertEqual(self.dog.run_once(), [])
        self.assertEqual(self.spawned, [])
        self.procs.set_window_holds(False)
        self.dog.run_once()
        self.assertEqual(len(self.spawned), 1)

    def test_dead_window_does_not_defer(self):
        self.fake_run()
        self.procs.write_json_atomic(self.home / "window_device_999999.json", dict(pid=999999))
        self.dog.run_once()
        self.assertEqual(len(self.spawned), 1)

    def test_one_resume_per_spectrometer_newest_first(self):
        self.fake_run("a_old"); self.fake_run("b_new")
        self.dog.run_once()
        self.assertEqual([Path(a[-1]).name for a in self.spawned], ["b_new"])

    def test_task_is_skipped_for_a_test_home_unless_asked(self):
        self.assertIn("skipped", self.procs.set_watchdog_task(True))

    def test_second_supervisor_lock_is_refused_while_the_first_lives(self):
        code = ("import sys; sys.path.insert(0, r'%s'); from lightkeeper import procs; "
                "print(procs.supervisor_lock('SIM0001'), flush=True); sys.stdin.read()" % ROOT)
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        a = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], **kw)
        self.assertEqual(a.stdout.readline().strip(), "True")
        b = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], **kw)
        self.assertEqual(b.stdout.readline().strip(), "False")
        b.communicate(""); a.kill(); a.wait()                    # however the first one dies, the lock goes with it
        c = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], **kw)
        self.assertEqual(c.stdout.readline().strip(), "True")
        c.communicate("")


class LogonEntry(Base):
    def test_supervisor_removes_the_entry_of_a_closed_run(self):
        import winreg
        d = self.fake_run("logon_entry_selfclean", status="closed")
        name = self.procs._run_value(d)
        self.assertIn("logon resume set", self.procs.set_logon_resume(d, True))
        subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "LightKeeper.py"), "--supervisor", "--resume", str(d)], timeout=30)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.procs.RUN_KEY) as k:
            with self.assertRaises(FileNotFoundError):
                winreg.QueryValueEx(k, name)


class EndToEnd(Base):
    def test_real_task_brings_back_a_killed_process_tree(self):
        from lightkeeper.schedule import RunPlan
        from lightkeeper.storage import LOCAL_TZ
        os.environ["LIGHTKEEPER_WATCHDOG"] = "1"
        name = self.procs.watchdog_task_name()
        self.assertNotEqual(name, "LightKeeper Watchdog")        # never the real task
        task_exists = lambda: subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True).returncode == 0
        c = None
        try:
            now = dt.datetime.now(LOCAL_TZ)
            plan = RunPlan(label="dog", out_dir=str(self.out), start_now=True, stop_local=(now + dt.timedelta(seconds=170)).isoformat(),
                           int_ms=100, dark_s_per_int=0.2, flush_s=2, burst_every_min=0, long_dark_ms=0, auto_resume_logon=False)
            pf = self.procs.app_home() / "pending" / "plan_dog.json"
            self.procs.write_json_atomic(pf, plan.to_dict())
            sup = self.procs.spawn_detached(self.procs.entry_args("--supervisor", "--plan", str(pf), "--serial", "SIM0001"))
            c = Client(self.procs, "SIM0001")
            c.wait(lambda e: e["type"] == "state" and e.get("state") == "AWAIT_UNCOVER", 30)
            c.link.send("continue")
            c.wait(lambda e: e["type"] == "slot", 30)
            self.assertTrue(task_exists(), "the recorder should have set the task")
            run_dir = Path(self.procs.live_sessions()[0]["run_dir"])
            pid0 = self.procs.live_sessions()[0]["pid"]
            c.close(); c = None

            subprocess.run(["taskkill", "/F", "/T", "/PID", str(sup.pid)], capture_output=True)
            t_kill = time.time()
            time.sleep(1)
            self.assertFalse(self.procs.pid_alive(pid0), "the kill should have taken the recorder with it")

            c = Client(self.procs, "SIM0001", timeout=100)           # only the scheduled task can bring it back
            print(f"\n  back {time.time() - t_kill:.0f} s after the whole tree was killed")
            self.assertNotEqual(self.procs.live_sessions()[0]["pid"], pid0)
            self.assertEqual(self.procs.read_json(run_dir / "run_state.json")["status"], "open")
            c.wait(lambda e: e["type"] == "slot", 30)
            self.assertEqual(self.procs.read_json(run_dir / "run_state.json")["segment"], 1)
            self.assertTrue((self.home / "logs").glob("watchdog_*.log"))

            c.wait(lambda e: e["type"] == "state" and e.get("state") == "POST_RUN", 180)
            c.link.send("post_dark")
            c.wait(lambda e: e["type"] == "run_closed", 60)
            c.close(); c = None
            t0 = time.time()
            while task_exists() and time.time() - t0 < 15:
                time.sleep(0.5)
            self.assertFalse(task_exists(), "closing the run should remove the task")
            self.assertEqual(self.procs.read_json(run_dir / "run_state.json")["status"], "closed")
            logs = sorted((self.home / "logs").glob("recorder_*.log"))
            self.assertTrue(any("watchdog task set" in f.read_text(encoding="utf-8") for f in logs), "recorder logs must have content")
        finally:
            if c:
                c.close()
            for s in self.procs.live_sessions():
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(s["pid"])], capture_output=True)
            subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
