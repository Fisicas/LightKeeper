"""Wavelength calibration from emission-line lamps against NIST ASD air wavelengths.

Reference data: lightkeeper/data/nist_asd/*.tsv, retrieved unmodified from the NIST Atomic Spectra Database
(see PROVENANCE.json; observed air wavelength used when listed, otherwise Ritz). NIST relative intensities are
source-dependent and NOT comparable between species; they are used only to rank/select candidate lines and to label.

Procedure (same logic as the Oct 2026 Hg/Ar recalibration that gave 0.072 nm rms with 35 lines):
1. dark-subtracted, averaged lamp spectrum; peaks found with prominence >= snr x robust noise; saturated peaks
   (>= 60,000 counts) rejected; sub-pixel peak = 3-point parabola (calibration defined on line PEAKS).
2. reference lines for the lamp: rel. intensity >= min_rel within species, inside the detector range, and isolated
   (no other candidate line of any listed species within isolation_nm -> blends excluded).
3. match on the current axis within tol 1.5 nm, fit polynomial pixel -> lambda (deg 3), re-match within 0.5 nm,
   iterative 3-sigma (MAD) clipping; leave-one-out rms reported.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data" / "nist_asd"
SAT = 60000.0
LAMPS = {
    "UVC germicidal (Hg + Ar fill)": ["Hg I", "Ar I"],
    "Hg-Ar calibration lamp": ["Hg I", "Ar I"],
    "Mercury (Hg)": ["Hg I"],
    "Neon (Ne)": ["Ne I"],
    "Argon (Ar)": ["Ar I"],
    "Neon + Argon": ["Ne I", "Ar I"],
    "Helium (He)": ["He I"],
    "Hydrogen (H, Balmer)": ["H I"],
    "Krypton (Kr)": ["Kr I"],
    "Xenon (Xe)": ["Xe I"],
    "Sodium (Na) - check only, too few isolated lines to calibrate alone": ["Na I"],
    "Cadmium (Cd)": ["Cd I"],
    "Zinc (Zn)": ["Zn I"],
}


def _num(x: str) -> float:
    m = re.match(r"^\s*[^\d\-+.]*([-+]?\d+\.?\d*)", (x or "").strip().strip('"').lstrip("=").strip('"'))
    return float(m.group(1)) if m else float("nan")


def load_species(sp: str) -> np.ndarray:
    """structured array (wl_air_nm, rel) for one NIST spectrum, rel = intens / max intens of that species."""
    path = DATA / (sp.replace(" ", "_") + ".tsv")
    rows = []
    with open(path, encoding="utf-8") as fh:
        rd = csv.DictReader(fh, delimiter="\t")
        for r in rd:
            obs = _num(r.get("obs_wl_air(nm)", "")); ritz = _num(r.get("ritz_wl_air(nm)", ""))
            wl = obs if np.isfinite(obs) else ritz
            it = _num(r.get("intens", ""))
            if np.isfinite(wl) and np.isfinite(it) and it > 0:
                rows.append((wl, it))
    a = np.array(rows, dtype=[("wl", "f8"), ("int", "f8")])
    out = np.zeros(len(a), dtype=[("wl", "f8"), ("rel", "f8"), ("species", "U8")])
    out["wl"], out["rel"], out["species"] = a["wl"], a["int"] / a["int"].max(), sp
    return out


def reference_lines(lamp: str, lo: float, hi: float, min_rel: float = 0.02, isolation_nm: float = 1.0) -> np.ndarray:
    cand = np.concatenate([load_species(s) for s in LAMPS[lamp]])
    cand = cand[(cand["wl"] >= lo) & (cand["wl"] <= hi)]
    strong = cand[cand["rel"] >= min_rel]
    keep = []
    for i, r in enumerate(strong):
        others = strong[np.arange(len(strong)) != i]
        if not np.any(np.abs(others["wl"] - r["wl"]) < isolation_nm):
            keep.append(i)
    return np.sort(strong[keep], order="wl")


def find_peaks_px(y: np.ndarray, snr: float = 15.0):
    from scipy.signal import find_peaks
    d = np.diff(y)
    noise = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)
    idx, prop = find_peaks(y, prominence=max(snr * noise, 1e-9), distance=2)
    idx = idx[(idx > 0) & (idx < len(y) - 1)]
    a, b, c = y[idx - 1], y[idx], y[idx + 1]
    den = a - 2 * b + c
    off = np.where(den != 0, 0.5 * (a - c) / den, 0.0)
    return idx + np.clip(off, -0.5, 0.5), b, noise


def fit_calibration(device_wl: np.ndarray, lamp_counts: np.ndarray, lamp: str, deg: int = 3, min_rel: float = 0.02,
                    isolation_nm: float = 1.0, snr: float = 15.0, raw_max: np.ndarray | None = None) -> dict:
    """lamp_counts: dark-subtracted mean. raw_max: per-pixel max of the raw scans (for saturation rejection)."""
    px = np.arange(len(device_wl), dtype=float)
    pk, height, noise = find_peaks_px(lamp_counts, snr)
    if raw_max is not None:
        sat = raw_max[np.round(pk).astype(int)] >= SAT
    else:
        sat = np.zeros(len(pk), bool)
    n_sat = int(sat.sum()); pk, height = pk[~sat], height[~sat]
    refs = reference_lines(lamp, float(device_wl[5]), float(device_wl[-5]), min_rel, isolation_nm)
    axis = lambda p, coef=None: np.interp(p, px, device_wl) if coef is None else np.polyval(coef, p)

    def match(coef, tol):
        lp = axis(pk, coef); used = set(); m = []
        for r in sorted(refs, key=lambda r: -r["rel"]):
            d = np.abs(lp - r["wl"]); d[list(used)] = np.inf
            j = int(np.argmin(d)) if len(d) else -1
            if j >= 0 and d[j] <= tol:
                used.add(j); m.append((r["wl"], r["species"], r["rel"], pk[j], height[j]))
        return m

    coef = None; m = []
    for tol in (1.5, 0.8, 0.5):
        m = match(coef, tol)
        if len(m) < deg + 3:
            return dict(ok=False, message=f"only {len(m)} lines matched within {tol} nm (need >= {deg + 3}); "
                                          f"{len(pk)} peaks found, {len(refs)} reference lines, {n_sat} saturated peaks rejected",
                        peaks_px=pk.tolist(), n_refs=len(refs))
        coef = np.polyfit([x[3] for x in m], [x[0] for x in m], deg)
    keep = np.ones(len(m), bool)
    for _ in range(5):
        P = np.array([x[3] for x in m]); L = np.array([x[0] for x in m])
        coef = np.polyfit(P[keep], L[keep], deg)
        res = L - np.polyval(coef, P)
        s = 1.4826 * np.median(np.abs(res[keep] - np.median(res[keep])))
        new = np.abs(res) <= max(3 * s, 0.05)
        if (new == keep).all():
            break
        keep = new
    P = np.array([x[3] for x in m]); L = np.array([x[0] for x in m]); res = L - np.polyval(coef, P)
    loo = []
    for i in np.where(keep)[0]:
        k2 = keep.copy(); k2[i] = False
        loo.append(L[i] - np.polyval(np.polyfit(P[k2], L[k2], deg), P[i]))
    lines = [dict(ref_nm=float(x[0]), species=str(x[1]), nist_rel=float(x[2]), pixel=float(x[3]), height=float(x[4]),
                  device_nm=float(np.interp(x[3], px, device_wl)), fit_nm=float(np.polyval(coef, x[3])),
                  resid_nm=float(res[i]), used=bool(keep[i])) for i, x in enumerate(m)]
    c = coef[::-1]
    return dict(ok=True, lamp=lamp, species=LAMPS[lamp], degree=deg, n_used=int(keep.sum()), n_matched=len(m),
                n_saturated_rejected=n_sat, rms_nm=float(np.sqrt(np.mean(res[keep] ** 2))), max_abs_nm=float(np.max(np.abs(res[keep]))),
                loo_rms_nm=float(np.sqrt(np.mean(np.square(loo)))), coverage_nm=[float(L[keep].min()), float(L[keep].max())],
                coefficients_low_to_high=[float(v) for v in c],
                oceanview=dict(intercept=f"{c[0]:.9g}", c1=f"{c[1]:.9g}", c2=f"{c[2]:.9g}" if deg >= 2 else "0", c3=f"{c[3]:.9g}" if deg >= 3 else "0"),
                device_minus_fit_at=dict({f"{w:.0f}": float(np.interp(np.interp(w, np.polyval(coef, px), px), px, device_wl) - w)
                                          for w in (300, 450, 600, 750, 900, 1050)}),
                noise_counts=float(noise), lines=lines, params=dict(min_rel=min_rel, isolation_nm=isolation_nm, snr=snr),
                nist_provenance=json.loads((DATA / "PROVENANCE.json").read_text()).get("retrieved_utc"))


def save_calibration(folder: Path, result: dict, device_wl, lamp_mean, dark_mean, raw_max, meta: dict) -> Path:
    from .storage import unique_dir
    out = unique_dir(folder / f"{dt.datetime.now():%Y-%m-%d}_{re.sub(r'[^A-Za-z0-9]+', '_', result.get('lamp', 'lamp')).strip('_')}")
    out.mkdir(parents=True)
    (out / "calibration.json").write_text(json.dumps(dict(result=result, meta=meta), indent=1, default=str))
    with open(out / "matched_lines.csv", "w", newline="", encoding="utf-8") as fh:
        if result.get("lines"):
            w = csv.DictWriter(fh, fieldnames=list(result["lines"][0])); w.writeheader(); w.writerows(result["lines"])
    px = np.arange(len(device_wl))
    new = np.polyval(np.array(result["coefficients_low_to_high"])[::-1], px) if result.get("ok") else np.full(len(px), np.nan)
    np.savetxt(out / "lamp_spectrum.csv", np.c_[px, device_wl, new, lamp_mean, dark_mean, raw_max], delimiter=",",
               header="pixel,device_wavelength_nm,calibrated_wavelength_nm,lamp_minus_dark_mean_counts,dark_mean_counts,raw_max_counts",
               comments="", fmt=["%d", "%.4f", "%.4f", "%.3f", "%.3f", "%.1f"])
    return out
