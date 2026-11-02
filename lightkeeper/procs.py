"""Process plumbing shared by the window, the background recorder and its supervisor.

* app_home()            %LOCALAPPDATA%\\LightKeeper (override: LIGHTKEEPER_HOME) - session files, open-run registry, logs
* recorder sessions     app_home/recorders/<serial>.json : how a window finds and attaches to a running recorder
* open-run registry     app_home/open_runs/<run folder name>.json : runs that are not finished (resumable)
* logon resume          HKCU\\...\\Run value that restarts the supervisor with --resume after a Windows restart
* pid_alive()           NEVER os.kill(pid, 0) on Windows: there it terminates the process.
"""
from __future__ import annotations

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
