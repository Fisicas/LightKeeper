"""Spectrometer access: any Ocean spectrometer on USB, through OceanDirect or seabreeze, plus a synthetic simulator.

Two backends, so that one installation finds whichever spectrometer is plugged in:
* OceanDirect SDK (Ocean Insight's current SDK): Maya2000 Pro, Flame, Ocean ST/SR/HR and the other models it supports;
* seabreeze (pyseabreeze over libusb, the existing WinUSB driver): the legacy models OceanDirect does not open, such as
  the USB650, USB2000(+), USB4000 and HR2000. OceanDirect is tried first; seabreeze adds only spectrometers whose serial
  OceanDirect did not list. If seabreeze is not installed, the window installs it once into its own folder
  (%LOCALAPPDATA%/LightKeeper/deps, never the Python environment); without it, OceanDirect devices still work.
Counts keep each detector's native scale (USB650: 12 bit, 0-4095); the saturation and exposure thresholds are scaled
to it (levels()).

Rules:
* device-side scans-to-average = 1 and boxcar = 0 (averaging is done by LightKeeper so per-slot SD can be kept);
* a Maya2000 Pro is held at an 8 ms floor although the SDK reports 7.2 ms (unreliable below 8 ms);
* electric-dark and nonlinearity corrections are requested on; what the device actually reports is recorded
  (None = not supported by that model).
Only one program can hold a USB spectrometer: close OceanView first.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np

SIM_ID = -1
MAYA_FLOOR_US = 8000
SB_PREFIX = "sb:"                 # device id of a seabreeze spectrometer: "sb:<serial>"
OD_PREFIX = "od:"                 # an OceanDirect one: "od:<serial>" (its integer ids change at every USB rescan)
_SDK = None
_SB = None                        # loaded seabreeze pieces, or the error that stopped them
SEABREEZE_PINS = ("seabreeze==2.11.0", "pyusb==1.3.1", "libusb-package==1.0.30.1", "importlib-resources==7.1.0")
# 16-bit thresholds (Maya2000 Pro, max intensity 64000) are kept exactly; a detector with a smaller range is scaled from 65535
BASE_LEVELS = dict(sat=60000.0, step_down=55000.0, target_high=40000.0, low_p995=15000.0)


def levels(max_counts=None) -> dict:
    """Saturation and exposure thresholds for a detector whose largest count is max_counts.
    16-bit detectors use the values LightKeeper has always used; a 12-bit USB650 (4095) gets 3749 / 3437 / 2500 / 937."""
    k = 1.0 if not max_counts or float(max_counts) >= 60000 else float(max_counts) / 65535.0
    return {n: round(v * k, 1) for n, v in BASE_LEVELS.items()}


def deps_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LightKeeper" / "deps"


def _sb_import():
    """Import seabreeze (pyseabreeze over libusb_package) from the environment or from deps_dir()."""
    d = deps_dir()
    if d.is_dir() and str(d) not in sys.path:
        sys.path.append(str(d))
    import libusb_package
    import seabreeze
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(Path(libusb_package.__file__).parent))
        except OSError:
            pass
    seabreeze.use("pyseabreeze", pyusb_backend="libusb1")
    from seabreeze.pyseabreeze.transport import USBTransport, _pyusb_backend_instances
    _pyusb_backend_instances["libusb1"] = libusb_package.get_libusb1_backend()
    from seabreeze.pyseabreeze.api import _seabreeze_device_factory
    from seabreeze.spectrometers import Spectrometer
    return dict(version=seabreeze.__version__, transport=USBTransport, factory=_seabreeze_device_factory, Spectrometer=Spectrometer)


def seabreeze_backend():
    """The seabreeze pieces, or None (seabreeze_error() says why)."""
    global _SB
    if _SB is None:
        try:
            _SB = _sb_import()
        except Exception as e:
            _SB = e
    return None if isinstance(_SB, Exception) else _SB


def seabreeze_error():
    return repr(_SB) if isinstance(_SB, Exception) else None


def ensure_seabreeze(install: bool = True, log=None) -> bool:
    """Make seabreeze importable. With install=True and no copy anywhere, pip installs the pinned packages once into
    deps_dir() (--no-deps: NumPy is already there; the Python environment itself is not changed).
    Returns True if seabreeze can be used. Never raises."""
    global _SB
    if seabreeze_backend() is not None:
        return True
    if not install or os.environ.get("LIGHTKEEPER_NO_INSTALL") == "1":
        return False
    import subprocess
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        exe = exe.parent / "python.exe"
    d = deps_dir()
    cmd = [str(exe), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-deps",
           "--target", str(d), "--upgrade", *SEABREEZE_PINS]
    try:
        d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (d / "install_log.txt").write_text(" ".join(cmd) + "\n\n" + r.stdout + "\n" + r.stderr, encoding="utf-8")
        if log:
            log(f"seabreeze install into {d}: exit {r.returncode}")
    except Exception as e:
        if log:
            log(f"seabreeze install failed: {e!r}")
        return False
    _SB = None
    return seabreeze_backend() is not None


def sdk():
    """(OceanDirectAPI singleton, SDK root)."""
    global _SDK
    if _SDK is None:
        root = Path(os.environ.get("OCEANDIRECT_HOME", r"C:\Program Files\Ocean Optics\OceanDirect SDK"))
        py, lib = root / "Python", root / "lib"
        if py.is_dir() and str(py) not in sys.path:
            sys.path.insert(0, str(py))
        if hasattr(os, "add_dll_directory") and lib.is_dir():
            os.add_dll_directory(str(lib))
        from oceandirect.OceanDirectAPI import OceanDirectAPI
        _SDK = (OceanDirectAPI(), root)
    return _SDK


def shutdown_sdk():
    global _SDK
    if _SDK is not None:
        try:
            _SDK[0].shutdown()
        except Exception:
            pass
        _SDK = None


OCEAN_VID = 0x2457


def drain_usb(log=None) -> int:
    """0.4.1: read and discard whatever an Ocean spectrometer still has queued on its input pipes. A reply left unread
    (a program stopped mid-command, or a probe) makes OceanDirect 3.1.4 fail every later open with a C++ exception
    (OSError 0xe06d7363); on 15 Nov 2026 a Maya2000 Pro held 3 bytes on endpoint 0x81. It must run BEFORE OceanDirect
    touches the device in this process: after one failed open OceanDirect keeps the device until the process exits
    (tested: draining or restarting the SDK afterwards does not help). Devices another program holds, or this process has
    open, are skipped (their interface cannot be claimed). Needs pyusb (seabreeze's); returns the bytes discarded."""
    if seabreeze_backend() is None:
        return 0
    import libusb_package
    import usb.core
    import usb.util
    total = 0
    for d in usb.core.find(find_all=True, idVendor=OCEAN_VID, backend=libusb_package.get_libusb1_backend()) or ():
        claimed = None
        try:
            intf = d.get_active_configuration()[(0, 0)]
            usb.util.claim_interface(d, intf.bInterfaceNumber); claimed = intf.bInterfaceNumber
            for ep in intf:
                if usb.util.endpoint_direction(ep.bEndpointAddress) != usb.util.ENDPOINT_IN:
                    continue
                for _ in range(64):
                    try:
                        n = len(ep.read(16384, timeout=25))
                    except usb.core.USBTimeoutError:
                        break
                    total += n
                    if log and n:
                        log(f"USB {d.idProduct:#06x} endpoint {ep.bEndpointAddress:#04x}: discarded {n} stale bytes")
        except Exception:
            pass
        finally:
            if claimed is not None:
                _try(lambda: usb.util.release_interface(d, claimed))
            _try(lambda: usb.util.dispose_resources(d))
    return total


def _od_open_names(api):
    out = []
    for i in api.get_device_ids():
        i = int(i); model = serial = err = ""
        try:
            d = api.open_device(i)
            model = str(_try(d.get_model, "")); serial = str(_try(d.get_serial_number, ""))
        except Exception as e:     # held by another program (another LightKeeper window, OceanView) or a USB fault
            err = repr(e)
        finally:
            _try(lambda: api.close_device(i))
        out.append((i, model, serial, err))
    return out


def _od_list():
    """[(id, model, serial, error)] of the spectrometers OceanDirect finds; each is opened briefly to read its name
    (OceanDirect 3.1 cannot read a serial number before the device is opened). Stale USB input is drained first."""
    drain_usb()
    api, _ = sdk()
    api.find_usb_devices()
    return _od_open_names(api)


def _model_key(name: str) -> str:
    """OceanDirect says 'MayaPro2000', seabreeze 'MAYA2000PRO': compare the letters and digits, in any order."""
    return "".join(sorted(c for c in str(name).upper() if c.isalnum()))


def _sb_list(skip_serials=(), skip_models=None):
    """[(id, model, serial)] of the spectrometers seabreeze can open over USB, except those already listed.
    0.4.1: a model OceanDirect already listed is not opened at all (as many of it as OceanDirect named): seabreeze's
    brief open is what may leave a stray reply that OceanDirect cannot get past."""
    sb = seabreeze_backend()
    if sb is None:
        return []
    skip = dict(skip_models or {})
    out = []
    for u in sb["transport"].list_devices():
        try:
            d = sb["factory"](u)
            key = _model_key(getattr(d, "_model_name", "") or "")
            if key and skip.get(key, 0) > 0:
                skip[key] -= 1
                continue
            d.open(); model, serial = str(d.model), str(d.serial_number); d.close()
        except Exception:          # in use (OceanDirect or another program holds it) or a USB error
            continue
        if serial and serial not in skip_serials:
            out.append((SB_PREFIX + serial, model, serial))
    return out


def device_label(model: str, serial: str, backend: str) -> str:
    name = {"MAYAPRO2000": "Maya2000 Pro", "MAYA2000PRO": "Maya2000 Pro"}.get(model.upper().replace(" ", ""), model)
    return f"{name or 'Ocean spectrometer'}  [{serial or 'serial read after connect'}]  ({backend})"


def list_devices(include_sim: bool = True):
    """[(id, label)], error string or None. Do not call while a device is open.
    id: "od:<serial>" for OceanDirect (an int if its serial could not be read), "sb:<serial>" for seabreeze, SIM_ID for the
    simulator. Serials, not OceanDirect's integer ids: those change at every rescan, and seabreeze's search is one."""
    out, errs, seen, models = [], [], set(), {}
    try:
        for i, model, serial, e in _od_list():
            if serial:
                out.append((OD_PREFIX + serial, device_label(model, serial, "OceanDirect"))); seen.add(serial)
                models[_model_key(model)] = models.get(_model_key(model), 0) + 1
            else:                  # 0.4.1: listed (Connect can still try it) but never opened automatically
                out.append((i, f"Ocean device id {i}, could not be opened (in use by another program?)  (OceanDirect)"))
                errs.append(f"OceanDirect id {i} could not be opened: {e}")
    except Exception as e:  # SDK missing or USB error: seabreeze and the simulator still work
        errs.append(f"OceanDirect: {e!r}")
    try:
        for i, model, serial in _sb_list(seen, models):
            out.append((i, device_label(model, serial, "seabreeze")))
        if seabreeze_backend() is None:
            errs.append(f"seabreeze: {seabreeze_error()}")
    except Exception as e:
        errs.append(f"seabreeze: {e!r}")
    if include_sim:
        out.append((SIM_ID, "Simulator (synthetic spectra, no hardware)"))
    return out, ("; ".join(errs) or None)


def open_device(dev_id, edc: bool = True, nlc: bool = True):
    """Open a device by the id list_devices() gave."""
    if dev_id == SIM_ID:
        return SimDevice()
    if isinstance(dev_id, str) and dev_id.startswith(SB_PREFIX):
        return SeaBreezeDevice(dev_id[len(SB_PREFIX):], edc, nlc)
    if isinstance(dev_id, str) and dev_id.startswith(OD_PREFIX):
        return open_by_serial(dev_id[len(OD_PREFIX):], edc, nlc, backends=("OceanDirect",))
    return OceanDevice(int(dev_id), edc, nlc)


def open_by_serial(serial: str, edc: bool = True, nlc: bool = True, hint_id: int | None = None,
                   backends=("OceanDirect", "seabreeze")):
    """Open the spectrometer with this serial number (device ids are per-process, serials are not).
    'SIM...' opens the simulator. Re-enumerates USB, so it also works after the device was unplugged and replugged.
    Raises RuntimeError if no attached device has that serial."""
    if not serial or str(serial).upper().startswith("SIM"):
        return SimDevice()
    seen = []
    try:
        if "OceanDirect" not in backends:
            raise LookupError("not searched")
        if drain_usb():                 # 0.4.1: before OceanDirect touches anything (see drain_usb)
            seen.append("stale USB input discarded")
        api, _ = sdk()
        api.find_usb_devices()
        ids = [int(i) for i in api.get_device_ids()]
        if hint_id in ids:
            ids.remove(hint_id); ids.insert(0, hint_id)
        for i in ids:
            try:
                d = OceanDevice(i, edc, nlc)
            except Exception as e:  # held by another program, or a transient USB error
                seen.append(f"id {i}: {e!r}"); continue
            if d.serial == str(serial):
                return d
            seen.append(f"id {i}: serial {d.serial}")
            d.close()
    except Exception as e:
        seen.append(f"OceanDirect: {e!r}")
    if "seabreeze" not in backends:
        pass
    elif seabreeze_backend() is not None:
        try:
            return SeaBreezeDevice(str(serial), edc, nlc)
        except Exception as e:
            seen.append(f"seabreeze: {e!r}")
    else:
        seen.append(f"seabreeze: {seabreeze_error()}")
    raise RuntimeError(f"spectrometer {serial} not found ({'; '.join(seen) or 'no Ocean devices on USB'})")


def _try(f, default=None):
    try:
        return f()
    except Exception:
        return default


class OceanDevice:
    simulated = False

    def __init__(self, device_id: int, edc: bool = True, nlc: bool = True):
        self.api, self.sdk_root = sdk()
        self.device_id = device_id
        self.dev = self.api.open_device(device_id)
        self.serial = str(_try(self.dev.get_serial_number, ""))
        self.model = str(_try(self.dev.get_model, ""))
        self.wavelengths = np.asarray(self.dev.get_wavelengths(), dtype=np.float64)
        self.sdk_min_us = int(self.dev.get_minimum_integration_time())
        self.max_us = int(self.dev.get_maximum_integration_time())
        self.min_us = max(self.sdk_min_us, MAYA_FLOOR_US if "maya" in self.model.lower() else 0)
        self.api_version = _try(lambda: ".".join(map(str, self.api.get_api_version_numbers())), "unknown")
        _try(lambda: self.dev.set_scans_to_average(1))
        _try(lambda: self.dev.set_boxcar_width(0))
        self.set_corrections(edc, nlc)
        self.int_us = int(_try(self.dev.get_integration_time, 100000))
        self.mode = "sky"
        self.features = self._features()
        # the SDK reads a temperature only through a thermo-electric cooler's thermistor; the Maya2000 Pro has none
        # (OceanDirect release notes: "Removed temperature feature from MayaPro device")
        self.temperature_source = "TEC thermistor" if self.features.get("THERMOELECTRIC") else ""
        self.nlc_coeffs = _try(lambda: [float(x) for x in self.dev.Advanced.get_nonlinearity_coeffs()], None)
        self.max_counts = float(_try(self.dev.get_max_intensity, 65535.0) or 65535.0)
        self.levels = levels(self.max_counts)

    def _features(self) -> dict:
        """{FeatureID name: supported} as the SDK reports them for this device."""
        try:
            from oceandirect.OceanDirectAPI import FeatureID
        except Exception:
            return {}
        out = {}
        for f in FeatureID:
            out[f.name] = _try(lambda f=f: bool(self.dev.is_feature_id_enabled(f)), None)
        return out

    def temperature_c(self):
        """Detector temperature in degC, or None when the device has no readable sensor or the read fails."""
        if not self.temperature_source:
            return None
        return _try(lambda: float(self.dev.Advanced.get_tec_temperature_degrees_C()), None)

    def set_corrections(self, edc: bool, nlc: bool):
        _try(lambda: self.dev.set_electric_dark_correction_usage(bool(edc)))
        _try(lambda: self.dev.set_nonlinearity_correction_usage(bool(nlc)))
        self.edc = _try(lambda: bool(self.dev.get_electric_dark_correction_usage()), None)
        self.nlc = _try(lambda: bool(self.dev.get_nonlinearity_correction_usage()), None)

    def set_int(self, us: int) -> int:
        us = int(min(max(int(us), self.min_us), self.max_us))
        self.dev.set_integration_time(us)
        self.int_us = int(self.dev.get_integration_time())
        return self.int_us

    def scan(self) -> np.ndarray:
        return np.asarray(self.dev.get_formatted_spectrum(), dtype=np.float64)

    def info(self) -> dict:
        return dict(kind="OceanDirect", device_id=int(self.device_id), serial=self.serial, model=self.model,
                    sdk_root=str(self.sdk_root), api_version=self.api_version, pixels=int(len(self.wavelengths)),
                    wl_first=float(self.wavelengths[0]), wl_last=float(self.wavelengths[-1]),
                    sdk_min_int_us=self.sdk_min_us, min_int_us_used=self.min_us, max_int_us=self.max_us,
                    electric_dark_correction=self.edc, nonlinearity_correction=self.nlc,
                    scans_to_average_device=1, boxcar=0,
                    features_supported=sorted(k for k, v in self.features.items() if v),
                    temperature_source=self.temperature_source or "none (no readable sensor on this device)",
                    nonlinearity_coeffs=self.nlc_coeffs, native_max_counts=self.max_counts,
                    saturation_level_counts=self.levels["sat"])

    def close(self):
        _try(lambda: self.api.close_device(self.device_id))


class SeaBreezeDevice:
    """A spectrometer opened by seabreeze (pyseabreeze, libusb over the WinUSB driver), by serial number.
    USB650: raw 12-bit counts read with a packet-aligned buffer (the stock 4097-byte read left a short USB packet queued
    on a Windows laptop, 2026-11-10), integration 3 ms-60 s in whole ms. Other models use seabreeze's own read.
    Electric-dark and nonlinearity corrections are used only where seabreeze supports them; what was used is recorded."""
    simulated = False

    def __init__(self, serial: str, edc: bool = True, nlc: bool = True):
        sb = seabreeze_backend()
        if sb is None:
            raise RuntimeError(f"seabreeze is not available: {seabreeze_error()}")
        self.sb_version = sb["version"]
        self.spec = sb["Spectrometer"].from_serial_number(str(serial))
        try:
            self.serial = str(self.spec.serial_number); self.model = str(self.spec.model)
            self.device_id = SB_PREFIX + self.serial
            self.wavelengths = np.asarray(self.spec.wavelengths(), dtype=np.float64)
            self.sdk_min_us, sdk_max = map(int, self.spec.integration_time_micros_limits)
            self.legacy = self.model.upper() == "USB650"
            self.min_us = self.sdk_min_us
            self.max_us = min(sdk_max, 60_000_000)
            self.max_counts = float(_try(lambda: self.spec.max_intensity, 65535.0) or 65535.0)
            self.levels = levels(self.max_counts)
            self.int_us = 100_000
            self.mode = "sky"
            self.temperature_source = ""
            self.nlc_coeffs = None
            _try(lambda: self.spec.trigger_mode(0))
            self.set_corrections(edc, nlc)
            self.set_int(self.int_us)
            if self.legacy:
                self._drain()
        except Exception:
            _try(self.spec.close)
            raise

    def _proto(self):
        return self.spec._dev.f.spectrometer.protocol

    def _drain(self):
        """Read whole USB packets until the input is idle, including any trailer a previous program left."""
        import usb.core
        for _ in range(16):
            try:
                self._proto().transport.read(size=8192, timeout_ms=100, mode="high_speed")
            except usb.core.USBTimeoutError:
                return
        raise RuntimeError(f"{self.model} input did not become idle")

    def set_corrections(self, edc, nlc):
        f = self.spec._dev.f.spectrometer
        has_dark = bool(len(getattr(f, "_dark_pixel_indices", ()) or ())) and not self.legacy
        has_nlc = hasattr(self.spec._dev.f, "nonlinearity_coefficients") and not self.legacy
        self.edc = bool(edc) if has_dark else None
        self.nlc = bool(nlc) if has_nlc else None

    def set_int(self, us):
        us = int(min(max(int(us), self.min_us), self.max_us))
        if self.legacy:
            us = max(self.min_us, (us // 1000) * 1000)
        self.spec.integration_time_micros(us)
        self.int_us = us
        return us

    def scan(self) -> np.ndarray:
        if not self.legacy:
            return np.asarray(self.spec.intensities(correct_dark_counts=bool(self.edc), correct_nonlinearity=bool(self.nlc)),
                              dtype=np.float64)
        p = self._proto()
        p.send(0x09)
        raw = p.transport.read(size=8192, timeout_ms=int(self.int_us / 1000) + 2000, mode="high_speed")
        if len(raw) != 4097 or raw[-1] != 0x69:
            raise RuntimeError(f"invalid USB650 frame: {len(raw)} bytes, sync {bytes(raw[-1:])!r}")
        blocks = np.frombuffer(bytes(raw[:4096]), dtype=np.uint8).reshape(32, 128)
        y = (blocks[:, :64].astype(np.uint16) + ((blocks[:, 64:].astype(np.uint16) & 0x0F) << 8)).ravel().astype(np.float64)
        if y.shape != self.wavelengths.shape:
            raise RuntimeError("USB650 returned a spectrum of the wrong length")
        return y

    def temperature_c(self):
        return None

    def info(self) -> dict:
        return dict(kind="seabreeze", backend=f"pyseabreeze {self.sb_version}", device_id=self.device_id, serial=self.serial,
                    model=self.model, pixels=int(len(self.wavelengths)), sdk_root=str(deps_dir()), api_version=self.sb_version,
                    wl_first=float(self.wavelengths[0]), wl_last=float(self.wavelengths[-1]),
                    sdk_min_int_us=self.sdk_min_us, min_int_us_used=self.min_us, max_int_us=self.max_us,
                    electric_dark_correction=self.edc, nonlinearity_correction=self.nlc,
                    scans_to_average_device=1, boxcar=0, features_supported=["SPECTROMETER"],
                    temperature_source="none (no readable sensor on this device)", nonlinearity_coeffs=None,
                    native_max_counts=self.max_counts, saturation_level_counts=self.levels["sat"],
                    raw_read="packet-aligned 4097-byte frame (USB650)" if self.legacy else "seabreeze intensities()")

    def close(self):
        _try(self.spec.close)


class SimDevice:
    """Synthetic spectrometer for testing the whole application (NOT a hardware validation).
    mode 'sky': vegetation-like radiance with a ~4x 'cloud' modulation (90 s period) and 1 Hz flutter;
    mode 'dark': offset + hot pixels growing with integration time; mode 'lamp': emission lines (set by the engine)
    placed on a deliberately mis-calibrated axis (true = device + 0.30 nm + 2e-7 (p-1000)^2) so calibration has work."""
    simulated = True

    def __init__(self, *a, **k):
        p = np.arange(2068)
        self.pixels = p
        self.wavelengths = 199.997 + 0.4755 * p - 1.389e-5 * p ** 2 - 8.26e-11 * p ** 3
        self.true_wl = self.wavelengths + 0.30 + 2e-7 * (p - 1000.0) ** 2
        w = self.true_wl
        veg = 0.05 + 0.08 * np.exp(-((w - 550) / 40) ** 2) + 0.45 / (1 + np.exp(-(w - 715) / 12))
        sun = np.exp(-((w - 650) / 260) ** 2) * (w > 330)
        self.sky = veg * sun * (1 - 0.7 * np.exp(-((w - 760.4) / 0.9) ** 2)) * (1 - 0.2 * np.exp(-((w - 687.3) / 1.0) ** 2))
        self.hot = np.zeros(len(p)); self.hot[[202, 449, 1646]] = [4300, 4500, 4800]
        self.sdk_min_us, self.min_us, self.max_us = 7200, 8000, 65_000_000
        self.int_us = 100_000
        self.edc = self.nlc = True
        self.serial, self.model = "SIM0001", "Simulator"
        self.rng = np.random.default_rng(1)
        self.t0 = time.perf_counter()
        self.temperature_source = "simulated" if os.environ.get("LIGHTKEEPER_SIM_TEMPERATURE", "1") != "0" else ""
        self.mode = "sky"
        self.lamp_lines = None   # (wl array, rel array)
        self.max_counts = 65535.0
        self.levels = levels(self.max_counts)

    def set_corrections(self, edc, nlc):
        self.edc, self.nlc = bool(edc), bool(nlc)

    def set_int(self, us):
        self.int_us = int(min(max(int(us), self.min_us), self.max_us)); return self.int_us

    def scan(self):
        ti = self.int_us / 1e6
        time.sleep(ti)
        y = 900.0 * ti + self.hot * ti
        if self.mode == "sky":
            t = time.perf_counter() - self.t0
            lvl = 4.0e5 * (1.6 + 1.2 * math.sin(2 * math.pi * t / 90)) * (1 + 0.02 * math.sin(2 * math.pi * t))
            y = y + lvl * ti * self.sky
        elif self.mode == "lamp" and self.lamp_lines is not None:
            wl, rel = self.lamp_lines
            prof = np.zeros_like(self.true_wl)
            for l0, r in zip(wl, rel):
                prof += r * np.exp(-0.5 * ((self.true_wl - l0) / 0.55) ** 2)
            y = y + 6.0e5 * ti * prof
        y = y + self.rng.normal(0, 1, y.size) * np.sqrt(np.abs(y) + 25)
        return np.minimum(y, 61380.0)

    def temperature_c(self):
        if not self.temperature_source:
            return None
        return 24.0 + 1.5 * math.sin(2 * math.pi * (time.perf_counter() - self.t0) / 600)   # slow drift, degC

    def info(self):
        return dict(kind="SIMULATED", temperature_source=self.temperature_source or "none (no readable sensor on this device)", serial=self.serial, model=self.model, pixels=len(self.wavelengths),
                    sdk_min_int_us=self.sdk_min_us, min_int_us_used=self.min_us, max_int_us=self.max_us,
                    electric_dark_correction=self.edc, nonlinearity_correction=self.nlc, scans_to_average_device=1, boxcar=0,
                    native_max_counts=self.max_counts, saturation_level_counts=self.levels["sat"])

    def close(self):
        pass
