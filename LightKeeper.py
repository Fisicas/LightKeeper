"""LightKeeper entry point.  pythonw -X utf8 LightKeeper.py

  LightKeeper.py                       the window (attaches to a running background recorder if there is one)
  LightKeeper.py --supervisor ...      background: keeps a run's recorder alive (started by the window / at logon)
  LightKeeper.py --recorder ...        background: the recorder itself (started by the supervisor)
  LightKeeper.py --watchdog            background: one check that an open run is alive (started by a scheduled task)
  LightKeeper.py --fetch-weather <run folder> ...   public weather for recorded runs, written beside them
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--supervisor" in sys.argv:
        from lightkeeper.supervisor import main as sup_main
        raise SystemExit(sup_main([a for a in sys.argv[1:] if a != "--supervisor"]))
    if "--watchdog" in sys.argv:
        from lightkeeper.watchdog import main as dog_main
        raise SystemExit(dog_main([a for a in sys.argv[1:] if a != "--watchdog"]))
    if "--recorder" in sys.argv:
        from lightkeeper.recorder import main as rec_main
        raise SystemExit(rec_main([a for a in sys.argv[1:] if a != "--recorder"]))
    if "--fetch-weather" in sys.argv:
        from lightkeeper.weather import main as wx_main
        raise SystemExit(wx_main([a for a in sys.argv[1:] if a != "--fetch-weather"]))
    from lightkeeper.ui import main
    raise SystemExit(main())
