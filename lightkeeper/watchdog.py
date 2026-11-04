"""LightKeeper watchdog: one pass, started about once a minute by a Windows scheduled task while a run is open.

The supervisor restarts a crashed recorder, but nothing restarted the two if they died together (2026-11-03: a Claude
desktop update closed a supervisor that had been started from inside it, mid-burst, and the run stayed down until someone
opened the window). A pass resumes each open run that has neither a supervisor nor a recorder, and otherwise does nothing:

* it leaves alone runs that were interrupted, closed, given up on by the supervisor, declined with "Not now" in the window,
  or more than WATCHDOG_GRACE_H past their scheduled stop (procs.needs_watching);
* it waits while a LightKeeper window has the spectrometer open;
* the supervisor's lock decides if a window or the logon entry resumes the same run at the same moment;
* with nothing left to watch, it removes its own scheduled task.

  LightKeeper.py --watchdog [--home <LIGHTKEEPER_HOME>]
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import traceback
from pathlib import Path

from .procs import (app_home, entry_args, pid_alive, read_json, session_path, set_watchdog_task, spawn_detached,
                    supervisor_alive, watched_runs, window_holds_device)


def run_once() -> list[str]:
    """One pass; returns what it did, for the log (empty when everything is as it should be)."""
    runs = watched_runs()
    if not runs:
        return [set_watchdog_task(False)]
    done, tried = [], set()
    holds = window_holds_device()
    for r in reversed(runs):                     # newest first: one resume per spectrometer
        run_dir, serial = r["run_dir"], r["state"].get("serial") or r.get("serial", "")
        sess = read_json(session_path(serial))
        if supervisor_alive(run_dir) or (sess and pid_alive(sess.get("pid"))) or holds or serial in tried:
            continue
        tried.add(serial)
        log = app_home() / "logs" / f"launch_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        spawn_detached(entry_args("--supervisor", "--resume", str(run_dir)), log)
        done.append(f"no supervisor or recorder for {Path(run_dir).name} (last slot {r['state'].get('last_slot_local')}); resuming it")
    return done


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--home" in argv:                         # a test or portable home; the scheduled task has no environment of its own
        home = Path(argv[argv.index("--home") + 1])
        os.environ.update(LIGHTKEEPER_HOME=str(home), LIGHTKEEPER_WATCHDOG="1")
        if not home.exists():                    # its home was deleted: never recreate it, only clean up
            set_watchdog_task(False)
            return 0
    try:
        lines = run_once()
    except Exception:
        lines = ["pass failed:\n" + traceback.format_exc()]
    if lines:
        with open(app_home() / "logs" / f"watchdog_{dt.date.today():%Y%m%d}.log", "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"[{dt.datetime.now():%H:%M:%S}] {line}\n")
    return 0
