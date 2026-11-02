"""LightKeeper background recorder: one process per run, independent of the window.

* owns the spectrometer (opened by serial) and runs the acquisition Engine thread;
* shows the LightKeeper icon in the Windows notification area while the run exists (badge colour = state) with a menu
  for the steps that need a person (Continue after uncovering, post-run darks, Stop, Interrupt) and balloon messages;
* serves any number of LightKeeper windows on 127.0.0.1 (authkey in the session file under %LOCALAPPDATA%\\LightKeeper),
  so closing or crashing a window never touches the recording;
* exits when the run is closed (code 0) or interrupted for a later resume (code 10). Any other exit is treated by the
  supervisor as a crash and the run is resumed from run_state.json.

Start:   LightKeeper.py --recorder --plan <plan.json> --serial <serial> [--device-id N]
Resume:  LightKeeper.py --recorder --resume <run folder>
(Normally started through the supervisor: LightKeeper.py --supervisor ..., see supervisor.py.)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import faulthandler
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from multiprocessing.connection import Listener
from pathlib import Path

from PySide6 import QtCore, QtWidgets as Q

from . import APP_NAME, VERSION
from .devices import shutdown_sdk
from .engine import Engine
from .icons import state_icon
from .link import RUN_COMMANDS
from .procs import (EXIT_DONE, EXIT_INTERRUPTED, EXIT_NO_DEVICE, EXIT_REFUSED, app_home, entry_args, pid_alive, read_json,
                    register_open_run, session_path, set_logon_resume, spawn_detached, unregister_open_run, write_json_atomic)
from .schedule import RunPlan
from .storage import LOCAL_TZ

HISTORY_RECENT = 900          # slot events kept in full for a window that attaches (last ~15 min at 1 s slots)
POST_DARK_REMIND_S = 900     # repeat the post-run dark reminder every 15 min until the darks are taken or skipped
HISTORY_SPARSE = 2400         # older slots kept every N-th (N doubles as needed) so a late window still sees the day
LABELS = {"RECORDING": "recording", "BURST": "fast burst", "STANDBY": "armed, waiting for the start time",
          "DARK": "dark set (input covered)", "AWAIT_UNCOVER": "waiting: uncover the input, then Continue",
          "POST_RUN": "recording finished: post-run darks needed", "WAITING_DEVICE": "waiting for the spectrometer",
          "PREVIEW": "closing", "IDLE": "connected", "DISCONNECTED": "starting"}


class Hub:
    """Local server. Each window gets its own sender thread and bounded queue: a frozen window is dropped, never waited for."""

    def __init__(self, on_command, snapshot):
        self.authkey = secrets.token_bytes(32)
        self.listener = Listener(("127.0.0.1", 0), authkey=self.authkey)
        self.port = self.listener.address[1]
        self.on_command, self.snapshot = on_command, snapshot
        self.lock = threading.Lock()
        self.clients: list[tuple] = []          # (conn, queue)
        self._closing = False
        threading.Thread(target=self._accept, name="hub-accept", daemon=True).start()

    def _accept(self):
        while not self._closing:
            try:
                conn = self.listener.accept()
            except Exception:
                if self._closing:
                    return
                time.sleep(0.2); continue
            threading.Thread(target=self._serve, args=(conn,), name="hub-client", daemon=True).start()

    def _serve(self, conn):
        q: queue.Queue = queue.Queue(maxsize=2000)
        try:
            if not conn.poll(10):
                return
            hello = conn.recv()
            if not (isinstance(hello, tuple) and hello and hello[0] == "hello"):
                return
            with self.lock:                     # snapshot + registration atomic w.r.t. broadcast
                conn.send(self.snapshot())
                self.clients.append((conn, q))
            threading.Thread(target=self._sender, args=(conn, q), name="hub-send", daemon=True).start()
            while True:
                msg = conn.recv()
                if isinstance(msg, tuple) and len(msg) == 3 and msg[0] == "cmd" and msg[1] in RUN_COMMANDS:
                    self.on_command(msg[1], tuple(msg[2]))
        except (EOFError, OSError, ConnectionError):
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self._drop(conn)

    def _sender(self, conn, q):
        try:
            while True:
                e = q.get()
                if e is None:
                    return
                conn.send(e)
        except Exception:
            self._drop(conn)

    def _drop(self, conn):
        with self.lock:
            for c in list(self.clients):
                if c[0] is conn:
                    self.clients.remove(c)
                    try:
                        c[1].put_nowait(None)
                    except queue.Full:
                        pass
        try:
            conn.close()
        except Exception:
            pass

    def broadcast(self, e):                     # caller holds self.lock
        for conn, q in list(self.clients):
            try:
                q.put_nowait(e)
            except queue.Full:                  # that window stopped reading: disconnect it
                threading.Thread(target=self._drop, args=(conn,), daemon=True).start()

    @property
    def n_clients(self):
        return len(self.clients)

    def close(self):
        self._closing = True
        try:
            self.listener.close()
        except Exception:
            pass
        with self.lock:
            for conn, q in self.clients:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass


class Recorder(QtCore.QObject):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.exit_code = None
        self.plan = None; self.run_dir = Path(args.resume) if args.resume else None
        if args.resume:
            meta = read_json(self.run_dir / "run_metadata.json") or {}
            st = read_json(self.run_dir / "run_state.json") or {}
            self.plan = RunPlan.from_dict(meta.get("plan", {}))
            self.serial = args.serial or st.get("serial") or meta.get("device", {}).get("serial", "")
            self.run_status = st.get("status")
        else:
            self.plan = RunPlan.from_dict(read_json(Path(args.plan)) or {})
            self.serial = args.serial
            self.run_status = None
        self.state, self.detail = "DISCONNECTED", "starting"
        self.device = None; self.run = None; self.darks = None; self.hot = None; self.scan = None; self.int_us = None
        self.counters = dict(bursts=0, dark_sets=0)
        self.recent = collections.deque(maxlen=HISTORY_RECENT); self.sparse = []; self.sparse_stride = 1; self.n_hist = 0
        self.logs = collections.deque(maxlen=500)
        self.last_slot = None; self._last_scan_tx = 0.0; self._last_err_note = 0.0; self._start_sent = False
        self._run_requested = False   # 0.3.2: start/resume sent; never reconnect after this
        self._connect_deadline = time.time() + 60; self._next_connect = 0.0; self._connecting = False
        self.engine = Engine()
        self.hub = Hub(self._command, self._snapshot)
        self._write_session()
        self._tray()
        self.engine.start()
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self._pump); self.timer.start(40)
        self.slow = QtCore.QTimer(self); self.slow.timeout.connect(self._tick); self.slow.start(1000)
        self._set_state("WAITING_DEVICE", f"opening {self.serial}")
        self._try_connect()

    # ---------------------------------------------------------------- session file / bookkeeping
    def _write_session(self):
        write_json_atomic(session_path(self.serial), dict(
            pid=os.getpid(), port=self.hub.port, authkey=self.hub.authkey.hex(), serial=self.serial, version=VERSION,
            run_dir=str(self.run_dir) if self.run_dir else None, started=dt.datetime.now(LOCAL_TZ).isoformat(),
            supervisor_pid=os.environ.get("LIGHTKEEPER_SUPERVISOR_PID")))

    def _remove_session(self):
        s = read_json(session_path(self.serial))
        if s and int(s.get("pid", -1)) == os.getpid():
            try:
                session_path(self.serial).unlink()
            except FileNotFoundError:
                pass

    # ---------------------------------------------------------------- tray
    def _tray(self):
        self.tray = Q.QSystemTrayIcon(state_icon("WAITING_DEVICE"))
        m = Q.QMenu()
        self.a_title = m.addAction(APP_NAME); self.a_title.setEnabled(False)
        self.a_detail = m.addAction(""); self.a_detail.setEnabled(False)
        m.addSeparator()
        m.addAction("Open LightKeeper window", self._open_window)
        self.a_folder = m.addAction("Open run folder", lambda: self.run_dir and os.startfile(str(self.run_dir)))
        m.addSeparator()
        self.a_continue = m.addAction("Continue (input uncovered)", lambda: self._confirm_cmd(
            "continue", "Uncover the input", "Remove the cover WITHOUT moving the fiber/diffuser, then press Continue.", "Continue"))
        self.a_post = m.addAction("Record post-run darks…", lambda: self._confirm_cmd(
            "post_dark", "Post-run dark set", "Cover the input completely, then press Record. Keep it covered until the tray icon "
            "shows the run is closed.", "Input is covered - record"))
        self.a_skip = m.addAction("Skip post-run darks…", lambda: self._confirm_cmd(
            "finish", "Skip post-run darks", "Close the run WITHOUT a post-run dark set?", "Close without darks"))
        m.addSeparator()
        self.a_stop = m.addAction("Stop run…", lambda: self._confirm_cmd(
            "stop", "Stop run", "Stop the run now? Recording ends after the current slot; the post-run dark set follows.", "Stop run"))
        self.a_int = m.addAction("Interrupt, resume later…", lambda: self._confirm_cmd(
            "interrupt", "Interrupt run", "Stop acquiring and release the spectrometer, but keep the run OPEN.\n\nResume it later from "
            "the LightKeeper window (it continues as a new segment until the scheduled stop, then asks for post-run darks).",
            "Interrupt"))
        self.tray.setContextMenu(m); self._menu = m
        self.tray.activated.connect(lambda r: self._open_window() if r == Q.QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if Q.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._refresh_tray()

    def _open_window(self):
        try:
            spawn_detached(entry_args())
        except Exception as e:
            self._note("Could not open the window", repr(e), "warning")

    def _confirm_cmd(self, cmd, title, text, ok):
        box = Q.QMessageBox(Q.QMessageBox.Icon.Question, f"{APP_NAME} · {title}", text)
        b = box.addButton(ok, Q.QMessageBox.ButtonRole.AcceptRole); box.addButton("Cancel", Q.QMessageBox.ButtonRole.RejectRole)
        box.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        box.exec()
        if box.clickedButton() is b:
            self._command(cmd, ())

    def _note(self, title, text, level="info", force=False):
        if not self.tray.isVisible():
            return
        if level != "info" and not force and time.time() - self._last_err_note < 300:
            return
        if level != "info":
            self._last_err_note = time.time()
        icon = {"info": Q.QSystemTrayIcon.MessageIcon.Information, "warning": Q.QSystemTrayIcon.MessageIcon.Warning,
                "error": Q.QSystemTrayIcon.MessageIcon.Critical}[level]
        self.tray.showMessage(f"{APP_NAME} · {title}", text, icon, 12000)

    def _refresh_tray(self):
        s = self.state
        run = self.run_dir.name if self.run_dir else "(run not created yet)"
        stop = ""
        if self.run:
            t1 = dt.datetime.fromisoformat(self.run["t_stop"])
            t0 = dt.datetime.fromisoformat(self.run["t_start"])
            stop = f"start {t0:%H:%M} · stop {t1:%a %H:%M}" if s == "STANDBY" else f"stop {t1:%a %H:%M}"
        slots = f"{self.last_slot['n_slots']:,} slots" if self.last_slot else ""
        self.a_title.setText(f"{APP_NAME} · {LABELS.get(s, s)}")
        self.a_detail.setText("  ·  ".join(x for x in (run, stop, slots) if x))
        tip = f"{APP_NAME} · {LABELS.get(s, s)}\n{run}" + (f"\n{stop}" if stop else "") + (f"\n{slots}" if slots else "")
        self.tray.setToolTip(tip[:127])
        self.tray.setIcon(state_icon(s))
        have_run = self.run is not None
        self.a_folder.setEnabled(self.run_dir is not None)
        self.a_continue.setVisible(s == "AWAIT_UNCOVER")
        self.a_post.setVisible(s == "POST_RUN"); self.a_skip.setVisible(s == "POST_RUN")
        self.a_stop.setEnabled(s in ("STANDBY", "RECORDING", "BURST", "AWAIT_UNCOVER"))
        self.a_int.setEnabled(have_run and s in ("STANDBY", "RECORDING", "BURST", "AWAIT_UNCOVER", "POST_RUN"))

    # ---------------------------------------------------------------- commands from windows / tray
    def _command(self, name, args):
        if name == "interrupt":
            self.engine.send("interrupt")
        else:
            self.engine.send(name, *args)

    # ---------------------------------------------------------------- device connection
    def _try_connect(self):
        self._connecting = True
        self.engine.send("connect_serial", self.serial, self.plan.edc, self.plan.nlc, self.args.device_id)

    def _tick(self):
        if self._connecting is False and self.state == "WAITING_DEVICE" and self.device is None and not self._run_requested \
                and time.time() >= self._next_connect:
            self._try_connect()
        if self.state in ("RECORDING", "BURST", "STANDBY"):
            self._refresh_tray()
        since = getattr(self, "_post_since", None)
        if self.state == "POST_RUN" and since and time.time() - self._post_last >= POST_DARK_REMIND_S:
            self._post_last = time.time()
            self._note("Post-run darks still needed", f"The run ended {(time.time() - since) / 60:.0f} min ago. Cover the input, "
                       "then choose 'Record post-run darks…' in this icon's menu.", "warning", force=True)
            Q.QApplication.beep()
        elif self.state != "POST_RUN":
            self._post_since = None

    # ---------------------------------------------------------------- snapshot for a window that attaches
    def _snapshot(self):
        return dict(type="snapshot", pid=os.getpid(), serial=self.serial, version=VERSION, state=self.state, detail=self.detail,
                    device=self.device, run=self.run, darks=self.darks, hot=self.hot, scan=self.scan, int_us=self.int_us,
                    counters=dict(self.counters), history=list(self.sparse) + list(self.recent), sparse_stride=self.sparse_stride,
                    logs=list(self.logs), last_slot=self.last_slot)

    def _keep_history(self, e):
        self.n_hist += 1
        if len(self.recent) == self.recent.maxlen:
            old = self.recent[0]
            if old.get("_k", 0) % self.sparse_stride == 0:
                self.sparse.append(old)
                if len(self.sparse) > HISTORY_SPARSE:
                    self.sparse = self.sparse[::2]; self.sparse_stride *= 2
        e["_k"] = self.n_hist
        self.recent.append(e)

    def _set_state(self, s, detail=""):
        self.state, self.detail = s, detail
        self._refresh_tray()

    # ---------------------------------------------------------------- engine events
    def _pump(self):
        for _ in range(400):
            try:
                e = self.engine.evt.get_nowait()
            except queue.Empty:
                break
            t = e["type"]
            with self.hub.lock:
                fwd = self._apply(e, t)
                if fwd:
                    self.hub.broadcast(e)

    def _apply(self, e, t) -> bool:
        if t == "scan":
            self.scan = dict(y=e["y"], int_us=e["int_us"], t_ns=e["t_ns"])
            now = time.time()
            if now - self._last_scan_tx < 0.2:
                return False
            self._last_scan_tx = now
            return True
        if t == "slot":
            self.last_slot = {k: v for k, v in e.items() if k != "mean"}
            self._keep_history(dict(e))
            return True
        if t == "log":
            self.logs.append(e["text"]); print(e["text"], flush=True)
            if "burst_start" in e["text"]:
                self.counters["bursts"] += 1
            return True
        if t == "int":
            self.int_us = e["int_us"]; return True
        if t == "darks":
            self.darks = e["darks"]; self.counters["dark_sets"] += 1; return True
        if t == "hot_pixels":
            self.hot = {k: v for k, v in e.items() if k != "type"}; return True
        if t == "device":
            self.device = dict(info=e["info"], wavelengths=e["wavelengths"]); self._connecting = False
            print(f"device: {e['info']}", flush=True)
            self._run_requested = True
            if self.args.resume:
                self.engine.send("resume", str(self.run_dir))
            elif self.run is None:
                self.engine.send("start", self.plan.to_dict()); self._start_sent = True
            return True
        if t == "connect_failed":
            self._connecting = False
            msg = f"cannot open spectrometer {self.serial}: {e['text']}"
            print(msg, flush=True); self.logs.append(msg)
            if not self.args.resume and time.time() > self._connect_deadline:
                self._note("Run not started", msg, "error", force=True)
                self.hub.broadcast(dict(type="error", text=msg + " - the run was NOT started"))
                QtCore.QTimer.singleShot(1500, lambda: self._exit(EXIT_NO_DEVICE)); return False
            self._next_connect = time.time() + (10 if self.args.resume else 3)
            self.detail = msg
            self.hub.broadcast(dict(type="state", state="WAITING_DEVICE", detail=msg))
            return False
        if t == "state":
            s = e["state"]
            if s in ("DISCONNECTED", "IDLE") and self.run is None and not self._run_requested:
                s = "WAITING_DEVICE"; e = dict(e, state=s)
            prev = self.state
            self._set_state(s, e.get("detail", ""))
            if s == "RECORDING" and prev not in ("RECORDING", "BURST"):
                self._note("Recording", f"{self.run_dir.name if self.run_dir else ''}\nRecording until "
                                        f"{dt.datetime.fromisoformat(self.run['t_stop']):%a %H:%M}." if self.run else "Recording started.")
            if s == "AWAIT_UNCOVER" and self.hub.n_clients == 0:
                self._note("Uncover the input", "Pre-run darks done. Uncover the input without moving it, then choose "
                                                "Continue in this icon's menu (or in the LightKeeper window).", "warning", force=True)
            if s == "STANDBY" and prev != "STANDBY" and self.run:
                self._note("Armed", f"Recording starts {dt.datetime.fromisoformat(self.run['t_start']):%a %H:%M:%S}. "
                                    "You can close the LightKeeper window; this icon keeps the run.")
            return True
        if t == "need_post_dark":
            self._post_since = self._post_last = time.time()
            self._note("Run finished - post-run darks", "Cover the input, then choose 'Record post-run darks…' in this icon's menu "
                                                         "or in the LightKeeper window.", "warning", force=True)
            return True
        if t == "run":
            self.run = dict(dir=e["dir"], plan=e["plan"], ladder_us=e["ladder_us"], t_start=e["t_start"], t_stop=e["t_stop"],
                            resumed=e.get("resumed", False), segment=e.get("segment", 0))
            self.run_dir = Path(e["dir"]); self._write_session()
            register_open_run(self.run_dir, self.serial)
            plan = RunPlan.from_dict(e["plan"])
            note = set_logon_resume(self.run_dir, bool(getattr(plan, "auto_resume_logon", True)))
            print(note, flush=True); self.logs.append(note)
            if e.get("resumed"):
                self._note("Run resumed", f"{self.run_dir.name}: continuing as segment {e.get('segment')}.", "info")
            self._refresh_tray()
            return True
        if t in ("run_closed", "run_interrupted"):
            closed = t == "run_closed"
            if self.run_dir:
                set_logon_resume(self.run_dir, False)
                if closed:
                    unregister_open_run(self.run_dir)
            self._note("Run closed" if closed else "Run interrupted",
                       f"{self.run_dir.name if self.run_dir else ''}\n" + ("All data saved; spectrometer released." if closed else
                                                                           "Data saved. Resume it from the LightKeeper window."))
            QtCore.QTimer.singleShot(2500, lambda: self._exit(EXIT_DONE if closed else EXIT_INTERRUPTED))
            return True
        if t == "resume_refused":
            print("resume refused:", e.get("text"), flush=True)
            self._note("Cannot resume", e.get("text", ""), "error", force=True)
            QtCore.QTimer.singleShot(2500, lambda: self._exit(EXIT_REFUSED))
            return True
        if t == "error":
            print("ERROR", e.get("text"), flush=True); self.logs.append("ERROR " + e.get("text", ""))
            if self._start_sent and self.run is None:          # the engine refused the plan: release the device and leave
                self._note("Run not started", e.get("text", ""), "error", force=True)
                QtCore.QTimer.singleShot(2500, lambda: self._exit(EXIT_REFUSED))
                return True
            if self.state in ("RECORDING", "BURST"):
                self._note("Problem", e.get("text", "")[:200], "warning")
            return True
        return True                                   # progress, lamp_result, ... pass through

    # ---------------------------------------------------------------- exit
    def _exit(self, code):
        if self.exit_code is not None:
            return
        self.exit_code = code
        self.timer.stop(); self.slow.stop()
        self.engine.send("quit"); self.engine.join(20)
        shutdown_sdk()
        self._remove_session()
        self.hub.close()
        self.tray.hide()
        Q.QApplication.instance().exit(code)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="LightKeeper --recorder")
    ap.add_argument("--plan"); ap.add_argument("--resume"); ap.add_argument("--serial", default="")
    ap.add_argument("--device-id", type=int, default=None)
    args = ap.parse_args(argv)
    if bool(args.plan) == bool(args.resume):
        print("give exactly one of --plan or --resume"); return EXIT_REFUSED
    home = app_home()
    log = open(home / "logs" / f"recorder_{dt.datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log", "a", buffering=1, encoding="utf-8")
    if sys.stdout is None or not sys.stdout.isatty():
        sys.stdout = sys.stderr = log
    faulthandler.enable(log)                       # native crashes (e.g. in the vendor SDK) leave a traceback here
    print(f"LightKeeper recorder {VERSION} pid {os.getpid()} args {argv}", flush=True)
    if args.resume:
        st = read_json(Path(args.resume) / "run_state.json")
        if not st or st.get("status") == "closed":
            print("nothing to resume:", st and st.get("status")); return EXIT_REFUSED
    serial = args.serial or (read_json(Path(args.resume) / "run_state.json") or {}).get("serial", "") if args.resume else args.serial
    other = read_json(session_path(serial))
    if other and pid_alive(other.get("pid")) and int(other["pid"]) != os.getpid():
        print(f"another recorder (pid {other['pid']}) holds {serial}; refusing"); return EXIT_REFUSED
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LightKeeper.Recorder")
    except Exception:
        pass
    app = Q.QApplication.instance() or Q.QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    from . import theme
    theme.apply(app)
    rec = Recorder(args)
    code = app.exec()
    code = rec.exit_code if rec.exit_code is not None else code
    print(f"recorder exit {code}", flush=True)
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
