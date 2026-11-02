"""Spectrometer access: any spectrometer the OceanDirect SDK can open (USB), plus a synthetic simulator.

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
_SDK = None


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


def list_devices(include_sim: bool = True):
    """[(id, label)], error string or None. Do not call while a device is open."""
    out, err = [], None
    try:
        api, _ = sdk()
        api.find_usb_devices()
        for i in api.get_device_ids():
            try:
                s = str(api.get_serial_number(i))
            except Exception:
                s = ""
            out.append((int(i), f"Ocean device id {i}" + (f"  [{s}]" if s else "  [serial read after connect]")))
    except Exception as e:  # SDK missing or USB error: the simulator still works
        err = repr(e)
    if include_sim:
        out.append((SIM_ID, "Simulator (synthetic spectra, no hardware)"))
    return out, err


def open_by_serial(serial: str, edc: bool = True, nlc: bool = True, hint_id: int | None = None):
    """Open the spectrometer with this serial number (device ids are per-process, serials are not).
    'SIM...' opens the simulator. Re-enumerates USB, so it also works after the device was unplugged and replugged.
    Raises RuntimeError if no attached device has that serial."""
    if not serial or str(serial).upper().startswith("SIM"):
        return SimDevice()
    api, _ = sdk()
    api.find_usb_devices()
    ids = [int(i) for i in api.get_device_ids()]
    if hint_id in ids:
        ids.remove(hint_id); ids.insert(0, hint_id)
    seen = []
    for i in ids:
        try:
            d = OceanDevice(i, edc, nlc)
        except Exception as e:  # held by another program, or a transient USB error
            seen.append(f"id {i}: {e!r}"); continue
        if d.serial == str(serial):
            return d
        seen.append(f"id {i}: serial {d.serial}")
        d.close()
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
                    nonlinearity_coeffs=self.nlc_coeffs)

    def close(self):
        _try(lambda: self.api.close_device(self.device_id))


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
                    electric_dark_correction=self.edc, nonlinearity_correction=self.nlc, scans_to_average_device=1, boxcar=0)

    def close(self):
        pass
