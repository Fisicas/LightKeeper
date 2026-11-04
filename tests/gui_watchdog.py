"""GUI check (0.3.11): the window and the watchdog do not fight over the spectrometer. Off-screen, simulator only, temporary
LIGHTKEEPER_HOME. Run:  set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\gui_watchdog.py"""
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="lk_dog_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = str(HOME)
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"          # never open a real spectrometer that happens to be plugged in
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import procs, ui  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402

app = Q.QApplication(sys.argv); ui.theme.apply(app)
results = []


def check(name, cond):
    results.append(bool(cond)); print(("[PASS] " if cond else "[FAIL] ") + name)


def pump(s, until=None):
    t0 = time.time()
    while time.time() - t0 < s:
        app.processEvents(); time.sleep(0.03)
        if until and until():
            return


def fake_run():
    d = HOME / "runs" / "dead_run"; d.mkdir(parents=True)
    stop = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
    procs.write_json_atomic(d / "run_state.json", dict(status="open", serial="SIM0001", planned_stop_local=stop.isoformat()))
    procs.register_open_run(d, "SIM0001")
    return d


w = ui.Window(auto_confirm=True); w.show(); pump(0.5)
w.engine.send("connect", SIM_ID, True, True); pump(5, lambda: w.state == "IDLE"); pump(1.5)
check("a window with the spectrometer tells the watchdog", procs.window_holds_device())
w.engine.send("disconnect"); pump(5, lambda: w.engine.state == "DISCONNECTED"); pump(1.5)
check("and stops saying so when it lets go", not procs.window_holds_device())

d = fake_run()
seen = []
real_list = ui.list_devices
ui.list_devices = lambda *a: (seen.append(1), real_list(*a))[1]
w._auto = True; w.link = None; w._expect_attach = None; w._set_state("DISCONNECTED")
w._auto_connect(rescan=True)
check("a window waiting on a dead run leaves the spectrometer free for the resume", not seen)

w._ask = lambda *a: False
w._offer_resume()
check("'Not now' switches the watchdog off for that run", (d / "watchdog_off.json").exists() and not procs.watched_runs())
w._auto_connect(rescan=True)
check("and the window may then use the spectrometer", bool(seen))
procs.watchdog_clear(d)
check("a later resume switches it back on", bool(procs.watched_runs()))
w._auto = False
procs.unregister_open_run(d)
w.engine.send("quit"); pump(1)
print(f"\n{sum(results)}/{len(results)} checks passed; temp {HOME}")
sys.exit(0 if all(results) else 1)
