"""Process plumbing shared by the window, the background recorder and its supervisor.

* app_home()            %LOCALAPPDATA%\\LightKeeper (override: LIGHTKEEPER_HOME) - session files, open-run registry, logs
* recorder sessions     app_home/recorders/<serial>.json : how a window finds and attaches to a running recorder
* open-run registry     app_home/open_runs/<location hash>.json : runs that are not finished (resumable)
* watchdog              run-scoped scheduled task for crash recovery and resume after Windows sign-in (watchdog.py)
* pid_alive()           NEVER os.kill(pid, 0) on Windows: there it terminates the process.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from .locations import location_key, remember_path, resolve_path

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
WATCHDOG_RESTART_PHASES = {"STANDBY", "RECORDING", "BURST"}
BOOT_TIME_TOLERANCE_S = 30


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


def pid_alive(pid, *, started: str | None = None) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            if not ok or code.value != 259:                # STILL_ACTIVE
                return False
            if started:
                try:
                    stamp = dt.datetime.fromisoformat(started).timestamp()
                except (OSError, OverflowError, TypeError, ValueError):
                    return False
                fields = [wintypes.FILETIME() for _ in range(4)]
                k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
                if k32.GetProcessTimes(h, *(ctypes.byref(field) for field in fields)):
                    birth = (fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime
                    birth_unix = birth / 10_000_000 - 11_644_473_600
                    if birth_unix > stamp + 1:
                        return False                      # a later process now has the same PID
            return True
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
    exe = Path(os.environ.get("LIGHTKEEPER_PYTHON") or sys.executable).absolute()
    if IS_WIN and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return str(exe)


def spawn_detached(args: list[str], log_path: Path | None = None, *,
                   working_dir: Path | None = None) -> subprocess.Popen:
    """Start a process that outlives the caller (closing the LightKeeper window must not end it)."""
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kw = dict(cwd=str(working_dir if working_dir is not None else ROOT), stdin=subprocess.DEVNULL,
              stdout=out, stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL, close_fds=True)
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
        if s and pid_alive(s.get("pid"), started=s.get("started")):
            s["_file"] = str(f); out.append(s)
    return out


# ---------------------------------------------------------------- open-run registry
def _run_registry_file(location: dict) -> Path:
    tag = hashlib.sha256(location_key(location).encode("utf-8")).hexdigest()[:32]
    return app_home() / "open_runs" / f"location_{tag}.json"


def _remove_legacy_run(run_dir: Path) -> None:
    old = app_home() / "open_runs" / f"{Path(run_dir).name}.json"
    record = read_json(old)
    if isinstance(record, dict) and record.get("run_dir"):
        if location_key(record["run_dir"]) == location_key(run_dir):
            old.unlink(missing_ok=True)


def register_open_run(run_dir: Path, serial: str, *, recovery: dict | None = None) -> Path:
    location = remember_path(run_dir)
    record = dict(run_dir=location["path"], run_location=location, serial=serial,
                  registered=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if recovery is not None:
        record["recovery"] = recovery
    registration = _run_registry_file(location)
    write_json_atomic(registration, record)
    # A previous session may have had only a volume serial, or resolved a
    # different mount path. Retire only records resolving to this same run.
    for old in registration.parent.glob("location_*.json"):
        if old == registration:
            continue
        earlier = read_json(old)
        if isinstance(earlier, dict) and earlier.get("serial") == serial:
            previous = resolve_path(earlier.get("run_location", earlier.get("run_dir")))
            if previous is not None and location_key(previous) == location_key(run_dir):
                old.unlink(missing_ok=True)
    _remove_legacy_run(run_dir)
    return registration


def unregister_open_run(run_dir: Path, *, registration: Path | None = None) -> None:
    # Use the exact local marker that was written, even if recording media are gone.
    marker = registration if registration is not None else _run_registry_file(remember_path(run_dir))
    marker.unlink(missing_ok=True)
    _remove_legacy_run(run_dir)


def open_runs(*, include_unavailable: bool = False) -> list[dict]:
    """Registered open or interrupted runs. An unavailable location has state=None and a path hint only."""
    out = {}
    for f in sorted((app_home() / "open_runs").glob("*.json")):
        r = read_json(f)
        if not isinstance(r, dict) or not r.get("run_dir"):
            continue
        r["registration_id"] = f.stem
        path = resolve_path(r.get("run_location", r["run_dir"]))
        if path is None:
            if include_unavailable:
                r["state"] = None
                out[f.stem] = r
            continue
        st = read_json(path / "run_state.json")
        if not isinstance(st, dict) or st.get("status") not in ("open", "interrupted"):
            continue
        if r.get("serial") and st.get("serial") and str(r["serial"]) != str(st["serial"]):
            continue
        r.update(run_dir=str(path), state=st)
        key = location_key(path)
        if key not in out or "run_location" in r:
            out[key] = r
    return sorted(out.values(), key=lambda r: (r.get("registered") or "", r["run_dir"]))


def supervisor_alive(run_dir: Path) -> bool:
    s = read_json(Path(run_dir) / "supervisor.json")
    return bool(s and pid_alive(s.get("pid"), started=s.get("started")))


# ---------------------------------------------------------------- migration of old startup entries
def _run_value(run_dir: Path) -> str:
    return "LightKeeperResume_" + "".join(c if c.isalnum() else "_" for c in Path(run_dir).name)[-60:]


def remove_legacy_logon_entries(run_dir: Path | None = None) -> None:
    """Remove only startup entries from older LightKeeper versions."""
    if not IS_WIN or os.environ.get("LIGHTKEEPER_HOME"):
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            for i in reversed(range(winreg.QueryInfoKey(key)[1])):
                name, command, _ = winreg.EnumValue(key, i)
                if (name.startswith("LightKeeperResume_")
                        and (run_dir is None or name == _run_value(run_dir))
                        and isinstance(command, str) and "lightkeeper.py" in command.lower()
                        and '"--supervisor" "--resume"' in command):
                    winreg.DeleteValue(key, name)
    except OSError:
        pass


# ---------------------------------------------------------------- watchdog (0.3.11)
def watchdog_off(run_dir: Path, why: str) -> None:
    """The watchdog must leave this run alone until someone resumes it by hand (the supervisor's start clears this)."""
    write_json_atomic(Path(run_dir) / "watchdog_off.json", dict(why=why, local=dt.datetime.now().astimezone().isoformat(timespec="seconds")))


def watchdog_clear(run_dir: Path) -> None:
    try:
        (Path(run_dir) / "watchdog_off.json").unlink()
    except FileNotFoundError:
        pass


def windows_boot_time() -> float | None:
    """Approximate Windows boot time in UTC epoch seconds using the system uptime."""
    if not IS_WIN:
        return None
    import ctypes

    uptime_ms = ctypes.WinDLL("kernel32").GetTickCount64
    uptime_ms.restype = ctypes.c_ulonglong
    return time.time() - uptime_ms() / 1000


@lru_cache(maxsize=1)
def windows_boot_id() -> str | None:
    """Identify the current Windows boot by its Kernel-General OS-start event."""
    if not IS_WIN:
        return None
    from xml.etree import ElementTree

    # Event 12 belongs to the OS boot, so a later wall-clock change leaves its
    # recorded timestamp and record ID alone. The timestamp distinguishes an
    # event whose record ID was reused after clearing the System log.
    query = "*[System[Provider[@Name='Microsoft-Windows-Kernel-General'] and EventID=12]]"
    ns = "{http://schemas.microsoft.com/win/2004/08/events/event}"
    try:
        result = subprocess.run(["wevtutil.exe", "qe", "System", f"/q:{query}", "/c:1", "/rd:true", "/f:xml", "/uni:true"],
                                capture_output=True, timeout=4, creationflags=0x08000000, check=True)
        event = ElementTree.fromstring(result.stdout.decode("utf-16-le").lstrip("\ufeff"))
    except (OSError, subprocess.SubprocessError, UnicodeError, ElementTree.ParseError):
        return None
    number = event.findtext(f"{ns}System/{ns}EventRecordID")
    created = event.find(f"{ns}System/{ns}TimeCreated")
    stamp = created.get("SystemTime") if created is not None else None
    return f"{number}:{stamp}" if number and stamp else None


def _watchdog_reason(r: dict, now: dt.datetime, boot_time: float | None, boot_id: str | None = None) -> str | None:
    """Return the recovery reason, or a pending reason while the data volume is offline."""
    state = r["state"]
    recovery = r.get("recovery") or {}
    run_dir = Path(r["run_dir"])
    if state is not None and (state.get("status") != "open" or (run_dir / "watchdog_off.json").exists()):
        return None
    stop_local = (state or recovery).get("planned_stop_local")
    try:
        stop = dt.datetime.fromisoformat(stop_local).astimezone()
    except (TypeError, ValueError):
        return None
    if now > stop + dt.timedelta(hours=WATCHDOG_GRACE_H):
        return None

    recorded_boot = recovery.get("boot_time")
    recorded_id = recovery.get("boot_id")
    after_restart = False
    if recorded_id and boot_id:
        after_restart = boot_id != recorded_id
    elif boot_time is not None:
        if isinstance(recorded_boot, (int, float)):
            # Allow small wall-clock corrections while Windows remains up.
            after_restart = abs(boot_time - recorded_boot) > BOOT_TIME_TOLERANCE_S
        else:
            # Older registry entries include the local time at which the recorder registered.
            try:
                after_restart = dt.datetime.fromisoformat(r["registered"]).timestamp() < boot_time - 1
            except (KeyError, TypeError, ValueError):
                return None
    if after_restart:
        if now >= stop or (state is not None and state.get("phase") not in WATCHDOG_RESTART_PHASES):
            return None
        allow = recovery.get("auto_resume_logon")
        if allow is None and state is not None:
            meta = read_json(run_dir / "run_metadata.json") or {}
            allow = meta.get("plan", {}).get("auto_resume_logon", True) if meta.get("plan") else False
        return "windows_restart" if allow is True else None
    return "watchdog"


def needs_watching(r: dict, now: dt.datetime | None = None) -> bool:
    """Whether the watchdog can bring an accessible run back."""
    if r["state"] is None:
        return False
    boot_id = windows_boot_id() if (r.get("recovery") or {}).get("boot_id") else None
    return _watchdog_reason(r, now or dt.datetime.now().astimezone(), windows_boot_time(), boot_id) is not None


def watchdog_candidates(*, include_unavailable: bool = False) -> list[dict]:
    runs = open_runs(include_unavailable=include_unavailable)
    if not runs:
        return []
    now = dt.datetime.now().astimezone()
    boot = windows_boot_time()
    boot_id = windows_boot_id() if any((r.get("recovery") or {}).get("boot_id") for r in runs) else None
    out = []
    for run in runs:
        reason = _watchdog_reason(run, now, boot, boot_id)
        if reason:
            out.append(dict(run, resume_source=reason))
    return out


def watched_runs() -> list[dict]:
    return watchdog_candidates()


def set_window_holds(on: bool) -> None:
    """A window says it has the spectrometer open itself; the watchdog must not start a recorder that would fight for it."""
    f = app_home() / f"window_device_{os.getpid()}.json"
    if on:
        write_json_atomic(f, dict(pid=os.getpid(), started=dt.datetime.now().astimezone().isoformat()))
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def window_holds_device() -> bool:
    for f in app_home().glob("window_device_*.json"):
        record = read_json(f) or {}
        if not record.get("started"):
            try:
                record["started"] = dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat()
            except OSError:
                continue
        if pid_alive(record.get("pid"), started=record["started"]):
            return True
    return False


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


def _windows_user_sid() -> str:
    import csv
    import re

    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True,
                            text=True, errors="replace", timeout=10, creationflags=0x08000000)
    sid = next((row[-1] for row in csv.reader(result.stdout.splitlines())
                if row and re.fullmatch(r"S-\d+(?:-\d+)+", row[-1])), "")
    if not sid:
        raise OSError("Cannot identify the Windows account for LightKeeper recovery")
    return sid


