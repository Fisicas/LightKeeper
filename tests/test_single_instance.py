"""Process-level launch regressions. No SDK, real runs, or user settings are used.

python -X utf8 tests/test_single_instance.py
Set LIGHTKEEPER_INSTANCE_VISIBLE=1 to exercise native Windows focus as well.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child():
    import types
    import runpy
    from PySide6 import QtCore, QtWidgets as Q
    from lightkeeper import instance, splash

    home = Path(os.environ["LIGHTKEEPER_HOME"])
    events = home / f"{os.environ['LK_TEST_ID']}.jsonl"

    def log(event, **kw):
        with events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(event=event, **kw)) + "\n")

    class Window(Q.QWidget):
        def __init__(self, auto_confirm=False, progress=None):
            super().__init__()
            self.setWindowTitle("LightKeeper isolated launch regression")
            if progress:
                for step in (1, 2, 3):
                    progress(step)
            log("window")
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(20)

        def tick(self):
            if (home / "crash").exists():
                os._exit(77)  # crash the actual interpreter, including with Windows' venv redirector
            if (home / "stop").exists():
                self.close()
                Q.QApplication.instance().quit()
            if (home / "minimize").exists() and not hasattr(self, "minimized_once"):
                self.minimized_once = True
                self.showMinimized()
                log("minimized")
            if (home / "modal").exists() and not hasattr(self, "dialog"):
                self.dialog = Q.QDialog(self)
                self.dialog.setWindowTitle("Isolated recorder question")
                self.dialog.setModal(True)
                self.dialog.show()
                log("modal")

        def startup_finished(self):
            log("ready")

    def load(_):
        log("load")
        # Allow a simultaneous second launch while the first splash is visible.
        until = time.monotonic() + float(os.environ.get("LK_TEST_LOAD_DELAY", "0"))
        while time.monotonic() < until:
            Q.QApplication.processEvents()
            time.sleep(0.01)
        sys.modules["lightkeeper.ui"] = types.SimpleNamespace(Window=Window)
        sys.modules["lightkeeper.theme"] = types.SimpleNamespace(apply=lambda app: None)

    activate = instance.WindowInstance._activate

    def activated(self):
        activate(self)
        if self.window is not None:
            modal = Q.QApplication.activeModalWidget()
            native = None
            if os.environ.get("LIGHTKEEPER_INSTANCE_VISIBLE") and sys.platform == "win32":
                import ctypes
                u = ctypes.WinDLL("user32")
                u.GetForegroundWindow.restype = ctypes.c_void_p
                native = u.GetForegroundWindow() == int((modal or self.window).winId())
            log("activated", minimized=self.window.isMinimized(), modal=modal is not None, foreground=native)

    splash._load_libraries = load
    instance.WindowInstance._activate = activated
    Q.QMessageBox.critical = lambda *args: log("error", text=str(args[-1]))
    code = 1
    try:
        sys.argv = [str(ROOT / "LightKeeper.py"), *sys.argv[2:]]
        runpy.run_path(str(ROOT / "LightKeeper.py"), run_name="__main__")
    except SystemExit as e:
        code = e.code
    finally:
        log("exit", code=code, heavy_imports=any(k in sys.modules for k in ("matplotlib", "h5py", "lightkeeper.engine")))
    raise SystemExit(code)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lk_instance_")
        self.home = Path(self.tmp.name)
        self.procs = []
        self.logs = []
        self.env = dict(os.environ, LIGHTKEEPER_HOME=str(self.home), LIGHTKEEPER_AUTOCONNECT="0")
        if not os.environ.get("LIGHTKEEPER_INSTANCE_VISIBLE"):
            self.env["QT_QPA_PLATFORM"] = "offscreen"

    def tearDown(self):
        (self.home / "stop").touch()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        for f in self.logs:
            f.close()
        self.tmp.cleanup()

    def spawn(self, *args, **env):
        output = (self.home / f"output_{len(self.procs)}.txt").open("w", encoding="utf-8")
        self.logs.append(output)
        test_id = str(len(self.procs))
        p = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--child", *args],
                             env=dict(self.env, LK_TEST_ID=test_id, **env), cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        p.test_id = test_id
        self.procs.append(p)
        return p

    def events(self, p):
        path = self.home / f"{p.test_id}.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # writer may be in the middle of its last line
        return rows

    def wait_event(self, p, name, count=1):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            rows = [r for r in self.events(p) if r["event"] == name]
            if len(rows) >= count:
                return rows[-1]
            if p.poll() is not None:
                break
            time.sleep(0.02)
        output = (self.home / f"output_{self.procs.index(p)}.txt").read_text(encoding="utf-8")
        self.fail(f"No {name} event from {p.pid}: {self.events(p)}\n{output}")

    def duplicate(self, *args):
        p = self.spawn(*args)
        self.assertEqual(p.wait(timeout=10), 0)
        rows = self.events(p)
        self.assertEqual([r["event"] for r in rows], ["exit"], rows)
        self.assertFalse(rows[0]["heavy_imports"])
        return p

    def test_repeat_launch_restores_minimized_and_respects_modal(self):
        owner = self.spawn()
        self.wait_event(owner, "ready")
        (self.home / "minimize").touch()
        self.wait_event(owner, "minimized")
        self.duplicate("--no-splash")
        r = self.wait_event(owner, "activated")
        self.assertFalse(r["minimized"])
        if os.environ.get("LIGHTKEEPER_INSTANCE_VISIBLE"):
            self.assertTrue(r["foreground"])
        (self.home / "modal").touch()
        self.wait_event(owner, "modal")
        self.duplicate()
        r = self.wait_event(owner, "activated", count=2)
        self.assertTrue(r["modal"])
        if os.environ.get("LIGHTKEEPER_INSTANCE_VISIBLE"):
            self.assertTrue(r["foreground"])

    def test_repeat_during_splash_is_deferred(self):
        owner = self.spawn(LK_TEST_LOAD_DELAY="2")
        self.wait_event(owner, "load")
        self.duplicate()
        self.wait_event(owner, "ready")
        self.wait_event(owner, "activated")
        self.assertEqual(sum(r["event"] == "window" for r in self.events(owner)), 1)

    def test_simultaneous_launches_elect_exactly_one_ui(self):
        procs = [self.spawn(LK_TEST_LOAD_DELAY="2") for _ in range(6)]
        until = time.monotonic() + 15
        while time.monotonic() < until:
            windows = [p for p in procs if any(r["event"] == "ready" for r in self.events(p))]
            if windows and sum(p.poll() is not None for p in procs) == 5:
                break
            time.sleep(0.02)
        self.assertEqual(len(windows), 1)
        self.assertEqual(sum(p.poll() is None for p in procs), 1)
        self.assertEqual(sum(r["event"] == "window" for p in procs for r in self.events(p)), 1)

    def test_lock_recovers_after_crash_and_normal_exit(self):
        first = self.spawn("--no-splash")
        self.wait_event(first, "ready")
        (self.home / "crash").touch()
        self.assertEqual(first.wait(timeout=5), 77)
        (self.home / "crash").unlink()
        second = self.spawn("--no-splash")
        self.wait_event(second, "ready")
        (self.home / "stop").touch()
        self.assertEqual(second.wait(timeout=5), 0)
        (self.home / "stop").unlink()
        third = self.spawn()
        self.wait_event(third, "ready")


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        unittest.main(verbosity=2)
