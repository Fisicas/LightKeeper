"""0.4.0: a run's setup loads from the Load setup menu or a run_metadata.json dropped anywhere on the window (a line edit and a
plot included), after a question; the Time series tab shows only the series that are ticked; with several spectrometers the
window asks which one to open. Runs offscreen (LIGHTKEEPER_SPLASH_VISIBLE=1: real display). No spectrometer; temporary settings."""
import json
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("LIGHTKEEPER_SPLASH_VISIBLE"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME = tempfile.mkdtemp(prefix="lk_setupimp_")
os.environ.update(LIGHTKEEPER_HOME=HOME, LIGHTKEEPER_AUTOCONNECT="0", LIGHTKEEPER_DATA_ROOT=str(Path(HOME) / "runs"),
                  LIGHTKEEPER_NO_INSTALL="1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtCore, QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402

app = Q.QApplication(sys.argv[:1]); ui.theme.apply(app)
ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


# an earlier run somewhere else on disk, as LightKeeper 0.3.15 wrote it (USB650, 50 ms fixed, sky-facing diffuser)
plan = dict(label="Blue sky, R800-8-UV-VIS-SR, 50 ms fixed int day 01", notes="", start_now=False,
            start_event="nautical dawn (-12°)", start_offset_min=0.0, stop_event="nautical dusk (-12°)", stop_offset_min=0.0,
            slot_s=1.0, auto_exposure=False, int_ms=50.0, min_int_ms=500.0, max_int_ms=3000.0, edc=True, nlc=True, store_sd=True,
            burst_every_min=15.0, burst_offset_min=7.0, burst_s=60.0, burst_max_int_ms=50.0, burst_min_counts=0.0,
            save_all_frames=False, long_dark_ms=3000.0, backoff_step_ms=10.0, backoff_min_ms=10.0, dark_s_per_int=30.0,
            pre_darks=True, post_darks=True, site_lat=33.02806, site_lon=-84.98532, geometry="polystyrene diffuser in bag facing sky",
            fiber_core_um=800.0, diffuser_id="polystyrene packing foam", diffuser_thickness_mm=2.0, view_az_deg=190.0, view_el_deg=85.0,
            window_state="open", site_alt_m=230.0)
run = Path(HOME) / "elsewhere" / "2026-11-15_Blue_sky_v01"; run.mkdir(parents=True)
(run / "run_metadata.json").write_text(json.dumps(dict(app="LightKeeper 0.3.15+usb650", plan=plan, planned_start_local="2026-11-15T06:14:31-05:00",
                                                       device=dict(model="USB650", serial="USB2G52537"))), encoding="utf-8")

w = ui.Window(auto_confirm=True); w.resize(1500, 850); w.show(); app.processEvents()

# 1. read: folder, metadata file, a plan file, and something that is not a run
m, name = w._read_setup(run)
check("a run folder reads", m is not None and m["plan"]["int_ms"] == 50.0 and name == run.name, name)
m2, _ = w._read_setup(run / "run_metadata.json")
check("its run_metadata.json reads", m2 is not None and m2["plan"]["label"] == plan["label"])
pf = Path(HOME) / "plan_x.json"; pf.write_text(json.dumps(plan)); m3, _ = w._read_setup(pf)
check("a bare plan .json reads", m3 is not None and m3["plan"]["geometry"] == plan["geometry"])
bad = Path(HOME) / "other.json"; bad.write_text("{\"a\": 1}")
check("a .json that is not a run is refused", w._read_setup(bad)[0] is None)

# 2. drop the file on a line edit (which would otherwise take the text) and see the setup replaced
w.label_edit.setText("something else"); w.run_int.setValue(500); w.geom_edit.setText("")
md = QtCore.QMimeData(); md.setUrls([QtCore.QUrl.fromLocalFile(str(run / "run_metadata.json"))])
pos = QtCore.QPointF(5, 5)
enter = ui.QtGui.QDragEnterEvent(QtCore.QPoint(5, 5), QtCore.Qt.DropAction.CopyAction, md, QtCore.Qt.MouseButton.LeftButton,
                                 QtCore.Qt.KeyboardModifier.NoModifier)
app.sendEvent(w.geom_edit, enter)
check("dragging a run's metadata over a line edit is accepted", enter.isAccepted())
drop = ui.QtGui.QDropEvent(pos, QtCore.Qt.DropAction.CopyAction, md, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
app.sendEvent(w.geom_edit, drop)
for _ in range(20):
    app.processEvents()
check("drop replaced the exposure", w.run_int.value() == 50.0, w.run_int.value())
check("drop replaced the geometry (not the dropped path)", w.geom_edit.text() == plan["geometry"], w.geom_edit.text())
check("label counts on from the earlier run", w.label_edit.text().startswith("Blue sky"), w.label_edit.text())
check("the panel says where the setup came from", w.setup_src.isVisible() and run.name in w.setup_src.text(), w.setup_src.text())
check("start stays a sun-event rule", w.start_mode.currentIndex() == 2 and "nautical dawn" in w.start_event.currentText())

# drop on a plot too
w.run_int.setValue(500)
drop2 = ui.QtGui.QDropEvent(pos, QtCore.Qt.DropAction.CopyAction, md, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
w.tabs.setCurrentIndex([w.tabs.tabText(i) for i in range(w.tabs.count())].index("Time series")); app.processEvents()
enter2 = ui.QtGui.QDragEnterEvent(QtCore.QPoint(5, 5), QtCore.Qt.DropAction.CopyAction, md, QtCore.Qt.MouseButton.LeftButton,
                                  QtCore.Qt.KeyboardModifier.NoModifier)
app.sendEvent(w.c_ts, enter2)               # a real drag always enters before it drops
check("dragging over a plot is accepted", enter2.isAccepted())
app.sendEvent(w.c_ts, drop2)
for _ in range(20):
    app.processEvents()
check("drop on a plot works too", w.run_int.value() == 50.0)

# the menu remembers it
w._fill_setup_menu()
texts = [a.text() for a in w.setup_menu.actions()]
check("Load setup menu lists the run loaded before and Browse", any(run.name in t for t in texts) and any("Browse" in t for t in texts), texts)

# refused while a run is open
w.state = "RECORDING"; w.auto_confirm = True
qmb = Q.QMessageBox.information; seen = []
Q.QMessageBox.information = lambda *a, **k: seen.append(a)
check("no import while a run is open", w._import_setup(run) is False and seen)
Q.QMessageBox.information = qmb; w.state = "DISCONNECTED"

# 3. time series: tick boxes choose the panels
check("all five series shown by default", len(w.c_ts.ax) == 5)
for k in ("rfr", "nirred", "o2a"):
    w.ts_cbs[k].setChecked(False)
app.processEvents()
check("unticking three leaves two panels", len(w.c_ts.ax) == 2 and set(w.ts_ax) == {"par", "int"}, list(w.ts_ax))
w.ts_cbs["par"].setChecked(False); w.ts_cbs["int"].setChecked(False); app.processEvents()
check("the last one cannot be unticked", len(w.c_ts.ax) == 1 and w.ts_cbs["int"].isChecked(), list(w.ts_ax))
check("the choice is remembered", list(w.settings.value("ts_shown")) == ["int"], w.settings.value("ts_shown"))
import datetime as dt  # noqa: E402
now = dt.datetime.now(ui.LOCAL_TZ)
w.slots = [dict(t=now + dt.timedelta(seconds=i), par=100.0 + i, rfr=1.0, nirred=2.0, o2a=0.1, int_ms=50.0, n=20, nsat=0) for i in range(200)]
for k in ui.Window.TS_SERIES:
    w.ts_cbs[k[0]].setChecked(True)
app.processEvents()
try:
    w._draw_timeseries(); w.ts_cbs["o2a"].setChecked(False); app.processEvents(); w._draw_timeseries(); drew = True
except Exception as e:
    drew = repr(e)
check("drawing with a subset of panels works", drew is True and len(w.c_ts.ax) == 4, drew)

# 4. several spectrometers: the window asks (auto_confirm picks the first; the last used one is offered first)
w.dev_combo.clear()
for i, lab in [("od:MAYP111000", "Maya2000 Pro  [MAYP111000]  (OceanDirect)"), ("sb:USB2G52537", "USB650  [USB2G52537]  (seabreeze)"),
               (ui.SIM_ID, "Simulator")]:
    w.dev_combo.addItem(lab, i)
sent = []; w.engine.send = lambda *a: sent.append(a)
w._auto = True; w._auto_failed = None; w.state = "DISCONNECTED"; w.link = None; w._expect_attach = None
w._auto_choice = 1; w._auto_connect()
check("with two attached, the chosen one opens", sent and sent[-1][:2] == ("connect", "sb:USB2G52537"), sent)
w._auto_choice = None
orig = Q.QInputDialog.getItem; Q.QInputDialog.getItem = staticmethod(lambda *a, **k: ("", False))
sent.clear(); w._auto_failed = None; w._auto_connect()
check("cancel opens nothing and is not asked again", not sent and w._auto_failed == ("od:MAYP111000", "sb:USB2G52537"))
Q.QInputDialog.getItem = orig

w.close(); app.processEvents()
print("PASS" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
