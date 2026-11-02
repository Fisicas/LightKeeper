"""Regression test (0.1.1): while a run is armed/recording, the Schedule tab must show the times fixed at Start, not a
sun-event plan re-resolved from 'now' (which after today's dawn gives tomorrow's events). Off-screen, no hardware.
Run:  set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\test_schedule_display_running.py [screenshot folder]"""
import datetime as dt
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402
from lightkeeper.schedule import RunPlan  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402

app = Q.QApplication(sys.argv); ui.theme.apply(app)
w = ui.Window(auto_confirm=True); w.resize(1700, 1000); w.show(); app.processEvents()
now = dt.datetime.now(LOCAL_TZ)
plan = RunPlan(label="regression_running", start_now=False, start_event="astronomical dawn (-18°)", start_offset_min=-20,
               stop_event="astronomical dusk (-18°)", stop_offset_min=20)
# a run armed 10 h ago and still recording: start 3 h ago, stop 4 h from now (same calendar day as 'now' is not required)
t0, t1 = now - dt.timedelta(hours=3), now + dt.timedelta(hours=4)
w.engine_events = None
w._set_state("RECORDING")
w.run_plan = plan.to_dict(); w.run_times = (t0, t1); w._update_schedule(); app.processEvents()
txt = w.sch_text.toPlainText(); lbl = w.when_lbl.text()
ok = True
for need in (t0.strftime("%a %d %b, %H:%M:%S"), t1.strftime("%a %d %b, %H:%M:%S"), "Run in progress"):
    if need not in txt:
        print("FAIL: missing", need); ok = False
if "Running" not in lbl or t1.strftime("%H:%M:%S") not in lbl:
    print("FAIL: when_lbl", lbl); ok = False
xl = w.c_sch.ax[0].get_xlim()
import matplotlib.dates as mdates  # noqa: E402
lo, hi = mdates.num2date(xl[0]), mdates.num2date(xl[1])
if not (lo <= now <= hi and lo <= t0 and hi >= t1):
    print("FAIL: x range", lo, hi); ok = False
if len(sys.argv) > 1:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    w.tabs.setCurrentIndex([w.tabs.tabText(i) for i in range(w.tabs.count())].index("Schedule")); app.processEvents()
    w.grab().save(str(out / "schedule_running_fixed_v01.png"))
# form-based preview must be unaffected once the run is closed
w.run_plan = None; w.run_times = None; w._set_state("IDLE"); w._update_schedule(); app.processEvents()
if "Run in progress" in w.sch_text.toPlainText() or w.when_lbl.text().startswith("Running"):
    print("FAIL: still in running mode after close"); ok = False
print("schedule-display regression:", "OK" if ok else "FAILED")
print(txt.splitlines()[:4], lbl, sep="\n")
w.close(); app.quit()
sys.exit(0 if ok else 1)
