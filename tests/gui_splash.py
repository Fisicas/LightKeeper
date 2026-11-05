"""The splash from a fresh start: it shows, its ring turns while the libraries load, each row ends with a check mark, and it closes
after the main window has opened. Runs on the real display with LIGHTKEEPER_SPLASH_VISIBLE=1, otherwise offscreen. No spectrometer.
Usage: python -X utf8 tests/gui_splash.py [folder for pictures]"""
import os
import sys
import tempfile
import time
from pathlib import Path

if not os.environ.get("LIGHTKEEPER_SPLASH_VISIBLE"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME = tempfile.mkdtemp(prefix="lk_splash_")
os.environ.update(LIGHTKEEPER_HOME=HOME, LIGHTKEEPER_AUTOCONNECT="0", LIGHTKEEPER_DATA_ROOT=str(Path(HOME) / "runs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if SHOTS:
    SHOTS.mkdir(parents=True, exist_ok=True)

from PySide6 import QtCore, QtWidgets as Q  # noqa: E402
assert "lightkeeper.ui" not in sys.modules
from lightkeeper import VERSION, splash as S  # noqa: E402

ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


check("the splash module does not load the heavy libraries", "matplotlib" not in sys.modules and "h5py" not in sys.modules)
app = Q.QApplication(sys.argv[:1])
seen, frames_at = [], {}
real_begin = S.SplashScreen.begin


def begin(self, i):
    real_begin(self, i)
    seen.append(i); frames_at[i] = self.frames
    states = [s[0] for s in self.steps]
    if i > 0:
        check(f"step {i}: earlier rows done, this one active, later ones waiting",
              states == ["done"] * i + ["active"] + ["todo"] * (len(states) - i - 1), states)
    if SHOTS:
        self.grab().save(str(SHOTS / f"splash_step_{i}.png"))


S.SplashScreen.begin = begin
t0 = time.monotonic()
holder = {}
_new = S.SplashScreen.__init__


def init(self, *a, **k):
    _new(self, *a, **k); holder["s"] = self


S.SplashScreen.__init__ = init
_app, w, sp = S.start([sys.argv[0]])
check("it was shown before the window existed", holder["s"] is sp)
check("every step ran, in order", seen == [0, 1, 2, 3, 4], seen)
check("the ring kept turning while the libraries loaded", frames_at[1] - frames_at[0] >= 5, f"{frames_at[1] - frames_at[0]} frames")
check("the main window is open", w.isVisible())
check("the splash is still up for a moment, with every row checked", sp.isVisible() and all(s[0] == "done" for s in sp.steps))
check("every row has its time", all(s[2] is not None and s[2] >= 0 for s in sp.steps), [round(s[2], 2) for s in sp.steps])
if SHOTS:
    sp.grab().save(str(SHOTS / "splash_finished.png"))
    print("started in", round(time.monotonic() - t0, 2), "s")
end = time.monotonic() + 3.0
while time.monotonic() < end and sp.isVisible():
    app.processEvents(); time.sleep(0.01)
closed_after = time.monotonic()
check("the splash closes by itself", not sp.isVisible())
check("the main window stays open", w.isVisible())
check("nothing else is left open", [x for x in app.topLevelWidgets() if x.isVisible() and x is not w] == [],
      [type(x).__name__ for x in app.topLevelWidgets() if x.isVisible() and x is not w])
check("the window title has the version", w.windowTitle() == f"LightKeeper {VERSION}", w.windowTitle())
QtCore.QTimer.singleShot(0, w.close)
end = time.monotonic() + 20
while time.monotonic() < end and w.isVisible():
    app.processEvents(); time.sleep(0.01)
check("closing the window still works", not w.isVisible())

# a failure while starting must not leave the splash on top
real = S._load_libraries


def boom(_):
    raise RuntimeError("simulated failure")


S._load_libraries = boom
try:
    S.start([sys.argv[0]])
    check("a failure is reported", False)
except RuntimeError:
    left = [x for x in app.topLevelWidgets() if isinstance(x, S.SplashScreen) and x.isVisible()]
    check("a failure while starting closes the splash", left == [])
S._load_libraries = real

src = (Path(__file__).resolve().parents[1] / "lightkeeper" / "recorder.py").read_text(encoding="utf-8")
check("the background recorder does not use the splash", "splash" not in src.lower())
print(("splash: OK" if ok else "splash: FAILED"), flush=True)
os._exit(0 if ok else 1)
