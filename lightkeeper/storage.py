"""Run storage: HDF5 tables appended by open-append-close flushes on a file thread, an events.csv journal and helpers.
A crash loses what has not been stored yet: about one flush interval of slots and at most one chunk of raw frames.
All stored times are UTC nanoseconds."""
from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

import h5py
import numpy as np

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("LIGHTKEEPER_TZ", "America/New_York"))   # IANA name, e.g. Europe/Berlin
except Exception:  # pragma: no cover
    LOCAL_TZ = None


RAW_CHUNK_ROWS = 400        # burst and dark frames: gzip finds a pixel's repeating values only within its 32 kB window, so
RAW_CHUNK_PIXELS = 16       # narrow chunks shrank real 10 ms frames about 21 % with no change for readers (2026-11-04)
RETRY_S = 2.0               # a failed write is tried again this often
FORCE_WAIT_S = 120.0        # how long a forced flush waits for the file thread
WAKE_AFTER_S = 120.0        # after this long without a write the data drive may be asleep (recording writes every 30 s)


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
    """events.csv. Once a Writer takes over (defer set), lines go through its file thread, in order with the data."""

    def __init__(self, path: Path):
        self.path = path
        self.defer = None
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(["t_wall_ns", "t_local", "event", "detail"])

    def __call__(self, event: str, detail: str = "") -> str:
        t = wall_ns()
        row = [t, iso_local(t), event, detail]

        def write():
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)
        self.defer(write) if self.defer else write()
        return f"[{local_dt(t):%H:%M:%S}] {event}: {detail}"


