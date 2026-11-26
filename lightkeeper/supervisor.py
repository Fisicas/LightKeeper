"""LightKeeper supervisor: keeps a run's recorder alive.

Runs the recorder as a child process. A normal close (0), a deliberate interruption (10), a refusal (2) or a new run that
could not open the spectrometer (3) end supervision. Any other exit (crash, killed from Task Manager, native fault in
the vendor SDK) restarts the recorder with --resume after a back-off, as long as run_state.json still says 'open'.
More than MAX_CRASHES crashes within CRASH_WINDOW_S stops the restarts (the run stays open and can be resumed by hand).

  LightKeeper.py --supervisor --plan <plan.json> --serial <serial> [--device-id N]
  LightKeeper.py --supervisor --resume <run folder>
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

from .procs import (EXIT_DONE, EXIT_INTERRUPTED, EXIT_NO_DEVICE, EXIT_REFUSED, IS_WIN, WATCHDOG_RESTART_PHASES,
                    app_home, entry_args, pid_alive, read_json, remove_legacy_logon_entries, session_path,
                    supervisor_lock, watchdog_candidates, watchdog_clear, watchdog_off, write_json_atomic)

BACKOFF_S = [5, 10, 20, 40, 60]
MAX_CRASHES, CRASH_WINDOW_S = 6, 600
FINAL = {EXIT_DONE, EXIT_REFUSED, EXIT_NO_DEVICE, EXIT_INTERRUPTED}


def _arg(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    log = open(app_home() / "logs" / f"supervisor_{dt.datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log", "a", buffering=1, encoding="utf-8")

    def say(*a):
        print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}]", *a, file=log, flush=True)

    say("supervisor start", argv)
    run_dir = Path(_arg(argv, "--resume")) if "--resume" in argv else None
    source = _arg(argv, "--resume-source") or "manual"
    serial = _arg(argv, "--serial")
    if "--registration-id" in argv:
        index = argv.index("--registration-id")
        registration = argv[index + 1]
        del argv[index:index + 2]
        # Re-resolve the data volume before writing any supervisor files.
        match = next((r for r in watchdog_candidates() if r["registration_id"] == registration
                      and r["resume_source"] == source), None)
        if not match or run_dir is None:
            say("registered run is no longer available for recovery"); return EXIT_REFUSED
        run_dir = Path(match["run_dir"])
        argv[argv.index("--resume") + 1] = str(run_dir)
    if run_dir is not None:
        remove_legacy_logon_entries(run_dir)
        st = read_json(run_dir / "run_state.json")
        if not st or st.get("status") == "closed":
            say("run closed or unknown; nothing to do"); return EXIT_REFUSED
        serial = serial or st.get("serial", "")
        sup = read_json(run_dir / "supervisor.json")
        if sup and pid_alive(sup.get("pid"), started=sup.get("started")) and int(sup["pid"]) != os.getpid():
            say(f"another supervisor (pid {sup['pid']}) already looks after this run"); return EXIT_REFUSED
        sess = read_json(session_path(serial))
        if sess and pid_alive(sess.get("pid"), started=sess.get("started")):
            say(f"a recorder (pid {sess['pid']}) already holds {serial}"); return EXIT_REFUSED
    for attempt in range(6 if run_dir is None else 1):   # a new run may start just as the last run's supervisor is ending
        if supervisor_lock(serial):
            break
        time.sleep(0.5)
    else:
        say(f"another supervisor already looks after {serial}"); return EXIT_REFUSED
    if run_dir is not None:
        if source == "manual":
            watchdog_clear(run_dir)
        else:
            st = read_json(run_dir / "run_state.json") or {}
            if st.get("status") != "open" or (run_dir / "watchdog_off.json").exists():
                say("automatic resume is no longer scheduled"); return EXIT_REFUSED
            if source == "windows_restart":
                try:
                    stop = dt.datetime.fromisoformat(st["planned_stop_local"])
                except (KeyError, TypeError, ValueError):
                    say("the scheduled stop time is missing or invalid"); return EXIT_REFUSED
                if st.get("phase") not in WATCHDOG_RESTART_PHASES or dt.datetime.now(stop.tzinfo) >= stop:
                    say("run is no longer awaiting acquisition after a Windows restart"); return EXIT_REFUSED
    env = dict(os.environ, LIGHTKEEPER_SUPERVISOR_PID=str(os.getpid()))
    child_args = [a for a in argv]
    crashes: list[float] = []
    k = 0
    while True:
        if run_dir is not None:
            write_json_atomic(run_dir / "supervisor.json", dict(pid=os.getpid(), started=dt.datetime.now().isoformat()))
        cmd = entry_args("--recorder", *child_args)
        say("launch", cmd)
        kw = dict(creationflags=0x08000000) if IS_WIN else {}
        p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        rc = p.wait()
        say("recorder exit code", rc)
        if run_dir is None:                                  # new run: learn its folder from the recorder session file
            sess = read_json(session_path(serial)) or {}
            if sess.get("run_dir"):
                run_dir = Path(sess["run_dir"])
        if rc in FINAL:
            break
        if run_dir is None:
            say("recorder died before creating the run; nothing to resume"); break
        st = read_json(run_dir / "run_state.json") or {}
        if st.get("status") != "open":
            say("run status", st.get("status"), "- not restarting"); break
        now = time.time(); crashes = [t for t in crashes if now - t < CRASH_WINDOW_S] + [now]
        if len(crashes) > MAX_CRASHES:
            say(f"{len(crashes)} crashes in {CRASH_WINDOW_S} s - giving up; resume by hand from the window")
            watchdog_off(run_dir, f"supervisor gave up after {len(crashes)} crashes in {CRASH_WINDOW_S} s"); break
        wait = BACKOFF_S[min(k, len(BACKOFF_S) - 1)]; k += 1
        say(f"recorder crashed; resuming {run_dir} in {wait} s")
        time.sleep(wait)
        child_args = ["--resume-source", "supervisor_restart", "--resume", str(run_dir)]
    if run_dir is not None:
        sup = read_json(run_dir / "supervisor.json")
        if sup and int(sup.get("pid", -1)) == os.getpid():
            try:
                (run_dir / "supervisor.json").unlink()
            except FileNotFoundError:
                pass
    say("supervisor end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
