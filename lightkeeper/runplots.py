"""Render final-run figures from the saved HDF5 slot means."""
from __future__ import annotations

from io import BytesIO
import json
import math
from pathlib import Path

import h5py
import numpy as np
from matplotlib import dates, rc_context
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from .display import active_channels
from .runview import MAX_SPECTRA, band_masks, broad_band_weights, log_display_darks
from .storage import LOCAL_TZ, local_dt


def _slot_rows(files):
    times, integrations, locations = [], [], []
    wavelength = model = None
    for file in files:
        with h5py.File(file, "r") as source:
            if "blocks/mean" not in source or not len(source["blocks/mean"]):
                continue
            axis = source["wavelength_nm_device"][:]
            if wavelength is None:
                wavelength = axis
                model = source.attrs.get("model", "")
            elif not np.array_equal(axis, wavelength):
                raise ValueError("Recorded files contain different detector wavelength axes")
            times.append(source["blocks/t_wall_ns"][:])
            integrations.append(source["blocks/int_us"][:])
            locations.append(file)
    if not times:
        return None
    t = np.concatenate(times)
    us = np.concatenate(integrations)
    # Rotation names are local-time based. Order by absolute time across DST or
    # multiple files from a resumed run. The detector channel order is preserved.
    order = np.argsort(t, kind="stable")
    keep = np.r_[True, np.diff(t[order]) > 0]
    return wavelength, model, t, us, order[keep], locations, np.cumsum([0] + [len(x) for x in times])


def _edges(centers, first=None, last=None):
    mid = (centers[1:] + centers[:-1]) / 2
    if first is None:
        first = 2 * centers[0] - mid[0]
        last = 2 * centers[-1] - mid[-1]
    return np.r_[first, mid, last]


def _write_figure(folder, name, axis, segments, limits, title, units, note):
    # The recorder and interactive window can use different matplotlib themes.
    with rc_context({"axes.facecolor": "#eceff2", "text.color": "black", "axes.labelcolor": "black",
                     "xtick.color": "black", "ytick.color": "black", "axes.edgecolor": "black"}):
        fig = Figure(figsize=(12, 5.8), layout="constrained", facecolor="white")
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        we = _edges(axis)
        for time_edges, values in segments:
            im = ax.pcolormesh(time_edges, we, values.T, cmap="magma", vmin=limits[0], vmax=limits[1], shading="flat")
        ax.set_ylim(we[0], we[-1])
        ax.xaxis.set_major_locator(dates.AutoDateLocator(tz=LOCAL_TZ))
        ax.xaxis.set_major_formatter(dates.DateFormatter("%b %d\n%H:%M %Z", tz=LOCAL_TZ))
        ax.set(xlabel="Local time", ylabel="Device wavelength (nm)", title=title)
        fig.colorbar(im, ax=ax, label=units)
        fig.supxlabel(note, fontsize=8)
        image = BytesIO()
        fig.savefig(image, format="png", dpi=145, metadata={"Description": note})
        fig.clear()
    return _save_png(folder, name, image)


def _save_png(folder, name, image):
    original = folder / name
    path = original
    for version in range(2, 10_002):
        try:
            output = path.open("xb")
        except FileExistsError:
            path = folder / f"{original.stem}_v{version:02d}{original.suffix}"
            continue
        try:
            with output:
                output.write(image.getbuffer())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path.name
    raise FileExistsError(f"Too many versions of {original.name}")


def _qc_metadata(locations, counts, channel_count):
    saturation = []
    bad_pixels = set()
    for file, count in zip(locations, counts):
        with h5py.File(file, "r") as source:
            recorded = source.get("blocks/n_saturated_scans")
            saturation.append(recorded[:] if recorded is not None else np.full(count, np.nan))
            for attr in ("hot_pixels", "cold_pixels"):
                pixels = json.loads(source.attrs.get(attr, "[]"))
                bad_pixels.update(int(i) for i in pixels if 0 <= int(i) < channel_count)
    usable = np.ones(channel_count, dtype=bool)
    usable[list(bad_pixels)] = False
    return np.concatenate(saturation), usable