def _watchdog_xml(user_sid: str) -> str:
    from xml.sax.saxutils import escape
    home = os.environ.get("LIGHTKEEPER_HOME")
    exe, *args = entry_args("--watchdog", *(["--home", home] if home else []))
    arguments = subprocess.list2cmdline(args)
    start = dt.datetime.now().replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Recovers an interrupted LightKeeper run, including after sign-in. Removes itself when no run needs recovery.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user_sid)}</UserId></LogonTrigger><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user_sid)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
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
            return (f"watchdog task removed ({name})" if r.returncode == 0
                    else f"watchdog task could not be removed: {(r.stderr or r.stdout).strip()}")
        definition = _watchdog_xml(_windows_user_sid())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-16", prefix="watchdog_", suffix=".xml",
                                         dir=app_home(), delete=False) as file:
            xml = Path(file.name)
            file.write(definition)
        try:
            r = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(xml), "/F"], **kw)
        finally:
            xml.unlink(missing_ok=True)
        return f"watchdog task set ({name})" if r.returncode == 0 else f"watchdog task NOT set: {(r.stderr or r.stdout).strip()}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"watchdog task: {e!r}"


def sync_watchdog() -> str:
    """After a run closed or was interrupted, keep the task for any other recoverable run."""
    return ("watchdog task kept: another open run" if watchdog_candidates(include_unavailable=True)
            else set_watchdog_task(False))
