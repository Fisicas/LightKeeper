"""Hardware check (0.4.0): every attached spectrometer is found, by OceanDirect or seabreeze, and each one connects, gives live
scans on its own count scale, disconnects, and reopens by serial (the background recorder's path). No run is recorded.
Close OceanView first. Usage: python -X utf8 tests/hw_all_spectrometers.py"""
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LIGHTKEEPER_HOME", tempfile.mkdtemp(prefix="lk_hw_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402

from lightkeeper.devices import SIM_ID, ensure_seabreeze, list_devices, levels, seabreeze_error  # noqa: E402
from lightkeeper.engine import Engine  # noqa: E402

ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


t0 = time.time()
check("seabreeze available (installed once if missing)", ensure_seabreeze(install=True, log=print), seabreeze_error() or "")
print(f"  ({time.time() - t0:.1f} s)")
devs, err = list_devices(False)
print("devices:", devs, "| errors:", err)
check("at least one spectrometer found", len(devs) >= 1)
check("simulator not listed when include_sim=False", SIM_ID not in [i for i, _ in devs])
serials = [lab.split("[")[1].split("]")[0] for _, lab in devs]
check("each spectrometer listed once", len(serials) == len(set(serials)), serials)

engine = Engine(); engine.start()


def wait_for(kind, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            e = engine.evt.get(timeout=0.5)
        except queue.Empty:
            continue
        if e["type"] in ("error", "connect_failed") and kind != "any":
            raise AssertionError(e)
        if e["type"] == kind:
            return e
    raise TimeoutError(kind)


def disconnect():
    engine.send("disconnect")
    while wait_for("state")["state"] != "DISCONNECTED":
        pass


try:
    for dev_id, lab in devs:
        print(f"\n== {lab}")
        engine.send("connect", dev_id, True, True)
        info = wait_for("device")["info"]
        mx = float(info["native_max_counts"]); lv = levels(mx)
        print(f"  {info['model']} {info['serial']} {info['pixels']} px, max {mx:g}, saturation {info['saturation_level_counts']}, "
              f"edc {info['electric_dark_correction']}, nlc {info['nonlinearity_correction']}, min int {info['min_int_us_used']} us")
        check("saturation level scaled to the detector", info["saturation_level_counts"] == lv["sat"] and lv["sat"] < mx)
        check("engine uses this device's thresholds", engine.lv == lv, engine.lv)
        engine.send("preview", True, False, 20)
        ys = []
        for _ in range(8):
            ys.append(wait_for("scan")["y"])
        y = np.array(ys)
        check("8 live scans of the right length", y.shape == (8, info["pixels"]) and np.isfinite(y).all())
        check("counts within the detector's range", y.max() <= mx + 1, f"min {y.min():.0f} max {y.max():.0f}")
        engine.send("preview", False, False, None)
        disconnect()
        engine.send("connect_serial", info["serial"], True, True, dev_id if isinstance(dev_id, int) else None)
        info2 = wait_for("device")["info"]
        check("reopened by serial with the same backend", info2["serial"] == info["serial"] and info2["kind"] == info["kind"], info2["kind"])
        disconnect()
finally:
    engine.send("quit"); engine.join(timeout=15)
check("engine shut down", not engine.is_alive())
print("PASS" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
