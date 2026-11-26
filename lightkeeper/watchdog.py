"""LightKeeper watchdog: one pass at sign-in and about once a minute while a run is open.

The supervisor restarts a crashed recorder, but nothing restarted the two if they died together (2026-11-03: a Claude
desktop update closed a supervisor that had been started from inside it, mid-burst, and the run stayed down until someone
opened the window). A pass resumes each open run that has neither a supervisor nor a recorder, and otherwise does nothing:

* it leaves alone runs that were interrupted, closed, given up on by the supervisor, declined with "Not now" in the window,
  or more than WATCHDOG_GRACE_H past their scheduled stop (procs.needs_watching);
* it waits while a LightKeeper window has the spectrometer open;
* the supervisor's lock arbitrates a simultaneous manual and automatic resume;
* after a Windows restart, only an opted-in, armed or collecting run before its stop time can resume;
* it waits for the run's volume to come back, then removes its task when no runs qualify.

  LightKeeper.py --watchdog [--home <LIGHTKEEPER_HOME>]
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import traceback
from pathlib import Path

from .procs import (app_home, entry_args, pid_alive, read_json, session_path, set_watchdog_task, spawn_detached,
                    supervisor_alive, watchdog_candidates, window_holds_device)


def run_once() -> list[str]:
    """One pass; returns what it did, for the log (empty when everything is as it should be)."""
    runs = watchdog_candidates(include_unavailable=True)
    if not runs:
        return [set_watchdog_task(False)]
    done, tried = [], set()
    holds = window_holds_device()
    for r in reversed(runs):                     # newest first: one resume per spectrometer
        if r["state"] is None:                   # wait for the same volume, regardless of its drive letter
            continue
        run_dir, serial = r["run_dir"], r["state"].get("serial") or r.get("serial", "")
        sess = read_json(session_path(serial))
        if supervisor_alive(run_dir) or (sess and pid_alive(sess.get("pid"), started=sess.get("started"))) or holds or serial in tried:
            continue
        tried.add(serial)
        log = app_home() / "logs" / f"launch_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        spawn_detached(entry_args("--supervisor", "--registration-id", r["registration_id"],
                                  "--resume-source", r["resume_source"], "--resume", str(run_dir)), log)
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
