"""Run storage: HDF5 tables appended by open-append-close flushes (a crash loses at most one flush interval),
an events.csv journal and helpers. All stored times are UTC nanoseconds."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
from pathlib import Path

import h5py
import numpy as np

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("LIGHTKEEPER_TZ", "America/New_York"))   # IANA name, e.g. Europe/Berlin
except Exception:  # pragma: no cover
    LOCAL_TZ = None


def wall_ns() -> int:
    return time.time_ns()


def mono_ns() -> int:
    return time.perf_counter_ns()


def local_dt(ns: int) -> dt.datetime:
    t = dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc)
    return t.astimezone(LOCAL_TZ) if LOCAL_TZ else t.astimezone()


def now_local() -> dt.datetime:
    return local_dt(wall_ns())


def iso_local(ns: int) -> str:
    return local_dt(ns).isoformat(timespec="milliseconds")


def unique_dir(base: Path) -> Path:
    k = 1
    while True:
        p = base.with_name(f"{base.name}_v{k:02d}")
        if not p.exists():
            return p
        k += 1


class Events:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(["t_wall_ns", "t_local", "event", "detail"])

    def __call__(self, event: str, detail: str = "") -> str:
        t = wall_ns()
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([t, iso_local(t), event, detail])
        return f"[{local_dt(t):%H:%M:%S}] {event}: {detail}"


class Writer:
    """Tables: 'blocks' (one row per slot), 'dark_scans', 'burst'. Files rotate every rotate_min minutes (local clock)."""

    def __init__(self, run_dir: Path, wl: np.ndarray, attrs: dict, rotate_min: int = 60, flush_s: float = 30.0,
                 files: list | None = None, rows: dict | None = None):
        """files/rows: continue an existing run (resume); new rows are appended to the existing hourly files."""
        self.dir, self.wl, self.attrs = run_dir, np.asarray(wl), attrs
        self.rotate_min, self.flush_s = max(1, int(rotate_min)), flush_s
        self.buf: dict[str, list] = {}
        self.last_flush = mono_ns()
        self.files: list[str] = list(files or [])
        self.rows = {"blocks": 0, "dark_scans": 0, "burst": 0}
        self.rows.update({k: int(v) for k, v in (rows or {}).items()})
        self.alt: dict[Path, Path] = {}       # hourly file -> replacement if the original cannot be opened
        self.notes: list[str] = []            # repairs and fallbacks, for the event log

    def repair(self) -> list[str]:
        """Resume after a hard stop: a flush interrupted mid-write can leave the columns of one table with different
        lengths. Trim every table to its shortest column (losing at most that one partial flush) and report it."""
        out = []
        for fn in list(self.files):
            try:
                with h5py.File(fn, "a") as f:
                    for t in ("blocks", "dark_scans", "burst"):
                        if t not in f:
                            continue
                        lens = {k: f[t][k].shape[0] for k in f[t]}
                        n = min(lens.values()) if lens else 0
                        if lens and max(lens.values()) != n:
                            for k, m in lens.items():
                                if m != n:
                                    f[t][k].resize(n, axis=0)
                            out.append(f"{Path(fn).name}/{t}: trimmed to {n} rows (column lengths were {sorted(set(lens.values()))})")
            except Exception as e:
                out.append(f"{Path(fn).name}: cannot open ({e!r}); new rows for that hour go to a separate file")
                self.alt[Path(fn)] = self._fallback(Path(fn))
        self.notes += out
        return out

    def _fallback(self, path: Path) -> Path:
        k = 2
        while (q := path.with_name(f"{path.stem}_part{k}{path.suffix}")).exists():
            k += 1
        return q

    def _path(self, t_ns: int) -> Path:
        t = local_dt(t_ns)
        m = (t.hour * 60 + t.minute) // self.rotate_min * self.rotate_min
        # fold = 1: second pass through the repeated hour at the autumn DST change -> "...0100b.h5" (sorts after "...0100.h5")
        return self.dir / f"spectra_{t:%Y%m%d}_{m // 60:02d}{m % 60:02d}{'b' if t.fold else ''}.h5"

    def add(self, table: str, row: dict):
        self.buf.setdefault(table, []).append(row)

    def maybe_flush(self, force: bool = False) -> bool:
        if force or (mono_ns() - self.last_flush) / 1e9 >= self.flush_s:
            self.flush(); return True
        return False

    def flush(self):
        for table, rows in list(self.buf.items()):
            if not rows:
                continue
            groups: dict[Path, list] = {}
            for r in rows:
                base = self._path(int(r["t_wall_ns"]))
                groups.setdefault(self.alt.get(base, base), []).append(r)
            for path, rr in groups.items():
                new = not path.exists()
                try:
                    f = h5py.File(path, "a")
                except OSError as e:                     # unreadable (e.g. killed mid-write): continue in a new file
                    q = self._fallback(path); self.alt[path] = q
                    self.notes.append(f"{path.name}: cannot open ({e!r}); writing to {q.name}")
                    path, new = q, True
                    f = h5py.File(path, "a")
                with f:
                    if new:
                        for k, v in self.attrs.items():
                            f.attrs[k] = json.dumps(v, default=str) if isinstance(v, (dict, list, tuple)) or v is None else v
                        f.create_dataset("wavelength_nm_device", data=self.wl)
                    if str(path) not in self.files:
                        self.files.append(str(path))
                    g = f.require_group(table)
                    for key in rr[0]:
                        v0 = np.asarray(rr[0][key])
                        if v0.dtype.kind in "US":
                            data = np.array([str(r[key]) for r in rr], dtype=h5py.string_dtype())
                        else:
                            data = np.stack([np.asarray(r[key], dtype=v0.dtype) for r in rr])
                        if key not in g:
                            big = data.ndim > 1
                            g.create_dataset(key, shape=(0,) + data.shape[1:], maxshape=(None,) + data.shape[1:], dtype=data.dtype,
                                             chunks=((64,) + data.shape[1:]) if big else (4096,),
                                             compression="gzip" if big else None, compression_opts=4 if big else None)
                        d = g[key]; n0 = d.shape[0]
                        d.resize(n0 + data.shape[0], axis=0); d[n0:] = data
            self.rows[table] = self.rows.get(table, 0) + len(rows)
            self.buf[table] = []
        self.last_flush = mono_ns()

    def size_mb(self) -> float:
        return sum(Path(p).stat().st_size for p in self.files if Path(p).exists()) / 1e6