def _qc_values(wavelength, usable, signal, exposures, saturation, missing, floors, channels):
    masks = band_masks(wavelength, usable)
    bands = np.column_stack([masks[key] for key in ("par", "red", "fr")]).astype(float)
    widths = bands.sum(axis=0)
    np.divide(bands, widths, out=bands, where=widths > 0)
    weights = np.column_stack((bands, broad_band_weights(wavelength, usable)))
    present = np.any(weights > 0, axis=0)
    used_pixels = np.any(weights > 0, axis=1)
    integrated = signal[:, used_pixels] @ weights[used_pixels]
    integrated[:, ~present] = np.nan
    floor_by_slot = np.full_like(integrated, np.nan)
    for exposure, floor in floors.items():
        floor_by_slot[exposures == exposure] = floor[channels][used_pixels] @ weights[used_pixels]
    known = (exposures > 0) & ~missing
    ratios_ok = known & (saturation == 0)
    above_floor = integrated > np.maximum(0, floor_by_slot)
    intensity = np.full(len(exposures), np.nan)
    np.divide(integrated[:, 0], exposures / 1e6, out=intensity, where=known & (integrated[:, 0] > 0))
    curves = [intensity]
    for numerator, denominator in ((1, 2), (3, 4), (5, 6)):
        ratio = np.full(len(exposures), np.nan)
        ok = ratios_ok & above_floor[:, numerator] & above_floor[:, denominator]
        if numerator == 5:
            ok &= integrated[:, numerator] <= integrated[:, denominator]
        np.divide(integrated[:, numerator], integrated[:, denominator], out=ratio, where=ok)
        curves.append(ratio * (100 if numerator == 5 else 1))
    return np.array(curves).T


