"""GUI check (0.4.2): Full scale shows each detector's whole range with the saturation line in view, and holds while the
live view updates; the wavelength cursor works with pan/zoom, on the colour strip and at hidden pixels, and pointing at a
plot reads the wavelength there. Off-screen, no spectrometer (scans are handed to the window), temporary folders."""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lk_full_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.update(LIGHTKEEPER_HOME=str(TMP / "home"), LIGHTKEEPER_DATA_ROOT=str(TMP / "empty"), LIGHTKEEPER_AUTOCONNECT="0",
                  LIGHTKEEPER_NO_INSTALL="1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
from matplotlib.backend_bases import LocationEvent, MouseEvent  # noqa: E402
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402

ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


app = Q.QApplication(sys.argv[:1]); ui.theme.apply(app)


def pump(s=0.15):
    end = time.time() + s
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)


w = ui.Window(auto_confirm=True); w.resize(1600, 1000); w.show(); pump(0.3)
a0, a1 = w.c_spec.ax


def connect(model, max_counts, sat, npx, wl0, wl1):
    """What the window holds after a 'device' event, without hardware."""
    w._handle(dict(type="device", info=dict(model=model, serial="T1", pixels=npx, native_max_counts=max_counts,
                                             saturation_level_counts=sat, wl_first=wl0, wl_last=wl1),
                   wavelengths=np.linspace(wl0, wl1, npx)))


def scan(peak, int_ms, npx):
    x = np.linspace(0, 1, npx)
    w.latest = 280 + peak * np.exp(-((x - 0.4) / 0.15) ** 2); w.latest_int = int(int_ms * 1000)
    w._draw_live(force=True); pump()


# ---------------------------------------------------------------- full scale, USB650 (12 bit)
connect("USB650", 4095.0, 3749.1, 2048, 339.9, 1025.0); scan(2700, 50, 2048)
check("auto-scale fits the data first (top far below the 4,095 ceiling)", a0.get_ylim()[1] < 3200, a0.get_ylim())
w.btn_full.click(); pump()
lo, hi = a0.get_ylim()
check("Full scale: USB650 top just above 4,095", 4095 <= hi <= 4095 * 1.05, (lo, hi))
check("the saturation line (3,749) is in view", lo < 3749.1 < hi and w.l_sat.get_visible())
check("from 0", lo <= 0 < 0.05 * hi, lo)
check("the whole wavelength range", abs(a0.get_xlim()[0] - 339.9) < 1e-6 and abs(a0.get_xlim()[1] - 1025.0) < 1e-6, a0.get_xlim())
check("Auto-scale unticks itself", not w.cb_auto.isChecked())
scan(1500, 50, 2048)
check("it holds while new scans arrive", abs(a0.get_ylim()[1] - hi) < 1e-6, a0.get_ylim())
w.cb_rate.setChecked(True); pump()
check("in counts/s it follows the units (x20 at 50 ms)", abs(a0.get_ylim()[1] - hi * 20) < 1e-3 * hi * 20, a0.get_ylim())
w.cb_rate.setChecked(False); pump()

# switching to a 16-bit spectrometer keeps full scale, now to 64,000
connect("MayaPro2000", 64000.0, 60000.0, 2068, 200.0, 1122.8); scan(20000, 10, 2068)
lo, hi = a0.get_ylim()
check("after switching to a Maya: top just above 64,000", 64000 <= hi <= 64000 * 1.05, (lo, hi))
check("the Maya's saturation line (60,000) is in view, labelled", lo < 60000 < hi and "60,000" in w.l_sat.get_label(), w.l_sat.get_label())
w.cb_log.setChecked(True); pump()
check("log y: full scale to above 64,000 too", a0.get_ylim()[1] >= 64000 and a0.get_ylim()[0] > 0, a0.get_ylim())
w.cb_log.setChecked(False); pump()

# what ends it
w.btn_fity.click(); scan(20000, 10, 2068)
check("Fit y ends full scale", a0.get_ylim()[1] < 30000, a0.get_ylim())
w.btn_full.click(); pump()
w.cb_auto.setChecked(True); pump()
check("ticking Auto-scale ends it", not w._full_hold and a0.get_ylim()[1] < 30000, a0.get_ylim())
w.btn_full.click(); pump(); w.c_spec.draw()
px, py = a0.transData.transform((600.0, 30000.0))
MouseEvent("scroll_event", w.c_spec, px, py, button="up", step=1)._process(); pump()
scan(20000, 10, 2068)
check("zooming with the wheel ends it", not w._full_hold)

