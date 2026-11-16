"""Hardware check (0.4.0): every attached spectrometer is found, by OceanDirect or seabreeze, and each one connects, gives live
scans on its own count scale, disconnects, and reopens by serial (the background recorder's path). No run is recorded.
Also leaves an unread reply on a Maya2000 Pro and checks it is recovered (0.4.1). Close OceanView first. Usage: python -X utf8 tests/hw_all_spectrometers.py"""
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

# 0.4.1: a reply left unread on an OceanDirect spectrometer made OceanDirect fail every open (OSError 0xe06d7363) until the
# device was unplugged. Leave one on purpose, in another process, and check that a new process finds and opens it.
import subprocess  # noqa: E402
wedge = r'''
import sys; sys.path.insert(0, sys.argv[1])
from lightkeeper import devices as D
D.seabreeze_backend()
import usb.core, usb.util, libusb_package
for d in usb.core.find(find_all=True, idVendor=0x2457, idProduct=int(sys.argv[2], 16), backend=libusb_package.get_libusb1_backend()):
    i = d.get_active_configuration()[(0, 0)]; usb.util.claim_interface(d, i.bInterfaceNumber)
    d.write(0x01, bytes([0x05, 0x00]), timeout=500)        # query information slot 0; the reply is never read
    usb.util.release_interface(d, i.bInterfaceNumber); usb.util.dispose_resources(d)
'''
heal = r'''
import sys; sys.path.insert(0, sys.argv[1])
from lightkeeper import devices as D
D.seabreeze_backend()
devs, err = D.list_devices(False)
print([i for i, _ in devs], err)
'''
root = str(Path(__file__).resolve().parents[1])
for dev_id, lab in devs:
    if "Maya2000 Pro" not in lab:
        continue
    subprocess.run([sys.executable, "-X", "utf8", "-c", wedge, root, "102A"], check=True, timeout=60)
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", heal, root], capture_output=True, text=True, timeout=120)
    check("a Maya left with an unread reply is found and named again", dev_id in r.stdout, r.stdout.strip() or r.stderr[-300:])
print("PASS" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
