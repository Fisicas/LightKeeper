"""Process plumbing shared by the window, the background recorder and its supervisor.

* app_home()            %LOCALAPPDATA%\\LightKeeper (override: LIGHTKEEPER_HOME) - session files, open-run registry, logs
* recorder sessions     app_home/recorders/<serial>.json : how a window finds and attaches to a running recorder
* open-run registry     app_home/open_runs/<run folder name>.json : runs that are not finished (resumable)
* logon resume          HKCU\\...\\Run value that restarts the supervisor with --resume after a Windows restart
* watchdog              a scheduled task that resumes an open run whose supervisor and recorder both died (watchdog.py)
* pid_alive()           NEVER os.kill(pid, 0) on Windows: there it terminates the process.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # folder that holds LightKeeper.py
ENTRY = ROOT / "LightKeeper.py"
IS_WIN = sys.platform.startswith("win")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# exit codes of the recorder process (the supervisor restarts only on anything else)
EXIT_DONE = 0          # run closed normally (or cancelled)
EXIT_REFUSED = 2       # nothing to do: run already closed, another recorder holds the device, bad arguments
EXIT_NO_DEVICE = 3     # a NEW run could not open the spectrometer (nothing was created)
EXIT_INTERRUPTED = 10  # user chose "interrupt, resume later": do not restart

WATCHDOG_GRACE_H = 1.0 # after a run's scheduled stop, the watchdog keeps resuming it this long (for the post-run darks)


def app_home() -> Path:
    base = os.environ.get("LIGHTKEEPER_HOME")
    if not base:
        base = str(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LightKeeper")
    p = Path(base)
    for sub in ("recorders", "open_runs", "logs", "pending"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def write_json_atomic(path: Path, obj) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    for _ in range(20):                     # a reader may hold the target open for a moment on Windows
        try:
            os.replace(tmp, path); return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259          # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child still answers kill(0); treat it as dead
    try:
        st = Path(f"/proc/{pid}/stat").read_text().split()
        return st[2] != "Z"
    except Exception:
        return True


def gui_python() -> str:
    """pythonw.exe next to the running interpreter on Windows (no console window); sys.executable elsewhere."""
    exe = Path(os.environ.get("LIGHTKEEPER_PYTHON") or sys.executable)
    if IS_WIN and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return str(exe)


def spawn_detached(args: list[str], log_path: Path | None = None) -> subprocess.Popen:
    """Start a process that outlives the caller (closing the LightKeeper window must not end it)."""
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kw = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
              close_fds=True)
    if IS_WIN:
        base = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:                                    # leave any job object of the launching shell, if allowed
            return subprocess.Popen(args, creationflags=base | 0x01000000, **kw)
        except OSError:
            return subprocess.Popen(args, creationflags=base, **kw)
    return subprocess.Popen(args, start_new_session=True, **kw)


def entry_args(*extra: str) -> list[str]:
    return [gui_python(), "-X", "utf8", str(ENTRY), *extra]


# ---------------------------------------------------------------- recorder sessions
def session_path(serial: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (serial or "UNKNOWN"))
    return app_home() / "recorders" / f"{safe}.json"


def live_sessions() -> list[dict]:
    """Recorder sessions whose process is alive (stale files are left alone; they are overwritten on the next start)."""
    out = []
    for f in sorted((app_home() / "recorders").glob("*.json")):
        s = read_json(f)
        if s and pid_alive(s.get("pid")):
            s["_file"] = str(f); out.append(s)
    return out


# ---------------------------------------------------------------- open-run registry
def register_open_run(run_dir: Path, serial: str) -> None:
    write_json_atomic(app_home() / "open_runs" / f"{Path(run_dir).name}.json",
                      dict(run_dir=str(run_dir), serial=serial, registered=time.strftime("%Y-%m-%dT%H:%M:%S")))


def unregister_open_run(run_dir: Path) -> None:
    try:
        (app_home() / "open_runs" / f"{Path(run_dir).name}.json").unlink()
    except FileNotFoundError:
        pass


def open_runs() -> list[dict]:
    """Registered runs whose run_state.json still says 'open' or 'interrupted'."""
    out = []
    for f in sorted((app_home() / "open_runs").glob("*.json")):
        r = read_json(f)
        if not r:
            continue
        st = read_json(Path(r["run_dir"]) / "run_state.json")
        if st and st.get("status") in ("open", "interrupted"):
            r["state"] = st; out.append(r)
    return out


def supervisor_alive(run_dir: Path) -> bool:
    s = read_json(Path(run_dir) / "supervisor.json")
    return bool(s and pid_alive(s.get("pid")))


# ---------------------------------------------------------------- resume at Windows logon
def _run_value(run_dir: Path) -> str:
    return "LightKeeperResume_" + "".join(c if c.isalnum() else "_" for c in Path(run_dir).name)[-60:]


def set_logon_resume(run_dir: Path, on: bool) -> str:
    """Add/remove an HKCU Run entry that restarts the supervisor with --resume at the next logon. Returns a note."""
    if not IS_WIN:
        return "logon resume: not Windows, skipped"
    import winreg
    name = _run_value(run_dir)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                cmd = " ".join(f'"{a}"' for a in entry_args("--supervisor", "--resume", str(run_dir)))
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
                return f"logon resume set (HKCU Run\\{name})"
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
            return f"logon resume removed (HKCU Run\\{name})"
    except OSError as e:
        return f"logon resume: registry error {e!r}"


# ---------------------------------------------------------------- watchdog (0.3.11)
def watchdog_off(run_dir: Path, why: str) -> None:
    """The watchdog must leave this run alone until someone resumes it by hand (the supervisor's start clears this)."""
    write_json_atomic(Path(run_dir) / "watchdog_off.json", dict(why=why, local=dt.datetime.now().astimezone().isoformat(timespec="seconds")))


def watchdog_clear(run_dir: Path) -> None:
    try:
        (Path(run_dir) / "watchdog_off.json").unlink()
    except FileNotFoundError:
        pass


def needs_watching(r: dict, now: dt.datetime | None = None) -> bool:
    """An entry of open_runs() the watchdog should keep alive: open, not switched off, not long past its scheduled stop."""
    st = r["state"]
    if st.get("status") != "open" or (Path(r["run_dir"]) / "watchdog_off.json").exists():
        return False
    try:
        stop = dt.datetime.fromisoformat(st["planned_stop_local"]).astimezone()
    except (KeyError, TypeError, ValueError):
        return False
    return (now or dt.datetime.now().astimezone()) <= stop + dt.timedelta(hours=WATCHDOG_GRACE_H)


def watched_runs() -> list[dict]:
    return [r for r in open_runs() if needs_watching(r)]


def set_window_holds(on: bool) -> None:
    """A window says it has the spectrometer open itself; the watchdog must not start a recorder that would fight for it."""
    f = app_home() / f"window_device_{os.getpid()}.json"
    if on:
        write_json_atomic(f, dict(pid=os.getpid()))
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def window_holds_device() -> bool:
    return any(pid_alive((read_json(f) or {}).get("pid")) for f in app_home().glob("window_device_*.json"))


_supervisor_lock = None


def supervisor_lock(serial: str) -> bool:
    """One supervisor per spectrometer: a named mutex, which Windows releases however this process ends. Kept for life."""
    global _supervisor_lock
    if not IS_WIN:
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    tag = hashlib.sha1(f"{app_home()}|{serial}".lower().encode()).hexdigest()[:16]
    h = k32.CreateMutexW(None, False, f"Local\\LightKeeperSupervisor_{tag}")
    if not h:
        return True                                 # no lock available: the session and supervisor.json checks still apply
    if ctypes.get_last_error() == 183:              # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h)
        return False
    _supervisor_lock = h
    return True


def watchdog_task_name() -> str:
    home = os.environ.get("LIGHTKEEPER_HOME")
    return "LightKeeper Watchdog " + hashlib.sha1(home.lower().encode()).hexdigest()[:8] if home else "LightKeeper Watchdog"


def _watchdog_xml() -> str:
    from xml.sax.saxutils import escape
    home = os.environ.get("LIGHTKEEPER_HOME")
    exe, *args = entry_args("--watchdog", *(["--home", home] if home else []))
    arguments = " ".join(f'"{a}"' if " " in a else a for a in args)
    start = dt.datetime.now().replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Resumes an open LightKeeper run if its recorder and supervisor have died. Removes itself when no run is open.</Description></RegistrationInfo>
  <Triggers><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>false</StartWhenAvailable><Hidden>true</Hidden><ExecutionTimeLimit>PT5M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def set_watchdog_task(on: bool) -> str:
    """Create or remove the scheduled task. Returns a note; a failure never touches the run. A test or portable home
    (LIGHTKEEPER_HOME) gets a task only when LIGHTKEEPER_WATCHDOG=1 asks for one."""
    if not IS_WIN:
        return "watchdog: not Windows, skipped"
    if os.environ.get("LIGHTKEEPER_HOME") and os.environ.get("LIGHTKEEPER_WATCHDOG") != "1":
        return "watchdog: skipped for this LIGHTKEEPER_HOME"
    name = watchdog_task_name()
    kw = dict(capture_output=True, text=True, timeout=20, creationflags=0x08000000)
    try:
        if not on:
            r = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], **kw)
            return f"watchdog task removed ({name})" if r.returncode == 0 else f"watchdog task: nothing to remove ({name})"
        xml = app_home() / "watchdog_task.xml"
        xml.write_text(_watchdog_xml(), encoding="utf-16")
        try:
            r = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(xml), "/F"], **kw)
        finally:
            xml.unlink(missing_ok=True)
        return f"watchdog task set ({name})" if r.returncode == 0 else f"watchdog task NOT set: {(r.stderr or r.stdout).strip()}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"watchdog task: {e!r}"


def sync_watchdog() -> str:
    """After a run closed or was interrupted: the task stays only while another open run needs it."""
    return "watchdog task kept: another open run" if watched_runs() else set_watchdog_task(False)
