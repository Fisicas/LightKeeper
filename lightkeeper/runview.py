"""Read a recorded run back for viewing, and find the usable part of the day.

load_run() opens the run's HDF5 files READ-ONLY (it never writes to a run folder) and returns per-slot band metrics and a
subsample of dark-corrected spectra in the same shape the window builds from live slots.

usable_window() finds where the light becomes meaningful: the 420-700 nm dark-corrected rate must stay above
USABLE_FRACTION of the run's bright level (its 99th percentile) for USABLE_HOLD_S. Before dawn and after dusk the
spectra are dark noise and drift, which swamps the waterfall and the ratios. On the 1 Nov 2026 forest-edge run this
gives 07:38-17:29 local time.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import h5py
import numpy as np

from .storage import local_dt

USABLE_FRACTION = 0.01
USABLE_HOLD_S = 300.0
MAX_SPECTRA = 1500
CHUNK = 2000


def band_masks(wl, valid=None):
    v = np.ones(len(wl), bool) if valid is None else np.asarray(valid, bool)
    b = lambda a, c: (wl >= a) & (wl <= c) & v
    return dict(par=b(420, 700), red=b(655, 665), fr=b(725, 735), nir=b(780, 820), red2=b(665, 680),
                o2=b(759.8, 761.0), sh1=b(753.5, 756.0), sh2=b(769.5, 772.0))


def band_metrics(rate, masks):
    """rate: (n, pixels) dark-corrected counts/s -> dict of (n,) arrays, as the window computes per live slot."""
    m = {k: np.nanmean(rate[:, v], 1) if v.any() else np.full(len(rate), np.nan) for k, v in masks.items()}
    with np.errstate(divide="ignore", invalid="ignore"):
        sh = 0.5 * (m["sh1"] + m["sh2"])
        return dict(par=m["par"], rfr=m["red"] / m["fr"], nirred=m["nir"] / m["red2"], o2a=np.where(sh != 0, 1 - m["o2"] / sh, np.nan))


def run_darks(files) -> tuple[dict, str]:
    """Mean dark per integration time from the run's pre-run dark set (else its last dark set)."""
    rows = []
    for f in files:
        with h5py.File(f, "r") as h:
            if "dark_scans" in h:
                g = h["dark_scans"]
                lab = [x.decode() if isinstance(x, bytes) else str(x) for x in g["label"][:]]
                rows += list(zip(g["session"][:], lab, g["int_us"][:], g["spectrum"][:]))
    if not rows:
        return {}, "none"
    sessions = sorted({(int(s), l) for s, l, _, _ in rows})
    pre = [s for s, l in sessions if l == "pre"]
    sid = pre[0] if pre else sessions[-1][0]
    acc: dict[int, list] = {}
    for s, _, u, y in rows:
        if int(s) == sid:
            acc.setdefault(int(u), []).append(y)
    label = next(l for s, l in sessions if s == sid)
    return {u: np.mean(v, 0) for u, v in acc.items()}, f"{label}-run dark set (session {sid})"


def _dark_for(darks, us, n_px):
    if not darks:
        return np.zeros(n_px)
    return darks.get(int(us), darks[min(darks, key=lambda k: abs(k - us))])


def load_run(run_dir, valid=None, progress=None) -> dict:
    """Everything the Waterfall and Time series tabs need from a recorded run. Read-only."""
    run_dir = Path(run_dir)
    files = sorted(run_dir.glob("spectra_*.h5"))
    if not files:
        raise FileNotFoundError(f"no spectra_*.h5 files in {run_dir}")
    darks, dark_src = run_darks(files)
    sizes = []
    wl = None; hot = []
    for f in files:
        with h5py.File(f, "r") as h:
            sizes.append(h["blocks/mean"].shape[0] if "blocks" in h else 0)
            if wl is None and "wavelength_nm_device" in h:
                wl = h["wavelength_nm_device"][:]
            if not hot and "hot_pixels" in h.attrs:
                try:
                    import json
                    hot = json.loads(h.attrs["hot_pixels"]) + json.loads(h.attrs.get("cold_pixels", "[]"))
                except Exception:
                    hot = []
    total = sum(sizes)
    if total == 0 or wl is None:
        raise ValueError(f"{run_dir.name} has no recorded slots")
    if valid is None or len(valid) != len(wl):
        valid = np.ones(len(wl), bool)
        if hot:
            valid[[i for i in hot if 0 <= i < len(wl)]] = False
    masks = band_masks(wl, valid)
    stride = max(1, math.ceil(total / MAX_SPECTRA))
    slots, spectra, done, k = [], [], 0, 0
    for f, n in zip(files, sizes):
        if not n:
            continue
        with h5py.File(f, "r") as h:
            b = h["blocks"]
            it = b["int_us"][:]; t = b["t_wall_ns"][:]; ns = b["n_scans"][:]
            pk = b["max_raw_window"][:] if "max_raw_window" in b else np.full(n, np.nan)
            sat = b["n_saturated_scans"][:] if "n_saturated_scans" in b else np.zeros(n)
            for a in range(0, n, CHUNK):
                z = slice(a, min(n, a + CHUNK))
                mean = b["mean"][z].astype(np.float64)
                d = np.stack([_dark_for(darks, u, mean.shape[1]) for u in it[z]])
                rate = (mean - d) / (it[z][:, None] / 1e6)
                met = band_metrics(rate, masks)
                for j in range(mean.shape[0]):
                    i = a + j; tl = local_dt(int(t[i]))
                    slots.append(dict(t=tl, int_ms=it[i] / 1000, n=int(ns[i]), peak=float(pk[i]), nsat=int(sat[i]),
                                      par=float(met["par"][j]), rfr=float(met["rfr"][j]), nirred=float(met["nirred"][j]),
                                      o2a=float(met["o2a"][j])))
                    if k % stride == 0:
                        spectra.append((tl, rate[j].astype(np.float32)))
                    k += 1
                done += mean.shape[0]
                if progress:
                    progress(done / total)
    return dict(name=run_dir.name, dir=str(run_dir), slots=slots, spectra=spectra, stride=stride, wl=wl, valid=valid,
                darks=darks, dark_source=dark_src, files=[f.name for f in files], n_slots=total)


def usable_window(times, par, fraction=USABLE_FRACTION, hold_s=USABLE_HOLD_S):
    """(first, last) local time of usable daylight, or None. times: datetimes; par: 420-700 nm dark-corrected rate."""
    par = np.asarray(par, float)
    if len(par) < 3:
        return None
    ok = np.isfinite(par)
    if not ok.any():
        return None
    ref = np.nanpercentile(par, 99)
    if not np.isfinite(ref) or ref <= 0:
        return None
    above = (np.where(ok, par, -np.inf) > fraction * ref).astype(float)
    ts = np.array([x.timestamp() for x in times])
    step = float(np.median(np.diff(ts))) if len(ts) > 1 else 1.0
    win = max(1, int(round(hold_s / max(step, 1e-3))))
    if win > 1:
        sus = np.convolve(above, np.ones(win) / win, "same") > 0.9
    else:
        sus = above > 0
    idx = np.flatnonzero(sus)
    if not len(idx):
        return None
    return times[int(idx[0])], times[int(idx[-1])]
