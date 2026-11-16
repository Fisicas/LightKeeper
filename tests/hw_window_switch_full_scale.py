"""Hardware check (0.4.2) through the real window, offscreen: it opens the spectrometer used last by itself (no question),
Full scale fits that detector's range, the cursor reads live counts, and picking the other spectrometer in the list switches
to it with the live view running again (to the simulator, if only one spectrometer is attached). Close OceanView and LightKeeper first.
Usage: python -X utf8 tests/hw_window_switch_full_scale.py [screenshot folder]"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME = tempfile.mkdtemp(prefix="lk_hwwin_")
os.environ.update(LIGHTKEEPER_HOME=HOME, LIGHTKEEPER_AUTOCONNECT="1", LIGHTKEEPER_DATA_ROOT=str(Path(HOME) / "runs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
import numpy as np  # noqa: E402
from matplotlib.backend_bases import MouseEvent  # noqa: E402
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import devices, ui  # noqa: E402

ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


app = Q.QApplication(sys.argv[:1]); ui.theme.apply(app)
devices.ensure_seabreeze(install=True)


def pump(s, until=None):
    end = time.time() + s
    while time.time() < end and not (until and until()):
        app.processEvents(); time.sleep(0.01)


w = ui.Window(auto_confirm=True); w.resize(1600, 1000); w.show()
ids = [w.dev_combo.itemData(k) for k in range(w.dev_combo.count())]
real = [i for i in ids if isinstance(i, str)]
print("listed:", [w.dev_combo.itemText(k) for k in range(w.dev_combo.count())])
check("at least one spectrometer listed by name", len(real) >= 1, real)
last = real[-1].split(":", 1)[1]
w.settings.setValue("last_serial", last)                              # this one was used last
# the window opens it by itself 0.7 s after start-up; its 1 s tick must not start a second connect meanwhile (0.4.2)
pump(20, until=lambda: w.state == "PREVIEW" and w.latest is not None)
check("the one used last opened by itself, live", w.state == "PREVIEW" and w.info.get("serial") == last, (w.state, w.info.get("serial")))


def full_scale_and_cursor(tag):
    pump(1.0)
    mx = float(w.info["native_max_counts"])
    w.btn_full.click(); pump(1.5)
    lo, hi = w.c_spec.ax[0].get_ylim()
    check(f"{tag}: Full scale reaches just above {mx:,.0f}", mx <= hi <= mx * 1.05 and lo <= 0, (lo, hi))
    check(f"{tag}: saturation line {w._sat():,.0f} in view", lo < w._sat() < hi)
    pump(1.0)
    check(f"{tag}: still at full scale while scans arrive", abs(w.c_spec.ax[0].get_ylim()[1] - hi) < 1e-6)
    w.cb_spec_cur.setChecked(True); w.c_spec.draw()
    a0 = w.c_spec.ax[0]; px, py = a0.transData.transform((650.0, (lo + hi) / 2))
    MouseEvent("button_press_event", w.c_spec, px, py, button=1)._process()
    MouseEvent("button_release_event", w.c_spec, px, py, button=1)._process()
    i = int(np.argmin(np.abs(w.axis - 650.0)))
    pump(0.5)
    txt = w.spec_cur_info.text()
    check(f"{tag}: cursor reads pixel {i} with a live value", f"(pixel {i}), live " in txt and "counts" in txt, txt)
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True); w.grab().save(str(SHOTS / f"{tag.replace(' ', '_')}.png"))
    w.cb_spec_cur.setChecked(False)


first = w.info.get("model")
full_scale_and_cursor(first)
other = real[0] if len(real) > 1 else devices.SIM_ID
want = other.split(":", 1)[1] if isinstance(other, str) else "SIM0001"
k = ids.index(other); w.dev_combo.setCurrentIndex(k); w._device_picked(k)
pump(20, until=lambda: w.state == "PREVIEW" and w.info.get("serial") == want and w.latest is not None
     and len(w.latest) == int(w.info.get("pixels", -1)))
check(f"picking {w.dev_combo.itemText(k).split('  ')[0]} in the list switched to it, live again", w.state == "PREVIEW" and w.info.get("serial") == want,
      (w.state, w.info.get("serial")))
w.cb_auto.setChecked(True)
if isinstance(other, str):
    full_scale_and_cursor(w.info.get("model"))
k = ids.index(real[-1]); w.dev_combo.setCurrentIndex(k); w._device_picked(k)
pump(20, until=lambda: w.state == "PREVIEW" and w.info.get("serial") == last and w.latest is not None and len(w.latest) == int(w.info.get("pixels", -1)))
check("and back, live again", w.state == "PREVIEW" and w.info.get("serial") == last, (w.state, w.info.get("serial")))

w.engine.send("disconnect"); pump(5, until=lambda: w.state == "DISCONNECTED")
w._auto = False; w.close(); pump(1)
print("PASS" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
