"""LightKeeper entry point.  pythonw -X utf8 LightKeeper.py

  LightKeeper.py                       the window (attaches to a running background recorder if there is one)
  LightKeeper.py --supervisor ...      background: keeps a run's recorder alive (started by the window / at logon)
  LightKeeper.py --recorder ...        background: the recorder itself (started by the supervisor)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--supervisor" in sys.argv:
        from lightkeeper.supervisor import main as sup_main
        raise SystemExit(sup_main([a for a in sys.argv[1:] if a != "--supervisor"]))
    if "--recorder" in sys.argv:
        from lightkeeper.recorder import main as rec_main
        raise SystemExit(rec_main([a for a in sys.argv[1:] if a != "--recorder"]))
    from lightkeeper.ui import main
    raise SystemExit(main())
