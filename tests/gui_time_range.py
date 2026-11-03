"""GUI check (0.3.7): time range for the Waterfall and Time series tabs, and opening a recorded run read-only.
Off-screen, simulator only, temporary LIGHTKEEPER_HOME. A synthetic run with a dark night, a day and a dark evening is
written to a temporary folder first; pass a real run folder as the first argument to open that one instead (it is
only read). Run:  set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\gui_time_range.py [run folder]"""
import datetime as dt
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="lk_range_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = str(HOME)
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import h5py  # noqa: E402
import numpy as np  # noqa: E402
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import runview, ui  # noqa: E402
from lightkeeper.devices import SimDevice  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402


def synthetic_run(folder: Path):
    """04:00-20:00 at 30 s slots: dark noise at night, a sine-shaped day from 07:00 to 17:00."""
    folder.mkdir(parents=True)
    sim = SimDevice(); wl = sim.wavelengths; n_px = len(wl); rng = np.random.default_rng(3)
    day0 = dt.datetime(2026, 11, 2, tzinfo=LOCAL_TZ)
    t = [day0 + dt.timedelta(hours=4, seconds=30 * i) for i in range(16 * 120)]
    h = np.array([(x - day0).total_seconds() / 3600 for x in t])
    light = np.clip(np.sin(np.pi * (h - 7) / 10), 0, None) ** 1.5
    dark = 1500.0 + sim.hot * 1.0
    mean = dark + 4.0e4 * light[:, None] * sim.sky[None, :] + rng.normal(0, 30, (len(t), n_px))
    with h5py.File(folder / "spectra_20261102_0400.h5", "w") as f:
        f.create_dataset("wavelength_nm_device", data=wl)
        d = f.create_group("dark_scans")
        d["int_us"] = np.full(5, 1_000_000, np.int32); d["session"] = np.ones(5, np.int32)
        d["label"] = np.array(["pre"] * 5, dtype=h5py.string_dtype()); d["spectrum"] = np.tile(dark, (5, 1)).astype(np.float32)
        d["t_wall_ns"] = np.zeros(5, np.int64)
        b = f.create_group("blocks")
        b["mean"] = mean.astype(np.float32); b["int_us"] = np.full(len(t), 1_000_000, np.int32)
        b["t_wall_ns"] = np.array([int(x.timestamp() * 1e9) for x in t], np.int64); b["n_scans"] = np.ones(len(t), np.int16)
        b["max_raw_window"] = mean.max(1).astype(np.float32); b["n_saturated_scans"] = np.zeros(len(t), np.int16)
    return folder


run = Path(sys.argv[1]) if len(sys.argv) > 1 else synthetic_run(HOME / "runs" / "2026-11-02_synthetic_v01")
digest = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(run.iterdir()) if p.is_file()}
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


w = ui.Window(auto_confirm=True); w.resize(1600, 1000); w.show(); pump(0.5)
w._start_load(str(run)); pump(120, lambda: w._load is None)
check("the recorded run opens", w.viewing is not None and len(w.slots) > 100, f"{len(w.slots):,} slots")
check("the header says it is read-only", "read-only" in w.run_lbl.text())
a, b, txt = w._time_window()
check("usable daylight found automatically", a is not None and b is not None, txt)
if len(sys.argv) == 1:
    check("the synthetic day is found between 07:00 and 17:00", a.hour == 7 and b.hour in (16, 17), txt)
shown_wf, _ = w._in_window(w.spectra, lambda sp: sp[0])
check("the waterfall shows only the usable part", 1 < len(shown_wf) < len(w.spectra), f"{len(shown_wf)} of {len(w.spectra)} spectra")
check("the range is quoted on screen", w.range_info.text().startswith("Showing usable daylight"), w.range_info.text())
w.range_mode.setCurrentIndex(1); pump(0.5)
check("All data shows everything", w._in_window(w.spectra, lambda sp: sp[0])[0] == w.spectra)
w.range_mode.setCurrentIndex(2); pump(0.5)
f0 = w.range_from.dateTime(); w.range_from.setDateTime(f0.addSecs(3600)); pump(0.5)
ca, cb, ctxt = w._time_window()
check("Custom range starts from the usable window and can be moved", ca is not None and abs((ca - a).total_seconds() - 3600) < 61, ctxt)
w.btn_range_reset.click(); pump(0.5)
check("Reset goes back to usable daylight", w.range_mode.currentIndex() == 0 and w._time_window()[0] == a)
w.tabs.setCurrentIndex(1); pump(0.3)
check("the range bar shows on the Waterfall tab", w.range_bar.isVisible())
w.tabs.setCurrentIndex(0); pump(0.3)
check("and hides on the Spectrum tab", not w.range_bar.isVisible())
# 0.3.9: the bar sits under the tab strip, inside the page, so switching tabs never moves the tabs
bar_y = {}
for i in range(w.tabs.count()):
    w.tabs.setCurrentIndex(i); pump(0.3)
    bar_y[w.tabs.tabText(i)] = w.tabs.tabBar().mapTo(w, w.tabs.tabBar().rect().topLeft()).y()
check("the tabs stay put on every tab", len(set(bar_y.values())) == 1, bar_y)
for name in ("Waterfall", "Time series"):
    w.tabs.setCurrentIndex([w.tabs.tabText(i) for i in range(w.tabs.count())].index(name)); pump(0.3)
    tb = w.tabs.tabBar(); below = w.range_bar.mapTo(w, w.range_bar.rect().topLeft()).y() >= tb.mapTo(w, tb.rect().bottomLeft()).y()
    check(f"on {name} the range bar is under the tabs, inside the page", w.range_bar.isVisible() and below
          and w.range_bar.parent() is w.tabs.currentWidget())
w.tabs.setCurrentIndex(1); pump(0.5)
check("the waterfall still draws with the bar moved", len(w.c_wf.ax[0].images) > 0)
w.grab().save(str(HOME / "waterfall_tabs_fixed_v01.png"))
w.tabs.setCurrentIndex(0); pump(0.3)
w._close_recorded_run(); pump(0.3)
check("closing the recorded run clears the graphs", w.viewing is None and not w.slots)
after = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(run.iterdir()) if p.is_file()}
check("the run folder is unchanged (read-only)", after == digest)
w.engine.send("quit"); pump(0.5)
print(f"\n{sum(results)}/{len(results)} checks passed; temp {HOME}")
sys.exit(0 if all(results) else 1)
