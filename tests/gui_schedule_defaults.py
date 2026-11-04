"""GUI check (0.3.12): the Start dialog's schedule defaults to nautical dawn to nautical dusk with no offsets, and those
resolve to the expected times at a stand-in site. Off-screen, no hardware, temporary LIGHTKEEPER_HOME.
Run:  set QT_QPA_PLATFORM=offscreen & python -X utf8 tests/gui_schedule_defaults.py"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = tempfile.mkdtemp(prefix="lk_defaults_")
os.environ["LIGHTKEEPER_DATA_ROOT"] = tempfile.mkdtemp(prefix="lk_defaults_runs_")   # no previous run to copy
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402
from lightkeeper.schedule import next_sun_event  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402

app = Q.QApplication(sys.argv); ui.theme.apply(app)
w = ui.Window(auto_confirm=True)
plan = w._plan()
ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got)
    ok = ok and bool(cond)


check("start is at a sun event", not plan.start_now and w.start_mode.currentIndex() == 2)
check("start event", plan.start_event == "nautical dawn (-12°)", plan.start_event)
check("start offset", plan.start_offset_min == 0, plan.start_offset_min)
check("stop event", plan.stop_event == "nautical dusk (-12°)", plan.stop_event)
check("stop offset", plan.stop_offset_min == 0, plan.stop_offset_min)
now = dt.datetime(2026, 11, 4, 22, 0, tzinfo=LOCAL_TZ)        # the night before 5 Nov 2026
plan.site_lat, plan.site_lon = 38.89, -77.04                  # a stand-in eastern-time site, not the window's built-in default
t0 = next_sun_event(plan.start_event, now, plan.site_lat, plan.site_lon)
plan.resolved_start_local = t0.isoformat(); t1 = plan.stop_dt()
check("start on 5 Nov near 05:40", t0.date() == dt.date(2026, 11, 5) and abs((t0.hour * 60 + t0.minute) - 340) <= 3, t0)
check("stop on 5 Nov near 18:03", t1.date() == dt.date(2026, 11, 5) and abs((t1.hour * 60 + t1.minute) - 1082) <= 3, t1)
print("schedule defaults:", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