def _write_qc_figure(folder, intervals, spectral_segments, acquisition_segments, note):
    panels = (("420–700 nm count rate", "counts/s", "#007b77"), ("Narrow R:FR  (655–665 / 725–735 nm)", "ratio", "#a34226"),
              ("B:G  (420–490 / 500–570 nm)", "ratio", "#2356a0"), ("Far-red share  (700–750 / 400–750 nm)", "%", "#8555aa"),
              ("Integration time", "ms", "#334155"), ("Saturated scans per slot", "scans", "#bd352f"))
    with rc_context({"axes.facecolor": "white", "text.color": "black", "axes.labelcolor": "black",
                     "xtick.color": "black", "ytick.color": "black", "axes.edgecolor": "#657582"}):
        fig = Figure(figsize=(12, 9.5), layout="constrained", facecolor="white")
        FigureCanvasAgg(fig)
        axes = fig.subplots(len(panels), 1, sharex=True)
        for i, (ax, (title, units, color)) in enumerate(zip(axes, panels)):
            ax.set_title(title, loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel(units)
            ax.yaxis.grid(True, color="#e7ecf0", linewidth=0.7)
            ax.spines[["top", "right"]].set_visible(False)
            has_data = False
            has_saturation = False
            for t, values in spectral_segments if i < 4 else acquisition_segments:
                y = values[:, i if i < 4 else i - 4]
                has_data |= np.isfinite(y).any()
                if i < 4:
                    ax.plot(t, y, color=color, linewidth=0.9, marker=".", markersize=1.4)
                elif i == 4:
                    ax.step(t, y, where="mid", color=color, linewidth=0.9, marker=".", markersize=1.4)
                else:
                    saturated = y > 0
                    has_saturation |= saturated.any()
                    ax.plot(t[saturated], y[saturated], linestyle="none", marker=".", markersize=3, color=color)
            if not has_data:
                ax.text(0.01, 0.45, "No valid values", transform=ax.transAxes, color="#566572", fontsize=9)
            elif i == 5 and not has_saturation:
                ax.text(0.01, 0.45, "Zero saturated scans in flagged slots", transform=ax.transAxes, color="#566572", fontsize=9)
            if i == 0 and has_data:
                ax.set_yscale("log")
            if i == 5:
                ax.set_ylim(0, max(1, ax.get_ylim()[1]))
            for (_, previous_end), (next_start, _) in zip(intervals, intervals[1:]):
                ax.axvspan(previous_end, next_start, facecolor="#dbe1e7", edgecolor="none", zorder=0)
        axes[-1].set_xlim(intervals[0][0], intervals[-1][-1])
        axes[-1].xaxis.set_major_locator(dates.AutoDateLocator(tz=LOCAL_TZ))
        axes[-1].xaxis.set_major_formatter(dates.DateFormatter("%b %d\n%H:%M %Z", tz=LOCAL_TZ))
        fig.suptitle("Run quality overview", fontsize=15, fontweight="bold")
        fig.supxlabel(note, fontsize=8)
        image = BytesIO()
        fig.savefig(image, format="png", dpi=145, metadata={"Description": note})
        fig.clear()
    return _save_png(folder, "run_qc_overview.png", image)


def export_run_figures(run_dir, meta, *, output_dir=None):
    """Export display-resolution images. HDF5 inputs are opened read-only."""
    run_dir = Path(run_dir)
    folder = Path(output_dir) if output_dir is not None else run_dir
    files = sorted(run_dir.glob("spectra_*.h5"))
    rows = _slot_rows(files)
    if rows is None:
        return {}
    wavelength, model, times, integrations, order, locations, offsets = rows
    spacing = np.diff(wavelength)
    if not spacing.size or not (np.all(spacing > 0) or np.all(spacing < 0)):
        raise ValueError("A monotonic detector wavelength axis is required to render spectrograms")
    cadence = float(meta.get("plan", {}).get("slot_s") or 1)
    if not np.isfinite(cadence) or cadence <= 0:
        raise ValueError("Recorded slot cadence must be positive")
    cadence_ns = round(cadence * 1e9)
    stride = max(1, math.ceil(len(order) / MAX_SPECTRA))
    split = np.flatnonzero(np.diff(times[order]) > 1.5 * cadence_ns) + 1
    groups = np.split(order, split)
    selection = np.concatenate([g[::stride] for g in groups])
    raw = np.empty((len(selection), len(wavelength)), np.float32)
    for file, start, stop in zip(locations, offsets[:-1], offsets[1:]):
        destinations = np.flatnonzero((selection >= start) & (selection < stop))
        if not len(destinations):
            continue
        indices = selection[destinations] - start
        sorted_indices = np.argsort(indices)
        with h5py.File(file, "r") as source:
            raw[destinations[sorted_indices]] = source["blocks/mean"][indices[sorted_indices]]

    time_edges, centers_by_group = [], []
    for group in groups:
        centers = dates.date2num([local_dt(int(t)) for t in times[group[::stride]]])
        start = dates.date2num(local_dt(int(times[group[0]] - cadence_ns // 2)))
        stop = dates.date2num(local_dt(int(times[group[-1]] + cadence_ns // 2)))
        time_edges.append(_edges(centers, start, stop))
        centers_by_group.append(centers)
    cuts = np.cumsum([len(g[::stride]) for g in groups])[:-1]
    clock_note = f"{len(selection):,} plotted of {len(order):,} unique slots, every {stride} within recorded intervals. Gray marks gaps."
    raw_limits = (float(np.nanmin(raw)), float(np.nanmax(raw)))
    raw_name = _write_figure(folder, "spectrogram_raw_counts.png", wavelength,
                             list(zip(time_edges, np.split(raw, cuts))), raw_limits,
                             "Raw-count spectrogram", "Recorded counts per scan (slot mean)",
                             f"All detector channels. Linear color scale. {clock_note}")
    result = dict(raw_counts=raw_name, plotted_slots=len(selection), unique_recorded_slots=len(order), slot_stride=stride,
                  gap_cutoff_s=1.5 * cadence, raw_color_limits_counts=list(raw_limits))

    darks, floors, dark_source = log_display_darks(files)
    us = integrations[selection]
    channels = active_channels(model, len(wavelength))
    signal = raw[:, channels].copy()
    missing = np.zeros(len(us), bool)
    detected = signal > 0
    if darks:
        for exposure in np.unique(us):
            match = us == exposure
            if int(exposure) in darks:
                signal[match] -= darks[int(exposure)][channels]
                detected[match] = signal[match] > floors[int(exposure)][channels]
            else:
                missing[match] = True
    available = np.isfinite(signal) & (us[:, None] > 0) & ~missing[:, None]
    valid = detected & available
    counts_per_s = np.full_like(signal, np.nan)
    np.divide(signal, us[:, None] / 1e6, out=counts_per_s, where=valid)
    np.log10(counts_per_s, out=counts_per_s)
    finite = counts_per_s[np.isfinite(counts_per_s)]
    log_limits = tuple(float(x) for x in np.percentile(finite, (1, 99))) if finite.size else (0.0, 1.0)
    if log_limits[0] == log_limits[1]:
        log_limits = log_limits[0] - 0.5, log_limits[1] + 0.5
    # Give every sub-floor value the same lowest color. Missing slots and dark
    # matches stay gray, so noise near zero does not appear as scattered signal.
    below_floor = available & ~detected
    counts_per_s[below_floor] = log_limits[0]
    corrected = f"Matched {dark_source}" if darks else "No dark set available, so rates are uncorrected"
    floor_note = ("Darkest color covers ≤ max(5× dark SD, 2× pre/post shift) at each channel."
                  if darks else "Nonpositive counts use the darkest color.")
    missing_note = f" {int(missing.sum()):,} plotted slots lacked an exact exposure match." if missing.any() else ""
    log_name = _write_figure(folder, "spectrogram_log10_counts_per_s.png", wavelength[channels],
                             list(zip(time_edges, np.split(counts_per_s, cuts))), log_limits,
                             "Log-count-rate spectrogram", "log₁₀(counts/s)",
                             f"{corrected}. {floor_note}{missing_note}\n{clock_note} Color scale uses the 1st–99th percentiles above the floor.")
    result.update(log10_counts_per_s=log_name, log_dark_source=dark_source, log_slots_without_matched_dark=int(missing.sum()),
                  log_color_limits=list(log_limits), log_omits_terminal_channels=channels.start is not None,
                  log_pixels_below_floor=int(below_floor.sum()),
                  log_floor_rule="max(5 × matched single-scan dark SD, 2 × absolute pre/post dark shift) per channel when available")
    try:
        saturation, usable = _qc_metadata(locations, np.diff(offsets), len(wavelength))
        qc = _qc_values(wavelength[channels], usable[channels], signal, us, saturation[selection], missing, floors, channels)
        intervals = [(edges[0], edges[-1]) for edges in time_edges]
        acquisition = [(dates.date2num(times[group].astype("datetime64[ns]")),
                        np.column_stack((integrations[group] / 1e3, saturation[group]))) for group in groups]
        unknown_saturation = int(np.isnan(saturation[order]).sum())
        saturation_note = f" Saturation unknown for {unknown_saturation:,} slots." if unknown_saturation else ""
        note = (f"{corrected}. Device wavelengths and detector counts. Spectral traces show every {stride} slot(s) within intervals.\n"
                "Ratios use bands above the projected max(5× dark SD, 2× pre/post shift). Unmatched darks, saturated slots and flagged pixels are excluded from ratios.\n"
                f"Acquisition panels use all {len(order):,} slots. Gray spans mark timestamp gaps longer than {1.5 * cadence:g} s.{saturation_note}")
        result["qc_overview"] = _write_qc_figure(folder, intervals, list(zip(centers_by_group, np.split(qc, cuts))), acquisition, note)
    except Exception as e:
        result["qc_overview_error"] = f"{type(e).__name__}: {e}"
    return result
