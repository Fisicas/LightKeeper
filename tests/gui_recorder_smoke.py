"""Off-screen GUI test of LightKeeper 0.2 with the simulator and the real background recorder (no hardware).

  window 1: connect simulator -> live view -> Start run (start now, ~70 s) -> recorder process starts, window attaches,
            pre-run darks, Continue (auto-confirmed) -> RECORDING; y-axis fit / wheel / double-click checks;
            CLOSE window 1 while recording -> the recorder must keep recording;
  window 2: opens, attaches by itself, replays the history, follows the run to POST_RUN, records post-run darks
            (auto-confirmed) -> run closed -> window detaches.
Screenshots of every stage go to the folder given as argument.
Run:  set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\gui_recorder_smoke.py <screenshot folder>
"""
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = Path(tempfile.mkdtemp(prefix="lk_gui_"))
os.environ["LIGHTKEEPER_HOME"] = str(TMP / "home")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import procs, ui  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402
from lightkeeper.engine import SAT_LEVEL  # noqa: E402
from lightkeeper.link import RecorderLink  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else TMP / "shots"); out.mkdir(parents=True, exist_ok=True)
app = Q.QApplication(sys.argv); ui.theme.apply(app)
results = []


def check(name, ok, info=""):
    results.append((name, bool(ok))); print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}", flush=True)


def pump(seconds, until=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents(); time.sleep(0.02)
        if until is not None and until():
            return True
    return until is None


def shot(w, name, tab="Spectrum"):
    w.tabs.setCurrentIndex([w.tabs.tabText(i) for i in range(w.tabs.count())].index(tab)); pump(0.3)
    w._draw_live(force=True); w._draw_timeseries(); w._draw_waterfall(); pump(0.3)
    w.grab().save(str(out / f"{name}.png"))


class Ev:  # minimal stand-in for a matplotlib mouse event
    def __init__(self, ax, x, y, button="up", key=None, dblclick=False):
        self.inaxes, self.xdata, self.ydata, self.button, self.key, self.dblclick, self.modifiers = ax, x, y, button, key, dblclick, set()


# ------------------------------------------------------------------ window 1
w = ui.Window(auto_confirm=True); w.resize(1700, 1000); w.show(); pump(0.5)
w.dev_combo.setCurrentIndex(w.dev_combo.findData(SIM_ID)); w._connect()
check("connect simulator", pump(10, lambda: w.state == "IDLE"))
w._toggle_preview(); pump(3, lambda: w.latest is not None); pump(1.5)
a0, a1 = w.c_spec.ax
peak = float(max(w.latest)); top = a0.get_ylim()[1]
check("auto-scale fits the data, not the 60,000 saturation line", top < 1.15 * peak and (peak > 0.8 * SAT_LEVEL or top < 0.9 * SAT_LEVEL),
      f"(peak {peak:.0f}, y top {top:.0f})")
y0 = a0.get_ylim(); w._on_scroll(Ev(a0, 600.0, 0.5 * peak, "up")); y1 = a0.get_ylim()
check("wheel stretches y about the cursor and holds the view", (y1[1] - y1[0]) < 0.85 * (y0[1] - y0[0]) and not w.cb_auto.isChecked(),
      f"{y0} -> {y1}")
x0 = a0.get_xlim(); w._on_scroll(Ev(a0, 600.0, 0.5 * peak, "up", key="shift")); x1 = a0.get_xlim()
check("shift+wheel zooms x", (x1[1] - x1[0]) < 0.85 * (x0[1] - x0[0]), f"{x0} -> {x1}")
pump(1.0); check("held view survives redraws", abs(a0.get_ylim()[1] - y1[1]) < 1e-6)
w._fit_y_now(); pump(0.3); shot(w, "01_preview_zoomed_fit_y")
w._on_click(Ev(a0, 600.0, 0.5 * peak, dblclick=True)); pump(1.0)
check("double-click resets to auto-scale", w.cb_auto.isChecked() and abs(a0.get_xlim()[0] - w.axis[0]) < 1)

now = dt.datetime.now(LOCAL_TZ)
w.out_edit.setText(str(TMP / "runs")); w.ladder_edit.setText("8,10,20,50,100,200"); w.max_spin.setValue(200); w.dark_s.setValue(0.2)
w.start_mode.setCurrentIndex(0); w.stop_mode.setCurrentIndex(0)
w.stop_edit.setDateTime(w.stop_edit.dateTime().currentDateTime().addSecs(150))
w.burst_every.setValue(0); w.cb_logon.setChecked(False)
w._start_run()
check("window attached to the background recorder", pump(45, lambda: w.link is not None))
check("pre-run darks -> Continue -> RECORDING", pump(60, lambda: w.state == "RECORDING"), w.state)
pump(8)
check("slots arrive in window 1", len(w.slots) >= 4, f"({len(w.slots)})")
shot(w, "02_recording_window1"); shot(w, "03_recording_window1_schedule", "Schedule")
sess = procs.live_sessions(); rec_pid = sess[0]["pid"] if sess else None
n_before = w.slots and len(w.slots)
w.close(); pump(1.0)
check("closing the window leaves the recorder running", procs.pid_alive(rec_pid))
probe = RecorderLink(procs.live_sessions()[0]); snap = probe.evt.get(timeout=5)
n1 = (snap.get("last_slot") or {}).get("n_slots", 0)
snap2 = None; t_end = time.time() + 6
while time.time() < t_end:
    try:
        e = probe.evt.get(timeout=0.5)
    except Exception:
        continue
    if e.get("type") == "slot":
        snap2 = e["n_slots"]
probe.close()
check("recording continues with no window open", (snap2 or 0) > n1, f"(slots {n1} -> {snap2})")

# ------------------------------------------------------------------ window 2
w2 = ui.Window(auto_confirm=True); w2.resize(1700, 1000); w2.show()
check("new window re-attaches by itself", pump(10, lambda: w2.link is not None and w2.state in ("RECORDING", "BURST")), w2.state)
pump(3)
check("history replayed into the new window", len(w2.slots) >= n_before, f"({len(w2.slots)} slots)")
shot(w2, "04_reattached_window2"); shot(w2, "05_reattached_timeseries", "Time series")
check("run reaches POST_RUN and post-run darks are recorded", pump(240, lambda: w2._closed_run), w2.state)
check("window detaches after the run closed", pump(15, lambda: w2.link is None))
check("recorder and supervisor exited", pump(20, lambda: not procs.live_sessions()))
runs = list((TMP / "runs").glob("*"))
st = procs.read_json(runs[0] / "run_state.json") if runs else {}
check("run_state.json says closed", st.get("status") == "closed", str(st.get("status")))
shot(w2, "06_after_run")
w2.close(); pump(0.5)
print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed; screenshots in {out}; temp {TMP}")
sys.exit(0 if all(ok for _, ok in results) else 1)