# ---------------------------------------------------------------- cursor
w.cb_auto.setChecked(True); scan(20000, 10, 2068); w.c_spec.draw()
c = w.spec_cursor; xs = w.axis


def mouse(name, ax, x, yfrac=0.5, button=1):
    px, py = ax.transData.transform((x, ax.get_ylim()[0] + yfrac * (ax.get_ylim()[1] - ax.get_ylim()[0])))
    MouseEvent(name, w.c_spec, px, py, button=button)._process()


w.spec_tb.pan(); pump()
check("pan is on before the cursor", "pan" in str(w.spec_tb.mode))
w.cb_spec_cur.setChecked(True); pump()
check("ticking Cursor turns pan off", not str(w.spec_tb.mode), str(w.spec_tb.mode))
mouse("button_press_event", a0, 656.3); mouse("button_release_event", a0, 656.3)
i = int(np.argmin(np.abs(xs - 656.3)))
check("a click places it on the nearest pixel", c.x == float(xs[i]), c.x)
check("the readout gives wavelength and pixel", f"{xs[i]:.2f} nm</b> (pixel {i})" in w.spec_cur_info.text(), w.spec_cur_info.text())
check("and the live value at that pixel", f"live {w._live_shown[0][i]:,.1f} counts" in w.spec_cur_info.text())
labels = [t for t in a0.texts if t.get_gid() == "cursor"]
check("the wavelength is printed on the plot", len(labels) == 1 and labels[0].get_text() == f"{xs[i]:.2f} nm", [t.get_text() for t in labels])
w.spec_tb.zoom(); pump()
x_before = c.x
mouse("button_press_event", a0, 500.0); mouse("button_release_event", a0, 500.0)
check("with zoom on, a click says why it did not move the cursor", c.x == x_before and "zoom is on" in w.spec_cur_info.text(),
      w.spec_cur_info.text())
w.spec_tb.zoom(); pump()
w.c_spec.draw()
mouse("button_press_event", w.ax_strip, 760.0, 0.5); mouse("button_release_event", w.ax_strip, 760.0, 0.5)
j = int(np.argmin(np.abs(xs - 760.0)))
check("a click on the colour strip places it too", c.x == float(xs[j]), c.x)

# a hidden (hot) pixel is reported as hidden, not as its neighbour's value
w.valid = np.ones(len(xs), bool); w.valid[j] = False; w.cb_mask.setChecked(True); scan(20000, 10, 2068)
check("at a hidden pixel the readout says so", "hidden hot/cold pixel" in w.spec_cur_info.text(), w.spec_cur_info.text())
w.valid[:] = True; scan(20000, 10, 2068)

# pointing: no click needed
w.c_spec.draw()
mouse("motion_notify_event", a0, 550.0, button=None)
h = int(np.argmin(np.abs(xs - 550.0)))
check("pointing shows the wavelength and pixel there", f"Pointer <b>{xs[h]:.2f} nm</b> (pixel {h})" in w.spec_cur_info.text(),
      w.spec_cur_info.text())
check("alongside the placed cursor", "Cursor <b>" in w.spec_cur_info.text())
LocationEvent("figure_leave_event", w.c_spec, 0, 0)._process(); w._spec_hover(None)
check("leaving the plot removes the pointer readout", "Pointer" not in w.spec_cur_info.text() and "Cursor" in w.spec_cur_info.text(),
      w.spec_cur_info.text())
w.cb_spec_cur.setChecked(False); pump()
mouse("motion_notify_event", a0, 900.0, button=None)
check("pointing works with the cursor off", "Pointer" in w.spec_cur_info.text() and "Cursor" not in w.spec_cur_info.text(),
      w.spec_cur_info.text())
w._spec_hover(None)
check("and clears afterwards", w.spec_cur_info.text() == "", w.spec_cur_info.text())

w.close(); pump()
print("PASS" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
