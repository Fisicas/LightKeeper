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


def summarize(run_dir, meta: dict) -> dict:
    """run_summary.json: one file that answers "was this a good run?". Reads the run's own files after they are closed."""
    from .schedule import solar_elevation
    run_dir = Path(run_dir)
    files = sorted(run_dir.glob("spectra_*.h5"))
    it, tw, nsat, temp, dk = [], [], [], [], {}
    for f in files:
        with h5py.File(f, "r") as h:
            if "blocks" in h:
                b = h["blocks"]
                it.append(b["int_us"][:]); tw.append(b["t_wall_ns"][:]); nsat.append(b["n_saturated_scans"][:])
                if "temperature_c" in b:
                    temp.append(b["temperature_c"][:])
            if "dark_scans" in h:
                g = h["dark_scans"]
                lab = [x.decode() if isinstance(x, bytes) else str(x) for x in g["label"][:]]
                for l, u, y in zip(lab, g["int_us"][:], g["spectrum"][:]):
                    dk.setdefault(l, {}).setdefault(int(u), []).append(y)
    it = np.concatenate(it) if it else np.zeros(0, int); tw = np.concatenate(tw) if tw else np.zeros(0, np.int64)
    nsat = np.concatenate(nsat) if nsat else np.zeros(0)
    loc = lambda ns: local_dt(int(ns)).isoformat(timespec="seconds")  # noqa: E731
    changes = [dict(local=loc(tw[i]), from_ms=float(it[i - 1]) / 1000, to_ms=float(it[i]) / 1000)
               for i in np.flatnonzero(np.diff(it) != 0) + 1]
    agree = []
    for u in sorted(set(dk.get("pre", {})) & set(dk.get("post", {}))):
        pre, post = np.mean(dk["pre"][u], 0), np.mean(dk["post"][u], 0)
        d = float(np.median(post - pre))
        agree.append(dict(int_ms=u / 1000, median_post_minus_pre_counts=round(d, 1),
                          relative_percent=round(100 * d / float(np.median(pre)), 2) if np.median(pre) else None))
    plan = meta.get("plan", {}); lat, lon = float(plan.get("site_lat", 0)), float(plan.get("site_lon", 0))

    def with_sun(t):
        el = float(solar_elevation(np.array([np.datetime64(t.astimezone(dt.timezone.utc).replace(tzinfo=None), "ns")]), lat, lon)[0])
        return dict(local=t.isoformat(timespec="seconds"), sun_elevation_deg=round(el, 2))
    usable, above100 = {}, {}
    if len(it):
        v = load_run(run_dir)
        times = [s["t"] for s in v["slots"]]; par = np.array([s["par"] for s in v["slots"]])
        w = usable_window(times, par)
        if w:
            usable = dict(rule=f"420-700 nm dark-corrected rate above {USABLE_FRACTION:.0%} of the run's 99th percentile "
                               f"for {USABLE_HOLD_S / 60:g} min", first_local=with_sun(w[0])["local"],
                          first_sun_elevation_deg=with_sun(w[0])["sun_elevation_deg"], last_local=with_sun(w[1])["local"],
                          last_sun_elevation_deg=with_sun(w[1])["sun_elevation_deg"])
        a = np.flatnonzero(par > 100)
        if len(a):
            above100 = dict(rule="420-700 nm dark-corrected rate above 100 counts/s", first=with_sun(times[a[0]]), last=with_sun(times[a[-1]]))
    clocks = [c["offset_s"] for c in meta.get("clock_checks", []) if c.get("ok")]
    temp = np.concatenate(temp) if temp else np.zeros(0)
    temp = temp[np.isfinite(temp)]
    import csv
    import re
    rows = list(csv.reader((run_dir / "events.csv").open(encoding="utf-8"))) if (run_dir / "events.csv").exists() else []
    skipped = sum(1 for r in rows if len(r) > 2 and r[2] == "burst_skipped")
    bursts = meta.get("bursts_done") or []
    if not bursts:                  # runs before 0.3.8: the burst_start/burst_end events carry the same numbers
        start_ms = {}
        for r in rows:
            if len(r) > 3 and r[2] == "burst_start" and (m := re.match(r"#(\d+), ([\d.]+) ms", r[3])):
                start_ms[m.group(1)] = float(m.group(2))
            if len(r) > 3 and r[2] == "burst_end" and (m := re.match(r"#(\d+), (\d+) scans \(([\d.]+) per s\)", r[3])):
                bursts.append(dict(id=int(m.group(1)), int_ms=start_ms.get(m.group(1)), scans=int(m.group(2)),
                                   scans_per_s=float(m.group(3))))
    return dict(
        app=meta.get("app"), run=run_dir.name, written_local=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        recording=dict(start_local=meta.get("resolved_start_local") or meta.get("planned_start_local"),
                       end_local=meta.get("recording_end_local"), segments=1 + len(meta.get("resumes", []))),
        slots=dict(recorded=int(len(it)), with_saturation=int((nsat > 0).sum())),
        exposure=dict(mode="automatic" if plan.get("auto_exposure") else "fixed",
                      integration_times_ms=sorted({float(u) / 1000 for u in np.unique(it)}) if len(it) else []),
        integration_changes=changes,
        bursts=dict(done=len(bursts), skipped=skipped, int_ms=sorted({b["int_ms"] for b in bursts if b.get("int_ms") is not None}),
                    scans_per_s_min=min((b["scans_per_s"] for b in bursts), default=None),
                    scans_per_s_max=max((b["scans_per_s"] for b in bursts), default=None)),
        dark_sets=dict(labels=sorted(dk), integration_times_ms=sorted({u / 1000 for l in dk.values() for u in l}),
                       pre_post_agreement=agree, post_darks_delay_min=meta.get("post_darks_delay_min")),
        usable_light=usable, signal_above_100_counts_per_s=above100,
        clock_offset_s=dict(min=min(clocks), max=max(clocks), checks=len(clocks)) if clocks else {},
        temperature_c=dict(source=meta.get("temperature_source"), min=round(float(temp.min()), 2), max=round(float(temp.max()), 2),
                           mean=round(float(temp.mean()), 2)) if len(temp) else dict(source=meta.get("temperature_source"), logged=False),
    )