class FileWorker:
    """One thread that runs file jobs in the order they were submitted, so a slow disk never holds up the frame loop.
    A job that fails stays at the head of the queue and is tried again (a USB drive can come back), unless retry is False."""

    def __init__(self, note, wake=None):
        self.note = note
        self.wake = wake
        self.last_done = time.monotonic()
        self.q: collections.deque = collections.deque()
        self.cv = threading.Condition()
        self.queued = 0
        self.busy = False
        self.stopping = False
        self.failing = False
        self.thread = None
        self._last_error = ""

    def submit(self, job, size: int = 0, retry: bool = True):
        with self.cv:
            self.q.append((job, size, retry))
            self.queued += size
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="LightKeeperFiles")
                self.thread.start()
            self.cv.notify_all()

    def _run(self):
        while True:
            with self.cv:
                while not self.q:
                    self.busy = False
                    self.cv.notify_all()
                    if self.stopping:
                        return
                    self.cv.wait()
                job, size, retry = self.q[0]
                self.busy = True
            if self.wake and time.monotonic() - self.last_done > WAKE_AFTER_S:
                self.wake()
            try:
                job()
            except Exception as e:
                msg = repr(e)
                if msg != self._last_error:          # once per distinct error, not once per attempt
                    self._last_error = msg
                    self.note(f"write failed ({'will retry' if retry else 'dropped'}): {msg}")
                if retry:
                    self.failing = True
                    with self.cv:
                        self.cv.wait(RETRY_S)
                    if not self.stopping:
                        continue
            else:
                if self.failing:
                    self.failing = False
                    self._last_error = ""
                    self.note("writing works again")
            self.last_done = time.monotonic()
            with self.cv:
                self.q.popleft()
                self.queued -= size
                self.cv.notify_all()

    def drain(self, timeout: float | None = None) -> bool:
        """Wait for every submitted job to finish. False if the time ran out."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cv:
            while self.q or self.busy:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.cv.wait(left)
        return True

    def stop(self, timeout: float = 5.0):
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        if self.thread is not None:
            self.thread.join(timeout)


class Writer:
    """Tables: 'blocks' (one row per slot), 'dark_scans', 'burst'. Files rotate every rotate_min minutes (local clock).

    The acquisition thread only collects rows; a file thread writes them. An ordinary write stores whole chunks and holds
    back the rest, because a compressed chunk that grows is rewritten elsewhere and its old space is never reused (10-30 %
    of the 4 Nov 2026 files). A forced flush writes everything and waits, so what follows it can rely on the files."""

    def __init__(self, run_dir: Path, wl: np.ndarray, attrs: dict, rotate_min: int = 60, flush_s: float = 30.0,
                 files: list | None = None, rows: dict | None = None, block_rows: int = 30):
        """files/rows: continue an existing run (resume); new rows are appended to the existing hourly files."""
        self.dir, self.wl, self.attrs = run_dir, np.asarray(wl), attrs
        self.rotate_min, self.flush_s = max(1, int(rotate_min)), flush_s
        self.block_rows = int(min(256, max(1, block_rows)))
        self.buf: dict[str, list] = {}
        self.held: dict[str, list] = {}       # file thread only: rows waiting to complete a chunk
        self.attempt: dict[tuple, int] = {}   # file thread only: where a failed write began, so its retry starts there too
        self.last_flush = mono_ns()
        self.files: list[str] = list(files or [])
        self.rows = {"blocks": 0, "dark_scans": 0, "burst": 0}
        self.rows.update({k: int(v) for k, v in (rows or {}).items()})
        self.alt: dict[Path, Path] = {}       # hourly file -> replacement if the original cannot be opened
        self.notes: list[str] = []            # repairs and fallbacks, for the event log
        self.lock = threading.Lock()          # rows, files and the size figure, between the two threads
        self.worker = FileWorker(self.notes.append, self._wake_drive)
        self.closed = False
        self._size = self._measure()

    def _wake_drive(self):
        """File thread, after a quiet spell. A sleeping drive takes seconds to spin up, and HDF5 calls may keep the Python lock
        while they wait, which would stop the frame loop too. A small flush waits for the drive without holding that lock."""
        t0 = time.monotonic()
        p = self.dir / ".lightkeeper_wake"
        try:
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(fd, b"x")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.unlink(p)
        except OSError:                  # the drive or folder is gone: the job that follows reports it and is tried again
            return
        waited = time.monotonic() - t0
        if waited >= 1.0:
            self.notes.append(f"the data drive took {waited:.1f} s to wake")

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

    def pending(self, table: str) -> int:
        return len(self.buf.get(table, ()))

    def defer(self, job, retry: bool = True):
        """Run a small file job on the file thread, in order with the data; directly once the writer has closed."""
        if self.closed:
            job()
        else:
            self.worker.submit(job, 0, retry)

    def submit(self, force: bool = False):
        """Hand what has been collected to the file thread. Never touches the disk itself."""
        batch, self.buf = self.buf, {}
        size = sum(np.asarray(v).nbytes for rows in batch.values() for r in rows for v in r.values() if not isinstance(v, str))
        if batch or force:
            self.worker.submit(lambda: self._write(batch, force), int(size))

    def maybe_flush(self, force: bool = False) -> bool:
        if force:
            self.flush(); return True
        if (mono_ns() - self.last_flush) / 1e9 >= self.flush_s:
            self.submit()
            self.last_flush = mono_ns()
            return True
        return False

    def flush(self):
        """Write everything, including rows held for a chunk, and wait until it is on disk."""
        self.submit(force=True)
        self.last_flush = mono_ns()
        self.drain()

    def drain(self):
        if not self.worker.drain(FORCE_WAIT_S):
            raise TimeoutError(f"files still not written after {FORCE_WAIT_S:g} s; the rows stay queued and writing keeps trying")

    def backlog_bytes(self) -> int:
        return self.worker.queued

    def close(self):
        """Finish writing and stop the file thread. Later small jobs run directly."""
        self.flush()
        self.closed = True
        self.worker.stop()

    def _write(self, batch: dict, force: bool):
        """File thread. Rows leave 'held' as soon as they are stored, so a retry after a failure writes each row once."""
        for table, rows in batch.items():
            self.held.setdefault(table, []).extend(rows)
        batch.clear()
        for table in list(self.held):
            rr = self.held[table]
            if not rr:
                continue
            first, last = self._path(int(rr[0]["t_wall_ns"])), self._path(int(rr[-1]["t_wall_ns"]))
            if first == last:                            # the usual case: no Python per row, which would keep the frame loop waiting
                groups = {first: rr}
            else:
                groups = {}
                for r in rr:
                    groups.setdefault(self._path(int(r["t_wall_ns"])), []).append(r)
            bases = list(groups)
            for i, base in enumerate(bases):
                final = force or i < len(bases) - 1      # an hour that has ended gets no more rows
                n = self._append(table, base, groups[base], final)
                if n:
                    if len(groups) == 1:
                        self.held[table] = self.held[table][n:]
                    else:
                        done = {id(r) for r in groups[base][:n]}
                        self.held[table] = [r for r in self.held[table] if id(r) not in done]
                    with self.lock:
                        self.rows[table] = self.rows.get(table, 0) + n
                        self._size = self._measure()

    def _chunk(self, table: str, shape: tuple) -> tuple:
        """Chunk shape for a new dataset; shape[0] is ignored."""
        if len(shape) == 1:
            return (4096,)
        return (self.block_rows, shape[1]) if table == "blocks" else (RAW_CHUNK_ROWS, min(RAW_CHUNK_PIXELS, shape[1]))

    def _chunk_rows(self, table: str, row: dict, g=None) -> int:
        """Rows per chunk of a table's spectra: the existing dataset's if there is one (a resumed older file keeps its own)."""
        for k, v in row.items():
            if np.ndim(v) > 0:
                if g is not None and k in g:         # the table may not exist yet
                    return g[k].chunks[0]
                return self._chunk(table, (0, np.size(v)))[0]
        return 1

    def _append(self, table: str, base: Path, rr: list, final: bool) -> int:
        """Store rows at the end of one table of one file; returns how many. All columns are written from one common length,
        so a write that failed partway and is tried again cannot leave the columns uneven."""
        path = self.alt.get(base, base)
        new = not path.exists()
        if new and not final and len(rr) < self._chunk_rows(table, rr[0]):
            return 0                                     # nothing to store yet: do not create the file
        try:
            f = h5py.File(path, "a")
        except OSError as e:                             # unreadable (e.g. killed mid-write): continue in a new file
            if new:                                      # no file to blame: the drive or folder is gone, so try again later
                raise
            q = self._fallback(path); self.alt[base] = q
            self.notes.append(f"{path.name}: cannot open ({e!r}); writing to {q.name}")
            path, new = q, True
            f = h5py.File(path, "a")
        with f:
            if new or "wavelength_nm_device" not in f:
                for k, v in self.attrs.items():
                    f.attrs[k] = json.dumps(v, default=str) if isinstance(v, (dict, list, tuple)) or v is None else v
                f.create_dataset("wavelength_nm_device", data=self.wl)
            with self.lock:
                if str(path) not in self.files:
                    self.files.append(str(path))
            g = f.get(table)
            have = [g[k].shape[0] for k in rr[0] if k in g] if g is not None else []
            n0 = self.attempt.get((str(path), table), min(have) if have else 0)
            take = len(rr)
            if not final:
                take -= (n0 + take) % self._chunk_rows(table, rr[0], g)
            if take <= 0:
                return 0
            self.attempt[(str(path), table)] = n0
            g = f.require_group(table)                   # only now: readers expect a table that exists to hold rows
            for key in rr[0]:
                v0 = np.asarray(rr[0][key])
                if v0.dtype.kind in "US":
                    data = np.array([str(r[key]) for r in rr[:take]], dtype=h5py.string_dtype())
                else:
                    data = np.array([r[key] for r in rr[:take]], dtype=v0.dtype)
                if key not in g:
                    big = data.ndim > 1
                    kw = dict(fillvalue=np.nan) if data.dtype.kind == "f" else {}     # a column new to a resumed file
                    g.create_dataset(key, shape=(0,) + data.shape[1:], maxshape=(None,) + data.shape[1:], dtype=data.dtype,
                                     chunks=self._chunk(table, data.shape), compression="gzip" if big else None,
                                     compression_opts=4 if big else None, shuffle=big and table == "blocks", **kw)
                d = g[key]
                d.resize(n0 + take, axis=0); d[n0:n0 + take] = data
        del self.attempt[(str(path), table)]
        return take

    def set_attrs(self, **kw):
        """Add file attributes: to the files already written and to every later hourly file."""
        self.attrs.update(kw)
        self.drain()
        with self.lock:
            files = list(self.files)
        for fn in files:
            try:
                with h5py.File(fn, "a") as f:
                    for k, v in kw.items():
                        f.attrs[k] = json.dumps(v, default=str) if isinstance(v, (dict, list, tuple)) or v is None else v
            except Exception as e:
                self.notes.append(f"{Path(fn).name}: attributes {sorted(kw)} not written ({e!r})")

    def _measure(self) -> float:
        return sum(Path(p).stat().st_size for p in self.files if Path(p).exists()) / 1e6

    def size_mb(self) -> float:
        """As of the last write; the acquisition thread never checks the disk for this."""
        with self.lock:
            return self._size

    def snapshot(self) -> tuple[dict, list]:
        with self.lock:
            return dict(self.rows), list(self.files)
