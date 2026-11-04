"""GUI check (0.3.12): a new run starts from the previous run's setup; the wavelength cursor; the slider through the
recorded slots; the raw-counts waterfall. Off-screen, simulator only, temporary folders. A real run folder may be passed
as the first argument, to take screenshots from it (only read): tests/gui_setup_cursor_slider.py [run folder] [png folder]"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lk_setup_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = str(TMP / "home")
os.environ["LIGHTKEEPER_DATA_ROOT"] = str(TMP / "empty")
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import h5py  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.backend_bases import KeyEvent, MouseEvent  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402
from lightkeeper.devices import SimDevice  # noqa: E402
from lightkeeper.schedule import next_clock_time, next_label  # noqa: E402
from lightkeeper.storage import LOCAL_TZ  # noqa: E402

n_ok = n_bad = 0


def check(name, cond, got=""):
    global n_ok, n_bad
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({got})" if got != "" else ""))
    n_ok += bool(cond); n_bad += not cond


app = Q.QApplication(sys.argv); ui.theme.apply(app)


def pump(s=0.2, until=None):
    end = time.time() + s
    while time.time() < end and not (until and until()):
        app.processEvents(); time.sleep(0.01)


def write_prior(folder, name, app_name="LightKeeper 0.3.12", **plan):
    d = folder / name; d.mkdir(parents=True)
    (d / "run_metadata.json").write_text(json.dumps(dict(app=app_name, planned_start_local=name[:10] + "T06:00:00-05:00", plan=plan)), encoding="utf-8")


w = ui.Window(auto_confirm=True); w.resize(1600, 1000); w.show(); pump(0.3)

# ---------------------------------------------------------------- pure helpers
check("a label ending in 'day N' counts up", next_label("canopy 10 ms fixed day 2") == "canopy 10 ms fixed day 3")
check("underscores too", next_label("canopy_day_9") == "canopy_day_10")
check("any other label stays", next_label("birthday party") == "birthday party" and next_label("day 2 canopy") == "day 2 canopy")
after = dt.datetime(2026, 11, 4, 22, 0, tzinfo=LOCAL_TZ)
check("a clock time already past goes to tomorrow", next_clock_time("2026-11-01T06:30:00-05:00", after) == dt.datetime(2026, 11, 5, 6, 30, tzinfo=LOCAL_TZ))
check("a clock time still to come stays today", next_clock_time("2026-11-01T23:30:00-05:00", after) == dt.datetime(2026, 11, 4, 23, 30, tzinfo=LOCAL_TZ))

# ---------------------------------------------------------------- previous-run setup
check("with no previous run the defaults stand", w.start_event.currentText() == "nautical dawn (-12°)" and not w.setup_src.text())
A = TMP / "a"
write_prior(A, "2026-11-03_older_v01", label="older day 1", diffuser_id="should not be used")
write_prior(A, "2026-11-04_canopy_day_2_v01", label="canopy day 2", notes="clear sky", geometry="fiber + bag", diffuser_id="PE foam",
            fiber_core_um=600.0, view_az_deg=None, view_el_deg=90.0, window_state="open", site_lat=38.89, site_lon=-77.04, site_alt_m=None,
            start_now=False, start_event="civil dawn (-6°)", start_offset_min=-5.0, stop_event="civil dusk (-6°)", stop_offset_min=15.0,
            slot_s=2.0, auto_exposure=False, int_ms=10.0, backoff_step_ms=10.0, backoff_min_ms=10.0, burst_every_min=30.0, burst_offset_min=7.0,
            burst_s=60.0, burst_max_int_ms=20.0, dark_s_per_int=3.0, long_dark_ms=2000.0, ladder_ms=[10.0, 20.0], edc=False, nlc=True, store_sd=False,
            pre_darks=True, post_darks=False, auto_resume_logon=False, setup_photo=str(TMP / "gone.jpg"),
            resolved_start_local="2026-11-04T06:30:00-05:00", resolved_stop_local="2026-11-04T18:30:00-05:00")
w.out_edit.setText(str(A)); w._apply_previous_setup(); pump(0.2)
p = w._plan()
check("the newest run is the one used", "2026-11-04_canopy_day_2_v01" in w.setup_src.toolTip(), w.setup_src.toolTip())
check("the label counts up", p.label == "canopy day 3", p.label)
check("notes, geometry and diffuser carry over", (p.notes, p.geometry, p.diffuser_id) == ("clear sky", "fiber + bag", "PE foam"))
check("fiber, view and window carry over", (p.fiber_core_um, p.view_el_deg, p.window_state) == (600.0, 90.0, "open"))
check("fields not set stay not set", p.view_az_deg is None and p.site_alt_m is None)
check("the site carries over", (p.site_lat, p.site_lon) == (38.89, -77.04))
check("the sun events and offsets carry over", (p.start_event, p.start_offset_min, p.stop_event, p.stop_offset_min) == ("civil dawn (-6°)", -5.0, "civil dusk (-6°)", 15.0))
check("acquisition carries over", (p.slot_s, p.int_ms, p.backoff_step_ms, p.burst_every_min, p.burst_offset_min, p.burst_s, p.burst_max_int_ms, p.dark_s_per_int, p.long_dark_ms)
      == (2.0, 10.0, 10.0, 30.0, 7.0, 60.0, 20.0, 3.0, 2000.0))
check("switches carry over", (p.edc, p.nlc, p.store_sd, p.pre_darks, p.post_darks, p.auto_resume_logon) == (False, True, False, True, False, False))
check("the ladder carries over", p.ladder_ms == [10.0, 20.0], p.ladder_ms)
check("a setup photo that is gone is not kept", p.setup_photo == "")
check("times confirmed for the last run are not kept", not p.resolved_start_local and not p.resolved_stop_local)
w.label_edit.setText("edited by hand"); w._apply_previous_setup()
check("the same run is not applied twice, so edits stay", w.label_edit.text() == "edited by hand")
write_prior(A, "2026-11-05_canopy_day_3_v01", label="canopy day 3", site_lat=38.89, site_lon=-77.04)
w._handle(dict(type="run_closed", dir="x", rows=0)); pump(0.1)
check("a run closing sets up the one after it", w.label_edit.text() == "canopy day 4", w.label_edit.text())

B = TMP / "b"
write_prior(B, "2026-11-04_old_v01", app_name="LightKeeper 0.3.11", label="old", start_now=False, start_event="astronomical dawn (-18°)", start_offset_min=-20.0,
            stop_event="astronomical dusk (-18°)", stop_offset_min=20.0)
w.start_event.setCurrentText("nautical dawn (-12°)"); w.start_off.setValue(0); w.stop_event.setCurrentText("nautical dusk (-12°)"); w.stop_off.setValue(0)
w.out_edit.setText(str(B)); w._apply_previous_setup(); p = w._plan()
check("the old astronomical defaults are not inherited", (p.start_event, p.start_offset_min, p.stop_event, p.stop_offset_min) == ("nautical dawn (-12°)", 0.0, "nautical dusk (-12°)", 0.0))
C = TMP / "c"
write_prior(C, "2026-11-04_new_v01", label="astro on purpose", start_now=False, start_event="astronomical dawn (-18°)", start_offset_min=-20.0,
            stop_event="astronomical dusk (-18°)", stop_offset_min=20.0)
w.out_edit.setText(str(C)); w._apply_previous_setup(); p = w._plan()
check("the same choice made from 0.3.12 on is inherited", (p.start_event, p.start_offset_min) == ("astronomical dawn (-18°)", -20.0))
D = TMP / "d"
write_prior(D, "2026-11-04_set_v01", label="set times", start_now=False, start_event="", start_local="2026-11-04T06:30:00-05:00", stop_event="", stop_local="2026-11-04T18:45:00-05:00")
w.out_edit.setText(str(D)); w._apply_previous_setup(); pump(0.1)
s0, s1 = w.start_edit.dateTime().toPython(), w.stop_edit.dateTime().toPython()
check("set times keep the clock time and move to a future date", (s0.hour, s0.minute, s1.hour, s1.minute) == (6, 30, 18, 45) and dt.datetime.now() < s0 < s1, f"{s0} to {s1}")
check("and the rows shown match", w.start_mode.currentIndex() == 1 and w.stop_mode.currentIndex() == 0)
(TMP / "e").mkdir(); write_prior(TMP / "e", "2026-11-04_bad_v01", label="garbled")
((TMP / "e") / "2026-11-04_bad_v01" / "run_metadata.json").write_text("{not json", encoding="utf-8")
w.out_edit.setText(str(TMP / "e")); w._setup_from = None; w._apply_previous_setup()
check("an unreadable previous run changes nothing", w._setup_from is None)


# ---------------------------------------------------------------- a synthetic recorded run
def synthetic_run(folder: Path):
    """04:00-20:00 at 30 s slots: dark at night, a sine-shaped day from 07:00 to 17:00."""
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
    return mean


raw = synthetic_run(TMP / "run")
w._start_load(str(TMP / "run")); pump(120, lambda: w._load is None); pump(0.3)
check("the recorded run opens", w.viewing is not None and len(w.spectra) > 100, len(w.spectra))
w.tabs.setCurrentIndex(0); pump(0.3)

# ---------------------------------------------------------------- slider
n = len(w.spectra); a1 = w.c_spec.ax[1]
check("the slider is enabled", w.scrub.isEnabled())
check("its right end is the newest slot", w.scrub.maximum() == n - 1 and w.scrub.value() == n - 1)
t_left = w.spectra[w.scrub.minimum()][0]
check("its left end is the start of usable light, not the first slot", w.scrub.minimum() > 0 and t_left.hour == 7, f"{t_left:%H:%M}")
check("the end labels show the times", w.scrub_first.text() == f"{t_left:%H:%M}" and w.scrub_last.text() == f"{w.spectra[-1][0]:%H:%M}", (w.scrub_first.text(), w.scrub_last.text()))
y = w.scrub.height() // 2
QtTest.QTest.mouseClick(w.scrub, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(w.scrub.width() // 2, y)); pump(0.3)
mid = w.scrub.value()
check("a click in the middle jumps there", abs(mid - (w.scrub.minimum() + w.scrub.maximum()) / 2) < 0.03 * n, mid)
check("the plot shows the slots up to that time", w._recent_shown[0] == w.spectra[mid][0] and len(a1.get_lines()) == 24 and "Slots up to" in a1.get_title(loc="left"),
      (a1.get_title(loc="left"), len(a1.get_lines()), w._recent_shown[0], w.spectra[mid][0], w.scrub.value()))
QtTest.QTest.mouseClick(w.scrub, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(1, y)); pump(0.3)
check("a click at the far left goes to the start of usable light", w.scrub.value() == w.scrub.minimum() and w._recent_shown[0] == t_left)
check("nothing before usable light is drawn", len(a1.get_lines()) == 1, len(a1.get_lines()))
QtTest.QTest.mouseClick(w.scrub, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(w.scrub.width() - 1, y)); pump(0.3)
check("a click at the far right goes to the end", w.scrub.value() == n - 1 and w._scrub_t is None and a1.get_title(loc="left").startswith("Recent slots"),
      (a1.get_title(loc="left"), w.scrub.value(), n - 1, w._scrub_t, w.scrub.width()))
QtTest.QTest.mousePress(w.scrub, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(w.scrub.width() // 4, y))
QtTest.QTest.mouseMove(w.scrub, QtCore.QPoint(w.scrub.width() // 2, y)); pump(0.1)
QtTest.QTest.mouseRelease(w.scrub, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(w.scrub.width() // 2, y)); pump(0.3)
check("dragging moves it", abs(w.scrub.value() - mid) < 0.05 * n, w.scrub.value())
# new slots arrive: a position in the past stays put; the far right follows the newest
held = w._scrub_t; extra = w.spectra[-1]
w.spectra.append((extra[0] + dt.timedelta(seconds=30), extra[1], extra[2])); w._draw_recent()
check("a position in the past stays on its time as slots arrive", w._scrub_t == held and w.spectra[w.scrub.value()][0] == held and w.scrub.maximum() == n)
w.spectra[:] = w.spectra[::2]; w._usable_cache = (None, 0); w._draw_recent()
check("and when the store thins itself out", abs((w.spectra[w.scrub.value()][0] - held).total_seconds()) <= 60)
w.scrub.setValue(w.scrub.maximum()); pump(0.2)
last = w.spectra[-1]; w.spectra.append((last[0] + dt.timedelta(seconds=60), last[1], last[2])); w._draw_recent()
check("the far right follows the newest slot", w.scrub.value() == len(w.spectra) - 1 and w._recent_shown[0] == w.spectra[-1][0])
del w.spectra[-1]

# ---------------------------------------------------------------- cursor
c = w.spec_cursor; a0 = w.c_spec.ax[0]
check("the cursor starts off", not c.on and not w.spec_cur_info.text())
c.axes_before = None
w.cb_spec_cur.setChecked(True); pump(0.1)
check("ticking it asks for a click", "Click" in w.spec_cur_info.text(), w.spec_cur_info.text())


def click(canvas, ax, x, yfrac=0.5, name="button_press_event"):
    px, py = ax.transData.transform((x, ax.get_ylim()[0] + yfrac * (ax.get_ylim()[1] - ax.get_ylim()[0])))
    MouseEvent(name, canvas, px, py, button=1)._process()
    MouseEvent("button_release_event", canvas, px, py, button=1)._process()


w.c_spec.draw(); pump(0.1)
click(w.c_spec, a1, 760.3)
xs = w.axis; want = float(xs[np.argmin(np.abs(xs - 760.3))])
check("a click places it on the nearest pixel", c.x == want, c.x)
check("the wavelength is shown", f"{want:.2f} nm" in w.spec_cur_info.text(), w.spec_cur_info.text())
sp = w._recent_shown; want_y = sp[2][np.argmin(np.abs(sp[1] - want))]
check("with the value of the newest slot shown", f"{want_y:,.1f} counts/s" in w.spec_cur_info.text(), f"{want_y:,.1f}")
check("the line is on both plots", sum(ln.get_gid() == "cursor" for a in (a0, a1) for ln in a.get_lines()) == 2)
ylim = a1.get_ylim(); w._fit_y(a1, a0.get_xlim())
check("the line does not change the y scale", abs(a1.get_ylim()[1] - ylim[1]) < 1e-6 * max(abs(ylim[1]), 1))
w._draw_recent(); pump(0.1)
check("it survives a redraw, with no extras", sum(ln.get_gid() == "cursor" for a in (a0, a1) for ln in a.get_lines()) == 2 and c.x == want)
i0 = int(np.argmin(np.abs(xs - c.x)))
KeyEvent("key_press_event", w.c_spec, "right")._process()
check("the right arrow moves one pixel", c.x == float(xs[i0 + 1]), c.x)
KeyEvent("key_press_event", w.c_spec, "shift+left")._process()
check("Shift+left moves ten", c.x == float(xs[i0 - 9]), c.x)
w.cb_spec_cur.setChecked(False); pump(0.1)
check("unticking removes it", not w.spec_cur_info.text() and not any(ln.get_gid() == "cursor" for a in (a0, a1) for ln in a.get_lines()))
x_before = c.x
click(w.c_spec, a1, 500.0)
check("while off, a click does nothing", c.x == x_before)

# ---------------------------------------------------------------- raw waterfall
w.range_mode.setCurrentIndex(1); w.tabs.setCurrentIndex(1)
items = [w.wf_mode.itemText(i) for i in range(w.wf_mode.count())]
check("the waterfall offers raw counts", any("Raw counts" in t for t in items), items)
w.wf_mode.setCurrentIndex(0); pump(0.3); shape = w._wf_shown[2].copy()
w.wf_mode.setCurrentIndex(items.index("Raw counts, as recorded")); pump(0.3)
xs_w, tn, Z, unit = w._wf_shown
check("the colour bar says what it shows", "no dark subtraction" in w.wf_cbar.ax.get_ylabel(), w.wf_cbar.ax.get_ylabel())
check("the other modes are unchanged", shape.shape == Z.shape and not np.allclose(shape, Z))
kept = raw[::2 * w.viewing["stride"]]; m = (w.axis > 380) & (w.axis < 1050); ok = w.valid[m]; nz = min(len(kept), Z.shape[0])   # the slider test thinned the store once
check("raw values equal the stored means (hidden pixels filled in)", np.allclose(Z[:nz][:, ok], kept[:nz][:, m][:, ok], atol=0.01), f"{nz} spectra")
check("the night level includes the dark, about 1500 counts", abs(np.median(Z[0, ok]) - 1500) < 20, f"{np.median(Z[0, ok]):.0f}")
w.cb_wf_cur.setChecked(True); w.c_wf.draw(); pump(0.1)
ax = w.c_wf.ax[0]
click(w.c_wf, ax, 650.0, 0.5)
txt = w.wf_cur_info.text(); i = int(np.argmin(np.abs(xs_w - 650.0))); j = int(np.argmin(np.abs(tn - w.wf_cursor.y)))
check("the waterfall cursor reads wavelength, time and counts", f"{xs_w[i]:.2f} nm" in txt and f"{Z[j, i]:,.3g} counts per scan" in txt, txt)
w.wf_mode.setCurrentIndex(0); pump(0.3)
check("and keeps its place when the view changes", w.wf_cursor.x is not None and sum(ln.get_gid() == "cursor" for ln in ax.get_lines()) == 2)
w._close_recorded_run(); pump(0.2)
check("closing the run empties the slider", not w.scrub.isEnabled() and w._recent_shown is None and not w.scrub_first.text())

if len(sys.argv) > 2:
    png = Path(sys.argv[2]); png.mkdir(parents=True, exist_ok=True)
    w.range_mode.setCurrentIndex(0); w.cb_wf_cur.setChecked(False)
    w._start_load(sys.argv[1]); pump(300, lambda: w._load is None); pump(0.5)
    w.tabs.setCurrentIndex(0); w.cb_spec_cur.setChecked(True); pump(0.3); w.spec_cursor.x = None; w.spec_cursor.enable(True); w.c_spec.draw(); pump(0.2)
    click(w.c_spec, w.c_spec.ax[1], 760.4); w.scrub.setValue((w.scrub.minimum() + w.scrub.maximum()) // 2); pump(0.5); w.c_spec.draw(); pump(0.2)
    w.grab().save(str(png / "spectrum_cursor_slider.png"))
    w.tabs.setCurrentIndex(1); w.wf_mode.setCurrentIndex(3); w.wf_cursor.x = None; w.cb_wf_cur.setChecked(True); pump(0.5); w.c_wf.draw(); pump(0.2)
    click(w.c_wf, w.c_wf.ax[0], 760.4, 0.5); pump(0.3); w.c_wf.draw(); pump(0.2)
    w.grab().save(str(png / "waterfall_raw_cursor.png"))

print(f"\n{n_ok}/{n_ok + n_bad} checks passed; temp {TMP}")
w.close(); sys.exit(1 if n_bad else 0)
