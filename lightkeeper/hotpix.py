"""Hot and cold pixels, found from the individual scans of a dark set.

A pixel is flagged when, in at least MIN_FRACTION of the scans at the longest integration time, it sits more than K
robust sigma above (hot) or below (cold) a 15-pixel running median of that scan. Random noise rarely repeats across
scans, so the persistence test keeps the list to pixels that are a property of the detector.

The list is display and analysis metadata only: raw spectra are never altered. The latest list for each spectrometer is
kept in app_home()/detector/<serial>.json so the live view can use it outside a run.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
from scipy.ndimage import median_filter

from .procs import app_home, read_json, write_json_atomic

K = 5.0               # robust sigma above / below the running median
MIN_FRACTION = 0.8    # of the scans at the longest integration time
WINDOW = 15           # running-median width, pixels
METHOD = (f"per scan at the longest dark integration: excess over a {WINDOW}-pixel running median beyond K robust sigma "
          f"(1.4826 MAD of the excess) in at least MIN_FRACTION of the scans")


def find(scans, k: float = K, min_fraction: float = MIN_FRACTION) -> tuple[list[int], list[int]]:
    """(hot, cold) pixel indices from dark scans taken at one integration time (array n_scans x n_pixels)."""
    s = np.atleast_2d(np.asarray(scans, float))
    ex = s - median_filter(s, size=(1, WINDOW), mode="nearest")
    sig = 1.4826 * np.median(np.abs(ex - np.median(ex, 1, keepdims=True)), 1, keepdims=True)
    sig[sig <= 0] = np.inf
    hot = (ex > k * sig).mean(0) >= min_fraction
    cold = (ex < -k * sig).mean(0) >= min_fraction
    return np.flatnonzero(hot).tolist(), np.flatnonzero(cold).tolist()


def fill(y, valid):
    """Copy of y (1-D, or 2-D with pixels along the last axis) with invalid pixels linearly interpolated from valid ones."""
    y = np.array(y, float, copy=True)
    valid = np.asarray(valid, bool)
    if valid.all() or not valid.any():
        return y
    idx = np.arange(valid.size); good = idx[valid]; bad = idx[~valid]
    if y.ndim == 1:
        y[bad] = np.interp(bad, good, y[good])
    else:
        for row in y.reshape(-1, valid.size):
            row[bad] = np.interp(bad, good, row[good])
    return y


def _path(serial: str):
    d = app_home() / "detector"; d.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(serial)) or "unknown"
    return d / f"{safe}.json"


def save(serial: str, entry: dict) -> None:
    """Remember the latest list for this spectrometer (never raises)."""
    try:
        write_json_atomic(_path(serial), dict(entry, serial=str(serial), saved_local=dt.datetime.now().astimezone().isoformat()))
    except Exception:
        pass


def load(serial: str) -> dict | None:
    return read_json(_path(serial)) if serial else None
