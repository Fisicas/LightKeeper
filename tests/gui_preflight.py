"""GUI check (0.3.8): the run's exposure is its own (Fixed 500 ms by default, not the live view's 100 ms) and the
pre-flight checklist at Start. Off-screen, simulator only, temporary LIGHTKEEPER_HOME; no run is started. Run:
   set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\gui_preflight.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="lk_pre_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = str(HOME)
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402
from lightkeeper.schedule import estimates  # noqa: E402

app = Q.QApplication(sys.argv); ui.theme.apply(app)
results = []


def check(name, cond, info=""):
    results.append(bool(cond)); print(("[PASS] " if cond else "[FAIL] ") + name + (f"  ({info})" if info else ""))


def pump(s, until=None):
    t0 = time.time()
    while time.time() - t0 < s:
        app.processEvents(); time.sleep(0.03)
        if until and until():
            return


w = ui.Window(auto_confirm=True); w.show(); w.out_edit.setText(str(HOME / "runs"))
w.engine.send("connect", SIM_ID, True, True); pump(5, lambda: w.state == "IDLE")
w.exp_mode.setCurrentIndex(1); w.int_spin.setValue(100); w._toggle_preview(); pump(1)
w.lat.setValue(38.89); w.lon.setValue(-77.04); pump(0.3)
p = w._plan()
check("the default back-off at 500 ms warns what its 51 times cost", any(lv == "warn" and t.startswith("Back off makes 50 integration")
      for lv, t in w._preflight(p, estimates(p, 2068, int(w.info.get("min_int_us_used", 8000)), int(w.info.get("max_int_us", 6e7))))))
w.run_int.setValue(50); pump(0.2); p = w._plan()
check("at 50 ms it steps 50, 40, 30, 20, 10 with no warning", not any(t.startswith("Back off makes") for _, t in w._preflight(
      p, estimates(p, 2068, int(w.info.get("min_int_us_used", 8000)), int(w.info.get("max_int_us", 6e7))))))
w.run_int.setValue(500); w.backoff_step.setValue(0); pump(0.2); p = w._plan()     # the rest: plain fixed exposure
check("a run defaults to Fixed 500 ms, whatever the live view uses", (p.auto_exposure, p.int_ms) == (False, 500.0), f"live view at {w.latest_int}")
check("the automatic range is greyed out under Fixed", not w.auto_range_row.isEnabled())
dev = lambda: (int(w.info.get("min_int_us_used", 8000)), int(w.info.get("max_int_us", 6e7)))  # noqa: E731
e = estimates(p, 2068, *dev())
check("darks at the burst, fixed and long times", e["dark_ladder_ms"] == [50, 500, 3000], e["dark_ladder_ms"])
chk = dict((t.split(" ")[0] + " " + t.split(" ")[1], lv) for lv, t in w._preflight(p, e))
lines = w._preflight(p, e)
for lv, t in lines:
    print(f"      {lv:4s} {t}")
check("fixed exposure is green", ("ok", "Fixed exposure at 500 ms") in lines)
check("bursts with their own dark are green", any(lv == "ok" and t.startswith("15 bursts at 50 ms") or (lv == "ok" and "bursts at 50 ms" in t) for lv, t in lines))
check("dark times are green", ("ok", "Darks at 50, 500, 3000 ms") in lines)
check("empty setup fields are flagged", any(lv == "warn" and t.startswith("Not filled") for lv, t in lines))
check("temperature source is reported", any("Temperature logged" in t or "No readable temperature" in t for _, t in lines))
check("disk space is checked", any(t.startswith("Disk:") for _, t in lines))
w.run_exp.setCurrentIndex(1); pump(0.2)
p2 = w._plan(); l2 = w._preflight(p2, estimates(p2, 2068, *dev()))
check("Automatic is selectable and flagged amber", p2.auto_exposure and any(lv == "warn" and t.startswith("Automatic exposure") for lv, t in l2))
check("and enables the automatic range", w.auto_range_row.isEnabled())
w.run_exp.setCurrentIndex(0); w.long_dark.setValue(0); w.burst_every.setValue(0); pump(0.2)
p3 = w._plan(); l3 = w._preflight(p3, estimates(p3, 2068, *dev()))
check("one dark time only is flagged", any(lv == "warn" and t.startswith("Darks at one integration time") for lv, t in l3))
w.lon.setValue(77.04); pump(0.3); p4 = w._plan()
l4 = w._preflight(p4, estimates(p4, 2068, *dev()))
check("a longitude that does not fit the time zone is flagged", any(lv in ("warn", "bad") and "West longitudes" in t for lv, t in l4))
w.engine.send("quit"); pump(0.5)
print(f"\n{sum(results)}/{len(results)} checks passed; temp {HOME}")
sys.exit(0 if all(results) else 1)
