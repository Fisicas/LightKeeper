"""Acquisition engine: one background thread owns the spectrometer. The GUI talks to it only through queues.

States
  DISCONNECTED  no device             IDLE      connected, not scanning
  PREVIEW       live view, nothing stored
  DARK          dark set in progress (input must be covered)      LAMP  calibration acquisition
  AWAIT_UNCOVER pre-run darks done; waiting for the user to uncover the input and press Continue
  STANDBY       run armed, live view, waiting for the scheduled start (nothing stored)
  RECORDING     storing one row per time slot             BURST  storing every scan (fast burst)
  POST_RUN      recording finished; live view; waiting for post-run darks (or Skip)
Commands (tuples): connect(id, edc, nlc) | connect_serial(serial, edc, nlc, hint_id) | disconnect | preview(on, auto, int_ms)
                   lamp(kind, int_ms, n, lamp_name) | start(plan_dict) | resume(run_dir) | continue | stop | post_dark
                   finish | interrupt | quit
Run journal (0.2.0): run_state.json in the run folder is rewritten atomically at every state change and every data flush;
'resume' rebuilds a run from it (and run_metadata.json + the HDF5 files) after the recorder was stopped or crashed.
Each resumed stretch of recording is a new 'segment' (column in the blocks table); gaps are logged in events.csv.
Darks cannot be taken during a run (the input cannot be covered), so a run brackets itself with dark sets over the
WHOLE integration-time ladder it may use: before the start (pre) and after the stop (post).
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import h5py
import numpy as np

from . import VERSION
from . import clock
from .devices import SIM_ID, OceanDevice, SimDevice, open_by_serial
from .procs import read_json, write_json_atomic
from .schedule import SETUP_FIELDS, RunPlan, burst_times, dark_scan_count, ladder_us, sun_events
from .storage import LOCAL_TZ, Events, Writer, iso_local, local_dt, mono_ns, now_local, unique_dir, wall_ns

SAT_LEVEL = 60000.0
STEP_DOWN_MAX = 55000.0
TARGET_HIGH = 40000.0
LOW_P995 = 15000.0
N_LOW = 10
AE_WINDOW_NM = (400.0, 1050.0)
DISCARD = 2
FORMAT_VERSION = 2               # 0.3.1: HDF5/run_metadata format; 0.1.0 files have no attribute = format 1
CLOCK_EVERY_NS = 3600 * 10 ** 9
BURST_MIN_FILL = 0.01             # skip a burst whose predicted p99.5 is below this fraction of SAT_LEVEL


def despiked(y):
    """3-pixel running minimum: single hot pixels (which grow with integration time) cannot drive auto-exposure."""
    return np.minimum(np.minimum(y[:-2], y[1:-1]), y[2:])


def prevent_sleep(on: bool):
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def time_sync_status() -> str:
    try:
        r = subprocess.run(["w32tm", "/query", "/status"], capture_output=True, text=True, timeout=10)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"unavailable: {e!r}"


class Engine(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="LightKeeperEngine")
        self.cmd: queue.Queue = queue.Queue()
        self.evt: queue.Queue = queue.Queue()
        self._clock_q: queue.Queue = queue.Queue()
        self.dev = None
        self.state = "DISCONNECTED"
        self.auto = True
        self.low_count = 0
        self.darks: dict[int, np.ndarray] = {}
        self._quit = False
        self._last_scan_emit = 0
        self.ctx = None          # run context dict
        self.conn = None         # (serial, edc, nlc) of the open device, used to reopen it after USB faults

    # ---------------------------------------------------------------- plumbing
    def send(self, *command):
        self.cmd.put(command)

    def emit(self, typ, **kw):
        self.evt.put(dict(type=typ, **kw))

    def log(self, event, detail=""):
        line = self.ctx["events"](event, detail) if self.ctx else f"[{now_local():%H:%M:%S}] {event}: {detail}"
        self.emit("log", text=line)

    def set_state(self, s, detail=""):
        self.state = s
        self.emit("state", state=s, detail=detail)
        if self.ctx is not None:
            self._save_state()

    def emit_scan(self, y, force=False):
        t = mono_ns()
        if force or t - self._last_scan_emit > 100_000_000:
            self._last_scan_emit = t
            self.emit("scan", y=y.astype(np.float32), int_us=self.dev.int_us, t_ns=wall_ns())

    # ---------------------------------------------------------------- thread main
    def run(self):
        while not self._quit:
            try:
                self._drain()
                if self.dev is None or self.state in ("IDLE", "DISCONNECTED"):
                    time.sleep(0.03); continue
                if self.state == "RECORDING":
                    self._record_slot()
                elif self.state in ("PREVIEW", "STANDBY", "POST_RUN", "AWAIT_UNCOVER"):
                    y = self._scan()
                    self.emit_scan(y)
                    if self.auto and self.state != "AWAIT_UNCOVER":
                        m = float(despiked(y[self._win()]).max())
                        self._auto_exposure(m, float(np.percentile(despiked(y[self._win()]), 99.5)), per_scan=True)
                    if self.state == "STANDBY" and now_local() >= self.ctx["t_start"]:
                        self._begin_recording()
                else:
                    time.sleep(0.03)
            except Exception as e:  # keep the thread alive; report
                self.emit("error", text=f"{type(e).__name__}: {e}")
                if self.ctx and self.state in ("RECORDING", "BURST"):
                    self.log("error", repr(e))
                    try:
                        self.ctx["writer"].flush()
                    except Exception:
                        pass
                time.sleep(0.5)
        self._shutdown()

    def _drain(self):
        while True:
            try:
                c = self.cmd.get_nowait()
            except queue.Empty:
                return
            getattr(self, "_c_" + c[0])(*c[1:])

    # ---------------------------------------------------------------- helpers
    def _win(self):
        wl = self.dev.wavelengths
        return (wl >= AE_WINDOW_NM[0]) & (wl <= AE_WINDOW_NM[1])

    def _scan(self):
        for attempt in range(6):
            try:
                return self.dev.scan()
            except Exception as e:
                self.emit("error", text=f"scan failed (attempt {attempt + 1}): {e!r}")
                if self.ctx:
                    self.log("scan_error", repr(e))
                time.sleep(1.0)
                if attempt in (2, 4):
                    self._reopen()
        raise RuntimeError("six consecutive scan failures")

    def _reopen(self):
        """Close and reopen the spectrometer by serial (USB glitch, unplug/replug). Keeps integration time."""
        if self.conn is None or self.dev is None or self.dev.simulated:
            return
        us = self.dev.int_us
        try:
            self.dev.close()
        except Exception:
            pass
        time.sleep(2.0)
        try:
            self.dev = open_by_serial(*self.conn)
            self.dev.set_int(us)
            msg = f"reopened {self.conn[0]} as device id {getattr(self.dev, 'device_id', '?')}"
            self.log("device_reopened", msg) if self.ctx else self.emit("log", text=msg)
        except Exception as e:
            self.emit("error", text=f"reopen failed: {e!r}")

    def _set_int(self, us, why="", log=True):
        old = self.dev.int_us
        new = self.dev.set_int(us)
        for _ in range(DISCARD):
            self._scan()
        if self.ctx:
            self.ctx["used_us"].add(new)
        if log and new != old:
            msg = f"{old / 1000:g} -> {new / 1000:g} ms ({why})"
            self.log("integration", msg) if self.ctx else self.emit("log", text=f"[{now_local():%H:%M:%S}] integration {msg}")
        self.emit("int", int_us=new)
        return new

    def _ladder(self):
        if self.ctx:
            return self.ctx["ladder"]
        plan = RunPlan()
        return ladder_us(plan, self.dev.min_us, self.dev.max_us)

    def _auto_exposure(self, bmax, p995, per_scan=False):
        lad = self._ladder(); cur = self.dev.int_us
        i = int(np.argmin(np.abs(np.array(lad) - cur)))
        if bmax >= STEP_DOWN_MAX and cur > lad[0]:
            target = cur * min(0.5, 0.8 * TARGET_HIGH / max(bmax, 1))
            j = max([k for k, v in enumerate(lad) if v <= target] or [0])
            self.low_count = 0
            self._set_int(lad[j], f"peak {bmax:.0f} counts")
        elif p995 < LOW_P995:
            self.low_count += 1
            if self.low_count >= (5 if per_scan else N_LOW) and cur < lad[-1]:
                ok = [k for k, v in enumerate(lad) if v > cur and bmax * v / cur <= TARGET_HIGH]
                if ok:
                    self._set_int(lad[max(ok)], f"p99.5 {p995:.0f}, peak {bmax:.0f} counts")
                self.low_count = 0
        else:
            self.low_count = 0

    # ---------------------------------------------------------------- commands
    def _c_connect(self, dev_id, edc=True, nlc=True):
        if self.ctx is not None:   # a run owns the device; a late reconnect would drop it to IDLE
            self.log("connect_ignored", "connect requested while a run is active; ignored"); return
        if self.dev is not None:
            self._c_disconnect()
        try:
            self.dev = SimDevice() if dev_id == SIM_ID else OceanDevice(dev_id, edc, nlc)
            self.dev.set_corrections(edc, nlc)
            self.dev.set_int(100_000)
        except Exception as e:
            self.dev = None
            self.emit("error", text=f"connect failed: {e!r}  (close OceanView; only one program can hold the spectrometer)")
            self.set_state("DISCONNECTED"); return
        self.conn = (self.dev.serial, edc, nlc)
        self.emit("device", info=self.dev.info(), wavelengths=self.dev.wavelengths.copy())
        self.set_state("IDLE", "connected")

    def _c_connect_serial(self, serial, edc=True, nlc=True, hint_id=None):
        """Open by serial (the background recorder: device ids are per-process). Emits 'device' or 'connect_failed'."""
        if self.ctx is not None:   # a run owns the device; a late reconnect would drop it to IDLE
            self.log("connect_ignored", "connect requested while a run is active; ignored"); return
        if self.dev is not None:
            self._c_disconnect()
        try:
            self.dev = open_by_serial(serial, edc, nlc, hint_id)
            self.dev.set_corrections(edc, nlc)
            self.dev.set_int(100_000)
        except Exception as e:
            self.dev = None
            self.emit("connect_failed", text=f"{e!r}")
            self.set_state("DISCONNECTED"); return
        self.conn = (self.dev.serial, edc, nlc)
        self.emit("device", info=self.dev.info(), wavelengths=self.dev.wavelengths.copy())
        self.set_state("IDLE", "connected")

    def _c_disconnect(self):
        if self.state in ("RECORDING", "BURST", "STANDBY", "POST_RUN", "AWAIT_UNCOVER"):
            self.emit("error", text="stop the run before disconnecting"); return
        if self.dev is not None:
            self.dev.close(); self.dev = None
        self.conn = None
        self.set_state("DISCONNECTED")

    def _c_preview(self, on, auto=True, int_ms=None):
        if self.dev is None or self.state not in ("IDLE", "PREVIEW"):
            return
        self.auto = bool(auto)
        if int_ms:
            self._set_int(int(int_ms * 1000), "preview setting")
        self.set_state("PREVIEW" if on else "IDLE")

    def _c_lamp(self, kind, int_ms, n, lamp_name=""):
        """kind 'dark' or 'lamp': average n scans at int_ms; emits mean, sd and per-pixel raw max."""
        if self.dev is None or self.state not in ("IDLE", "PREVIEW"):
            self.emit("error", text="calibration acquisitions need an idle/preview spectrometer"); return
        prev = self.state
        self.set_state("LAMP", f"{kind}, {int_ms:g} ms x {n}")
        if self.dev.simulated:
            self.dev.mode = "dark" if kind == "dark" else "lamp"
            if kind == "lamp":
                from .calibration import LAMPS, load_species
                L = np.concatenate([load_species(s) for s in LAMPS.get(lamp_name, ["Hg I", "Ar I"])])
                L = L[L["rel"] >= 0.02]; self.dev.lamp_lines = (L["wl"], L["rel"])
        self._set_int(int(int_ms * 1000), f"calibration {kind}", log=False)
        acc = 0; acc2 = 0; mx = None
        for k in range(int(n)):
            y = self._scan(); acc = acc + y; acc2 = acc2 + y * y
            mx = y if mx is None else np.maximum(mx, y)
            self.emit_scan(y)
            self.emit("progress", what=f"{kind} {k + 1}/{n}", frac=(k + 1) / n)
        mean = acc / n; sd = np.sqrt(np.maximum(acc2 / n - mean ** 2, 0) * n / max(n - 1, 1))
        if self.dev.simulated:
            self.dev.mode = "sky"
        self.emit("lamp_result", kind=kind, mean=mean, sd=sd, raw_max=mx, int_us=self.dev.int_us, n=int(n), lamp=lamp_name,
                  t_ns=wall_ns())
        self.set_state(prev)

    def _c_start(self, plan_dict):
        if self.dev is None or self.state not in ("IDLE", "PREVIEW"):
            self.emit("error", text="connect a spectrometer (and stop any other activity) before starting a run"); return
        plan = RunPlan.from_dict(plan_dict)
        t_start, t_stop = plan.start_dt(), plan.stop_dt()
        if t_stop <= max(t_start, now_local()):
            self.emit("error", text="stop time must be after the start time and in the future"); return
        out = Path(plan.out_dir); out.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in plan.label).strip("_") or "run"
        run_dir = unique_dir(out / f"{t_start:%Y-%m-%d}_{safe_label}"); run_dir.mkdir(parents=True)
        self.dev.set_corrections(plan.edc, plan.nlc)
        info = self.dev.info()
        lad = ladder_us(plan, self.dev.min_us, self.dev.max_us)
        meta = dict(app=f"LightKeeper {VERSION}", python=sys.version, platform=platform.platform(), numpy=np.__version__,
                    h5py=h5py.__version__, plan=plan.to_dict(), device=info, ladder_us=lad,
                    planned_start_local=t_start.isoformat(), planned_stop_local=t_stop.isoformat(),
                    sun_events_local={k: (v.isoformat() if v else None) for k, v in sun_events(
                        t_start.date(), plan.site_lat, plan.site_lon).items()},
                    time_note=f"all stored times are UTC ns; folder/file names use local time ({LOCAL_TZ})",
                    time_sync_status=time_sync_status(), format_version=FORMAT_VERSION,
                    auto_exposure=dict(step_down_above=STEP_DOWN_MAX, target_high=TARGET_HIGH, low_p995=LOW_P995,
                                       n_low_slots=N_LOW, window_nm=AE_WINDOW_NM, discard_after_change=DISCARD),
                    saturation_level_counts=SAT_LEVEL,
                    exposure_timing_note="Maya2000Pro benchmark 2026-10-31: scan period = integration time (no dead time); "
                                         "each scan timestamp is the host time at return = end of that exposure")
        if plan.setup_photo:
            try:
                src = Path(plan.setup_photo); dst = run_dir / f"setup_photo{src.suffix.lower()}"
                shutil.copy2(src, dst); meta["setup_photo_copy"] = dst.name
            except Exception as e:
                meta["setup_photo_copy"] = f"failed: {e!r}"
        (run_dir / "run_metadata.json").write_text(json.dumps(meta, indent=1, default=str))
        attrs = dict(app=meta["app"], serial=info.get("serial", ""), model=info.get("model", ""), label=plan.label,
                     notes=plan.notes, geometry=plan.geometry, edc=str(info.get("electric_dark_correction")),
                     nlc=str(info.get("nonlinearity_correction")), format_version=FORMAT_VERSION,
                     **{k: getattr(plan, k) for k in SETUP_FIELDS})
        self.ctx = dict(plan=plan, dir=run_dir, meta=meta, events=Events(run_dir / "events.csv"),
                        writer=Writer(run_dir, self.dev.wavelengths, attrs, plan.rotate_min, plan.flush_s),
                        t_start=t_start, t_stop=t_stop, ladder=lad, used_us=set(), n_slots=0, n_sat=0, burst_id=0,
                        dark_session=0, bursts=burst_times(plan, t_start, t_stop), stop_req=False,
                        segment=0, dark_sets=[], pre_done=False, last_slot_ns=None, uncovered=False)
        self.auto = plan.auto_exposure
        self.emit("run", dir=str(run_dir), plan=plan.to_dict(), ladder_us=lad, t_start=t_start.isoformat(), t_stop=t_stop.isoformat())
        self.log("run_created", f"{run_dir}")
        if str(meta.get("setup_photo_copy", "")).startswith("failed"):
            self.log("setup_photo_failed", meta["setup_photo_copy"])
        self._clock_check("start", sync=True)
        self._save_state(status="open")
        prevent_sleep(True)
        if not plan.auto_exposure:
            self._set_int(int(plan.int_ms * 1000), "fixed")
        if plan.pre_darks:
            self._dark_set("pre")
            self.set_state("AWAIT_UNCOVER", "pre-run darks done: uncover the input, then Continue")
        else:
            self._c_continue()

    def _c_continue(self):
        if self.ctx is None:
            return
        p = self.ctx["plan"]
        self.ctx["uncovered"] = True
        if self.ctx["segment"] > 0 and now_local() >= self.ctx["t_start"]:   # resumed run that was waiting for 'uncover'
            self._begin_recording(); return
        self._set_int(int(p.int_ms * 1000), "run start setting")
        self.log("armed", f"start {self.ctx['t_start']:%Y-%m-%d %H:%M:%S}, stop {self.ctx['t_stop']:%H:%M:%S}")
        self.set_state("STANDBY", f"waiting for {self.ctx['t_start']:%H:%M:%S}")

    def _c_stop(self):
        if self.ctx is None:
            return
        if self.state in ("RECORDING", "BURST"):
            self.ctx["stop_req"] = True
        elif self.state in ("STANDBY", "AWAIT_UNCOVER"):
            self.log("cancelled", "stopped before recording began")
            self._end_recording()

    def _c_post_dark(self):
        if self.ctx is None or self.state != "POST_RUN":
            return
        self._dark_set("post")
        self._finalize("post-run darks recorded")

    def _c_finish(self):
        if self.ctx is not None and self.state == "POST_RUN":
            self.log("post_darks_skipped", "user skipped post-run darks")
            self._finalize("post-run darks skipped")

    def _c_quit(self, mode="finalize"):
        """mode 'finalize' (0.1 behaviour: close the run) or 'interrupt' (leave it open and resumable)."""
        if self.ctx is not None:
            if mode == "interrupt":
                self._c_interrupt("recorder shutting down")
            else:
                if self.state in ("RECORDING", "BURST"):
                    self.ctx["stop_req"] = True
                    self._end_recording()
                if self.ctx is not None:
                    self._finalize("application closed")
        self._quit = True

    def _c_interrupt(self, why="interrupted by user (resume later)"):
        """Stop acquiring but leave the run OPEN: everything is flushed, run_state.json says 'interrupted', and the run can be
        resumed (it continues as a new segment) until its scheduled stop, then gets its post-run darks."""
        c = self.ctx
        if c is None:
            return
        c["writer"].maybe_flush(force=True)
        self.log("interrupted", why)
        c["meta"].setdefault("interruptions", []).append(dict(local=iso_local(wall_ns()), phase=self.state, why=why))
        (c["dir"] / "run_metadata.json").write_text(json.dumps(c["meta"], indent=1, default=str))
        self._save_state(status="interrupted")
        self.emit("run_interrupted", dir=str(c["dir"]))
        self.ctx = None
        prevent_sleep(False)
        self.set_state("PREVIEW" if self.dev is not None else "DISCONNECTED")

    # ---------------------------------------------------------------- run journal / resume
    def _save_state(self, status=None):
        c = self.ctx
        if c is None:
            return
        if status is not None:
            c["status"] = status
        st = dict(app=f"LightKeeper {VERSION}", status=c.get("status", "open"), phase=self.state, run_dir=str(c["dir"]),
                  serial=c["meta"].get("device", {}).get("serial", ""), planned_start_local=c["t_start"].isoformat(),
                  planned_stop_local=c["t_stop"].isoformat(), segment=c["segment"], last_slot_wall_ns=c["last_slot_ns"],
                  last_slot_local=iso_local(c["last_slot_ns"]) if c["last_slot_ns"] else None, n_slots=c["n_slots"],
                  n_sat=c["n_sat"], burst_id=c["burst_id"], dark_session=c["dark_session"], dark_sets=c["dark_sets"],
                  pre_done=c["pre_done"], uncovered=c["uncovered"], int_us=int(self.dev.int_us) if self.dev else None,
                  used_us=sorted(c["used_us"]), rows=c["writer"].rows, files=c["writer"].files,
                  updated_local=iso_local(wall_ns()))
        try:
            write_json_atomic(c["dir"] / "run_state.json", st)
        except Exception as e:  # never let bookkeeping stop acquisition
            self.emit("error", text=f"run_state.json not written: {e!r}")

    def _load_darks(self, run_dir: Path, sets: list) -> dict:
        """Mean dark per integration time from the most recent COMPLETE dark session in the run's HDF5 files."""
        done = [d for d in sets if d.get("complete")]
        if not done:
            return {}
        sid = done[-1]["session"]; acc: dict[int, list] = {}
        for f in sorted(run_dir.glob("spectra_*.h5")):
            try:
                with h5py.File(f, "r") as h:
                    if "dark_scans" not in h:
                        continue
                    g = h["dark_scans"]; s = g["session"][:]; m = s == sid
                    if m.any():
                        us = g["int_us"][:][m]; sp = g["spectrum"][np.where(m)[0]]
                        for u, y in zip(us, sp):
                            acc.setdefault(int(u), []).append(y)
            except Exception as e:
                self.emit("error", text=f"reading darks from {f.name}: {e!r}")
        return {u: np.mean(v, 0) for u, v in acc.items()}

    def _c_resume(self, run_dir):
        run_dir = Path(run_dir)
        if self.dev is None or self.ctx is not None:
            self.emit("resume_refused", text="connect the spectrometer first (and no other run may be active)"); return
        meta = read_json(run_dir / "run_metadata.json"); st = read_json(run_dir / "run_state.json")
        if not meta or not st:
            self.emit("resume_refused", text=f"{run_dir}: run_metadata.json or run_state.json missing (runs from LightKeeper "
                                             f"0.1.x cannot be resumed)"); return
        if st.get("status") == "closed":
            self.emit("resume_refused", text=f"{run_dir.name} is already closed"); return
        plan = RunPlan.from_dict(meta["plan"])
        t_start = dt.datetime.fromisoformat(meta["planned_start_local"]); t_stop = dt.datetime.fromisoformat(meta["planned_stop_local"])
        info = self.dev.info(); lad = [int(u) for u in meta["ladder_us"]]
        if str(info.get("serial")) != str(meta.get("device", {}).get("serial")):
            self.emit("error", text=f"resume: device serial {info.get('serial')} differs from the run's {meta['device'].get('serial')}")
        attrs = dict(app=meta["app"], serial=info.get("serial", ""), model=info.get("model", ""), label=plan.label,
                     notes=plan.notes, geometry=plan.geometry, edc=str(info.get("electric_dark_correction")),
                     nlc=str(info.get("nonlinearity_correction")), format_version=FORMAT_VERSION,
                     **{k: getattr(plan, k) for k in SETUP_FIELDS})
        files = sorted(str(f) for f in run_dir.glob("spectra_*.h5"))
        w = Writer(run_dir, self.dev.wavelengths, attrs, plan.rotate_min, plan.flush_s, files=files, rows=st.get("rows"))
        repairs = w.repair(); w.notes.clear()
        now = now_local(); seg = int(st.get("segment", 0)) + 1
        last = st.get("last_slot_wall_ns")
        gap_s = (wall_ns() - int(last)) / 1e9 if last else None
        self.ctx = dict(plan=plan, dir=run_dir, meta=meta, events=Events(run_dir / "events.csv"), writer=w,
                        t_start=t_start, t_stop=t_stop, ladder=lad, used_us=set(st.get("used_us", [])),
                        n_slots=int(st.get("n_slots", 0)), n_sat=int(st.get("n_sat", 0)), burst_id=int(st.get("burst_id", 0)),
                        dark_session=int(st.get("dark_session", 0)), bursts=[b for b in burst_times(plan, t_start, t_stop) if b > now],
                        stop_req=False, segment=seg, dark_sets=list(st.get("dark_sets", [])), pre_done=bool(st.get("pre_done")),
                        last_slot_ns=int(last) if last else None, uncovered=bool(st.get("uncovered")), status="open")
        meta.setdefault("resumes", []).append(dict(segment=seg, resumed_local=iso_local(wall_ns()), previous_phase=st.get("phase"),
                                                   previous_status=st.get("status"), last_slot_local=st.get("last_slot_local"),
                                                   gap_since_last_slot_s=gap_s, app=f"LightKeeper {VERSION}"))
        (run_dir / "run_metadata.json").write_text(json.dumps(meta, indent=1, default=str))
        self.auto = plan.auto_exposure
        self.emit("run", dir=str(run_dir), plan=plan.to_dict(), ladder_us=lad, t_start=t_start.isoformat(), t_stop=t_stop.isoformat(),
                  resumed=True, segment=seg)
        self.log("resumed", f"segment {seg}; previous phase {st.get('phase')} ({st.get('status')}); last slot {st.get('last_slot_local')}"
                            + (f"; gap {gap_s:.0f} s" if gap_s is not None else ""))
        for r in repairs:
            self.log("storage_repair", r)
        prevent_sleep(True)
        self.darks = self._load_darks(run_dir, self.ctx["dark_sets"])
        if self.darks:
            self.emit("darks", darks={k: v.astype(np.float32) for k, v in self.darks.items()})
        self._set_int(int(st.get("int_us") or plan.int_ms * 1000), "resume")
        phase = st.get("phase")
        ended = phase == "POST_RUN" or any(d.get("label") == "post" for d in self.ctx["dark_sets"])
        if ended:
            self.log("resume_post_run", "recording had already ended; post-run dark set still needed")
            self._end_recording(); return
        if now >= t_stop:
            if "recording_end_local" not in meta and last:
                meta["recording_end_local"] = iso_local(int(last))
            self.log("resume_after_stop", "scheduled stop passed while interrupted")
            self._end_recording(); return
        if plan.pre_darks and not self.ctx["pre_done"]:
            self.log("warning", "pre-run dark set was not completed before the interruption; rely on the post-run set")
        if not self.ctx["uncovered"] and self.ctx["pre_done"]:
            self.set_state("AWAIT_UNCOVER", "resumed: uncover the input, then Continue"); return
        if now < t_start:
            self.set_state("STANDBY", f"resumed; waiting for {t_start:%H:%M:%S}")
        else:
            self._begin_recording()

    # ---------------------------------------------------------------- darks
    def _dark_set(self, label):
        c = self.ctx; c["dark_session"] += 1; sid = c["dark_session"]
        c["dark_sets"].append(dict(session=sid, label=label, complete=False, start_local=iso_local(wall_ns())))
        restore = self.dev.int_us
        lad = c["ladder"]
        self.set_state("DARK", f"{label}-run darks: {len(lad)} integration times")
        self.log("dark_start", f"session {sid} ({label}); {len(lad)} integration times {[u / 1000 for u in lad]} ms")
        if self.dev.simulated:
            self.dev.mode = "dark"
        total = sum(dark_scan_count(u, c["plan"].dark_s_per_int) for u in lad); done = 0
        for us in lad:
            got = self._set_int(us, "dark set", log=False)
            n = dark_scan_count(got, c["plan"].dark_s_per_int); acc = 0
            for _ in range(n):
                y = self._scan(); acc = acc + y; done += 1
                c["writer"].add("dark_scans", dict(t_wall_ns=np.int64(wall_ns()), int_us=np.int32(got), session=np.int32(sid),
                                                   label=label, spectrum=y.astype(np.float32)))
                self.emit_scan(y)
                self.emit("progress", what=f"{label}-run darks {got / 1000:g} ms", frac=done / total)
            self.darks[got] = acc / n
            c["writer"].maybe_flush(force=True)
        if self.dev.simulated:
            self.dev.mode = "sky"
        self.emit("darks", darks={k: v.astype(np.float32) for k, v in self.darks.items()})
        c["dark_sets"][-1].update(complete=True, n_scans=done, end_local=iso_local(wall_ns()))
        if label == "pre":
            c["pre_done"] = True
        self.log("dark_end", f"session {sid} ({label}), {done} scans")
        self._set_int(restore, "after darks", log=False)

    # ---------------------------------------------------------------- recording
    def _begin_recording(self):
        c = self.ctx
        slot_ns = int(c["plan"].slot_s * 1e9)
        m0 = mono_ns(); w0 = wall_ns()
        c.update(slot_ns=slot_ns, m0=m0 + (slot_ns - (w0 % slot_ns)), k=0, overhead_ns=2e6)
        c["used_us"].add(self.dev.int_us)
        c["uncovered"] = True
        self.log("recording_start", f"segment {c['segment']}; slot {c['plan'].slot_s} s, {len(c['bursts'])} bursts scheduled")
        self.set_state("RECORDING")

    def _record_slot(self):
        c = self.ctx; w = c["writer"]; p = c["plan"]
        if c["stop_req"] or now_local() >= c["t_stop"]:
            self.log("recording_stop", "stop requested" if c["stop_req"] else "scheduled stop time")
            self._end_recording(); return
        if c["bursts"] and now_local() >= c["bursts"][0]:
            c["bursts"].pop(0); self._burst(); return
        s0 = c["m0"] + c["k"] * c["slot_ns"]; s1 = s0 + c["slot_ns"]
        while mono_ns() < s0:
            time.sleep(max(0.0, min(0.002, (s0 - mono_ns()) / 1e9)))   # clamp: the clock is read twice (0.1.0 bug: negative sleep)
        win = self._win(); us = self.dev.int_us
        acc = 0.0; acc2 = 0.0; n = 0; bmax = 0.0; nsat = 0; tw0 = wall_ns()
        while True:
            if n > 0 and mono_ns() + us * 1e3 + c["overhead_ns"] > s1:
                break
            ts = mono_ns(); y = self._scan()
            c["overhead_ns"] = 0.8 * c["overhead_ns"] + 0.2 * max(0.0, (mono_ns() - ts) - us * 1e3)
            acc = acc + y; acc2 = acc2 + y * y; n += 1
            m = float(despiked(y[win]).max()); bmax = max(bmax, m); nsat += int(m >= SAT_LEVEL)
            self.emit_scan(y)
        tw1 = wall_ns()
        mean = acc / n
        sd = np.sqrt(np.maximum(acc2 / n - mean ** 2, 0) * n / (n - 1)) if n > 1 else np.full(len(mean), np.nan)
        p995 = float(np.percentile(despiked(mean[win]), 99.5))
        c["last_p995"] = p995; c["last_p995_us"] = us
        row = dict(t_wall_ns=np.int64((tw0 + tw1) // 2), t_start_wall_ns=np.int64(tw0), t_end_wall_ns=np.int64(tw1),
                   slot_index=np.int64(c["k"]), segment=np.int16(c["segment"]), int_us=np.int32(us), n_scans=np.int16(n), max_raw_window=np.float32(bmax),
                   p995_mean_window=np.float32(p995), n_saturated_scans=np.int16(nsat),
                   overrun_ms=np.float32(max(0, mono_ns() - s1) / 1e6), mean=mean.astype(np.float32))
        if p.store_sd:
            row["sd"] = sd.astype(np.float32)
        w.add("blocks", row)
        c["n_slots"] += 1; c["n_sat"] += int(nsat > 0); c["last_slot_ns"] = int(row["t_wall_ns"])
        self.emit("slot", t_ns=int(row["t_wall_ns"]), int_us=us, n=n, bmax=bmax, p995=p995, nsat=nsat,
                  mean=mean.astype(np.float32), n_slots=c["n_slots"], n_sat_slots=c["n_sat"], size_mb=w.size_mb(),
                  next_burst=c["bursts"][0].isoformat() if c["bursts"] else "")
        if self.auto:
            self._auto_exposure(bmax, p995)
        c["k"] = max(c["k"] + 1, int((mono_ns() - c["m0"]) // c["slot_ns"]))
        if mono_ns() - c.get("clock_m", 0) > CLOCK_EVERY_NS:
            self._clock_check("hourly")
        self._drain_clock()
        if w.maybe_flush():
            while w.notes:
                self.log("storage", w.notes.pop(0))
            self._save_state()

    def _burst(self):
        c = self.ctx; p = c["plan"]; w = c["writer"]; c["burst_id"] += 1; bid = c["burst_id"]
        restore = self.dev.int_us
        us = max(self.dev.min_us, min(restore, int(p.burst_max_int_ms * 1000)))
        us = max([v for v in c["ladder"] if v <= us] or [c["ladder"][0]])
        if c.get("last_p995") is not None:
            pred = c["last_p995"] * us / c["last_p995_us"]
            if pred < BURST_MIN_FILL * SAT_LEVEL:
                self.log("burst_skipped", f"#{bid}, predicted p99.5 {pred:.0f} counts at {us / 1000:g} ms "
                                          f"(< {BURST_MIN_FILL * SAT_LEVEL:.0f})")
                c["k"] = int((mono_ns() - c["m0"]) // c["slot_ns"]) + 1
                return
        self._set_int(us, f"burst {bid}", log=False)
        self.set_state("BURST", f"#{bid}: {us / 1000:g} ms, {p.burst_s:g} s")
        self.log("burst_start", f"#{bid}, {us / 1000:g} ms, {p.burst_s:g} s")
        t_end = mono_ns() + int(p.burst_s * 1e9); n = 0; w.maybe_flush(force=True)
        while mono_ns() < t_end and not c["stop_req"]:
            y = self._scan(); n += 1
            w.add("burst", dict(t_wall_ns=np.int64(wall_ns()), int_us=np.int32(us), burst_id=np.int32(bid), spectrum=y.astype(np.float32)))
            self.emit_scan(y)
            if n % 250 == 0:
                w.maybe_flush(force=True)
            self._drain()
        w.maybe_flush(force=True)
        self.log("burst_end", f"#{bid}, {n} scans ({n / p.burst_s:.1f} per s)")
        self._save_state()
        self._set_int(restore, "after burst", log=False)
        c["k"] = int((mono_ns() - c["m0"]) // c["slot_ns"]) + 1
        if self.state == "BURST":
            self.set_state("RECORDING")

    def _clock_check(self, when, sync=False):
        """SNTP offset at Start, hourly and at Stop. Hourly runs in a thread so a slow network never delays a slot."""
        if self.ctx is not None:
            self.ctx["clock_m"] = mono_ns()
        job = lambda: self._clock_q.put((when, clock.sntp_query()))  # noqa: E731
        if sync:
            job(); self._drain_clock()
        else:
            threading.Thread(target=job, daemon=True, name="LightKeeperClock").start()

    def _drain_clock(self):
        c = self.ctx
        while c is not None:
            try:
                when, r = self._clock_q.get_nowait()
            except queue.Empty:
                return
            r = dict(r, when=when, local=iso_local(wall_ns()))
            c["meta"].setdefault("clock_checks", []).append(r)
            w32 = clock.w32time_running(str(c["meta"].get("time_sync_status", "")))
            if r["ok"]:
                c["meta"].update(clock_offset_s=r["offset_s"], clock_rtt_s=r["rtt_s"])
                self.log("clock_check", f"{when}: offset {r['offset_s']:+.3f} s, rtt {r['rtt_s']:.3f} s ({r['server']})")
            else:
                self.log("clock_check_failed", f"{when}: {r['error']}")
            warn = (not w32) or (r["ok"] and abs(r["offset_s"]) > clock.WARN_OFFSET_S)
            self.emit("clock", ok=r["ok"], offset_s=r.get("offset_s"), rtt_s=r.get("rtt_s"), w32time=w32, warn=warn)
            (c["dir"] / "run_metadata.json").write_text(json.dumps(c["meta"], indent=1, default=str))

    def _end_recording(self):
        c = self.ctx
        self._clock_check("stop", sync=True)
        c["writer"].maybe_flush(force=True)
        if "recording_end_local" not in c["meta"] or self.state in ("RECORDING", "BURST"):
            c["meta"]["recording_end_local"] = iso_local(wall_ns())
        c["meta"].update(n_slots=c["n_slots"], n_slots_with_saturation=c["n_sat"],
                         n_bursts=c["burst_id"], integration_times_used_ms=sorted(u / 1000 for u in c["used_us"]))
        (c["dir"] / "run_metadata.json").write_text(json.dumps(c["meta"], indent=1, default=str))
        if c["plan"].post_darks:
            self.set_state("POST_RUN", "cover the input and record post-run darks (or skip)")
            self.emit("need_post_dark")
        else:
            self._finalize("post-run darks not planned")

    def _finalize(self, why):
        c = self.ctx
        if c is None:
            return
        c["writer"].maybe_flush(force=True)
        c["meta"].update(final_local=iso_local(wall_ns()), finalize_reason=why, files=c["writer"].files,
                         rows=c["writer"].rows, dark_sessions=c["dark_session"])
        (c["dir"] / "run_metadata.json").write_text(json.dumps(c["meta"], indent=1, default=str))
        self.log("run_closed", why)
        self._save_state(status="closed")
        self.emit("run_closed", dir=str(c["dir"]), rows=c["writer"].rows)
        self.ctx = None
        prevent_sleep(False)
        self.set_state("PREVIEW" if self.dev is not None else "DISCONNECTED")

    def _shutdown(self):
        prevent_sleep(False)
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
