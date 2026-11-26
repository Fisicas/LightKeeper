"""Detector-channel limits for spectral display."""
from __future__ import annotations

import re

import numpy as np


def active_channels(model: str, size: int) -> slice:
    # Ocean Optics Maya2000Pro OEM data sheet, 020-00000-001-05-201603, p. 7.
    # The same detector order is present in the 2068-channel OceanDirect recordings.
    model = re.sub(r"[^a-z0-9]", "", str(model).casefold())
    if model in ("maya2000pro", "mayapro2000") and size in (2068, 2304):
        return slice(10, 2058)
    return slice(None)


def display_channels(model: str, size: int, raw=None) -> slice:
    known = active_channels(model, size)
    if known.start is not None or raw is None or size < 48:
        return known
    # CzernyScope's conservative zero-padding rule. A dim spectrum or an edge with
    # a few low readings is insufficient evidence to remove detector channels.
    values = np.atleast_2d(raw)
    if values.ndim != 2 or values.shape[1] != size:
        return known

    # In the archived 650-channel SeaBreeze USB650 output, pixel 0 is
    # identically zero while the adjacent channels retain the baseline.
    if ("usb650" in str(model).casefold() and size == 650 and np.all(values[:, 0] == 0)
            and np.min(np.median(np.abs(values[:, 1:9]), axis=0)) > 32):
        return slice(1, None)

    def edge_length(rows):
        nonzero = np.flatnonzero(np.any(rows != 0, axis=0))
        if not nonzero.size:
            return 0
        length = int(nonzero[0])
        adjacent = rows[:, length:length + 8]
        if length >= 16 and adjacent.shape[1] == 8 and np.min(np.median(np.abs(adjacent), axis=0)) > 32:
            return length
        return 0

    left = edge_length(values)
    right = edge_length(values[:, ::-1])
    if 0 < left + right <= size // 2:
        return slice(left, size - right)
    return known
