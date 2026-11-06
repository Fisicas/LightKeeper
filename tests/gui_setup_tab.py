"""Tab through the setup panel: its content never moves sideways, nothing is cut off, and focus still scrolls it up and down.
Runs on the real display with LIGHTKEEPER_SPLASH_VISIBLE=1, otherwise offscreen. No spectrometer; temporary settings."""
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("LIGHTKEEPER_SPLASH_VISIBLE"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME = tempfile.mkdtemp(prefix="lk_tab_")
os.environ.update(LIGHTKEEPER_HOME=HOME, LIGHTKEEPER_AUTOCONNECT="0", LIGHTKEEPER_DATA_ROOT=HOME)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtCore, QtTest, QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402

app = Q.QApplication(sys.argv[:1]); ui.theme.apply(app)
ok = True


def check(name, cond, got=""):
    global ok
    print(("ok  " if cond else "FAIL"), name, got, flush=True)
    ok = ok and bool(cond)


w = ui.Window(auto_confirm=True); w.resize(1500, 800); w.show(); app.processEvents()
sc = w.setup.findChild(Q.QScrollArea); inner = sc.widget(); hbar, vbar = sc.horizontalScrollBar(), sc.verticalScrollBar()


def walk(tag):
    w.label_edit.setFocus(); app.processEvents()
    moved, down, seen, cut = set(), set(), 0, 0
    for _ in range(90):
        QtTest.QTest.keyClick(app.focusWidget() or w, QtCore.Qt.Key.Key_Tab); app.processEvents()
        f = app.focusWidget()
        if f is None or not inner.isAncestorOf(f):
            continue
        seen += 1
        moved.add(hbar.value()); moved.add(inner.x()); down.add(vbar.value())
        if f.mapTo(sc.viewport(), QtCore.QPoint(0, 0)).x() < 0:
            cut += 1
    check(f"{tag}: Tab reached the panel's boxes", seen > 15, seen)
    check(f"{tag}: the content never moved sideways", moved == {0}, sorted(moved))
    check(f"{tag}: no focused box was cut off on the left", cut == 0, cut)
    check(f"{tag}: Tab still scrolls up and down", len(down) > 1, len(down))


walk("as opened")
if app.platformName() != "offscreen":              # offscreen has no system fonts, so its text is far wider than on any real screen
    check("the content fits the panel", inner.minimumSizeHint().width() <= sc.viewport().width(), f"{inner.minimumSizeHint().width()} in {sc.viewport().width()}")
    check("no sideways range is left", hbar.maximum() == 0, hbar.maximum())
right = inner.mapTo(w.setup, QtCore.QPoint(inner.width(), 0)).x()
w.btn_adv.setChecked(True); app.processEvents()
walk("advanced open")
w.start_mode.setCurrentIndex(1); w.stop_mode.setCurrentIndex(0); w.run_exp.setCurrentIndex(1); app.processEvents()
walk("set times, automatic exposure")
hbar.setValue(20); app.processEvents()
check("even a direct sideways scroll is undone", hbar.value() == 0 and inner.x() == 0, hbar.value())
check("the panel stays a sensible width", 392 <= w.setup.width() <= ui.SETUP_MAX_W, w.setup.width())
print(("setup tab: OK" if ok else "setup tab: FAILED"), flush=True)
os._exit(0 if ok else 1)
