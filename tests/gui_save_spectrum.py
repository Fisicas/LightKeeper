"""GUI check (0.3.6, header 0.3.7): Save spectrum writes the displayed scan as a two-column .dpt (with # detail lines) plus a .json, named
from the run label or the scan time. Off-screen, simulator only, temporary LIGHTKEEPER_HOME. Run:
   set QT_QPA_PLATFORM=offscreen & python -X utf8 tests\\gui_save_spectrum.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="lk_save_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_HOME"] = str(HOME)
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"          # never open a real spectrometer that happens to be plugged in
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
from PySide6 import QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402
from lightkeeper.devices import SIM_ID  # noqa: E402

SIM_HOT = [202, 449, 1646]
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


w = ui.Window(auto_confirm=True); w.show()
w.out_edit.setText(str(HOME / "field_runs"))
w.engine.send("connect", SIM_ID, True, True); pump(5, lambda: w.state == "IDLE")
w.exp_mode.setCurrentIndex(1); w.int_spin.setValue(100); w._toggle_preview(); pump(2)
w.max_spin.setValue(200); w.btn_dark.click(); pump(15, lambda: w.hot is not None and w.state == "PREVIEW"); pump(1)
w.cb_dark.setChecked(True); pump(0.5)
w.label_edit.setText("Hg Ar lamp"); w.btn_save_spec.click()
w.label_edit.setText(""); w.btn_save_spec.click(); w.btn_save_spec.click()

files = sorted((HOME / "spectra").glob("*.dpt"))
check("three .dpt files, each with a .json", len(files) == 3 and all(f.with_suffix(".json").exists() for f in files))
check("the run label names the file", any(f.name.startswith("Hg_Ar_lamp_") for f in files))
check("the scan time names the file when there is no label", any(f.name[:4].isdigit() for f in files))
check("a second save in the same second gets _2", any(f.stem.endswith("_2") for f in files))
f = next(f for f in files if f.name.startswith("Hg_Ar"))
lines = f.read_text(encoding="utf-8").splitlines()
check("the .dpt starts with # detail lines", lines[0].startswith("# LightKeeper") and any(l.startswith("# scan ") for l in lines))
rows = [line.split(",") for line in lines if not line.startswith("#")]
check("only two numeric columns, one row per pixel", len(rows) == 2068 and all(len(r) == 2 for r in rows)
      and np.isfinite(np.array(rows, float)).all())
m = json.loads(f.with_suffix(".json").read_text())
check("the .json holds the details", m["integration_ms"] == 100 and m["dark_subtracted"] and m["device"]["serial"] == "SIM0001"
      and m["hot_cold_pixels_bridged"] == SIM_HOT and len(m["raw_counts"]) == 2068)
y = np.loadtxt(f, delimiter=",", comments="#")[:, 1]
check("hot pixels are bridged in the saved spectrum", max(abs(y[i] - (y[i - 1] + y[i + 1]) / 2) for i in SIM_HOT) < 300)
w.engine.send("quit"); pump(1)
print(f"\n{sum(results)}/{len(results)} checks passed; temp {HOME}")
sys.exit(0 if all(results) else 1)
