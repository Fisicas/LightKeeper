"""Startup splash for the window: the look of the GitHub social card, with a row for each thing the program is doing.

A row shows an empty circle while it waits, a spinning ring while it runs and a check mark with its time when it is done. The
slowest step, importing the plotting libraries, runs on a thread so that the ring keeps turning; a step that has to run on the
main thread (building the window, finding the spectrometer) holds the ring still until it finishes.

The splash only draws. It never touches the spectrometer, a run or the recorder, and the background recorder never uses it.
"""
from __future__ import annotations

import csv
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets as Q

from . import APP_NAME, VERSION
from .icons import ICON_SVG, icon

W, H = 720, 405
STEPS = ("Loading plotting and analysis libraries", "Building the window", "Reading the previous run's setup",
         "Looking for a recorder and spectrometers", "Opening LightKeeper")
# the colours of theme.py and the social card; theme.py itself imports numpy, which should wait until the splash is up
TEXT, TEXT2, TEXT3, LAMP, LINE, SURFACE = "#dde3ea", "#8e9aa8", "#5f6b78", "#f2b24c", "#2f3843", "#1b2128"
WASH = ((380, "#5a2d82"), (450, "#2d4fb8"), (500, "#1f9fb0"), (550, "#3fae4a"), (590, "#d8c13a"), (630, "#e0702e"), (700, "#b0282a"),
        (800, "#4a1418"), (1050, "#2a1214"))
TRACE = Path(__file__).resolve().parents[1] / "tools" / "social_trace.csv"
ROW_Y0, ROW_DY, MARK_X, TEXT_X, RIGHT_X = 204, 25, 271, 292, 680


def _font(px: float, weight=QtGui.QFont.Weight.Normal) -> QtGui.QFont:
    f = QtGui.QFont("Segoe UI"); f.setPixelSize(int(round(px))); f.setWeight(weight)
    return f


class SplashScreen(Q.QWidget):
    def __init__(self, version: str = VERSION, steps=STEPS):
        flags = (QtCore.Qt.WindowType.SplashScreen | QtCore.Qt.WindowType.FramelessWindowHint
                 | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        super().__init__(None, flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(W, H)
        self.setWindowTitle(APP_NAME)
        self.version = version
        self.steps = [["todo", text, None] for text in steps]     # state (todo, active, done), text, seconds
        self.frames = 0
        self._current = None; self._started = 0.0; self._back = None
        screen = Q.QApplication.primaryScreen()
        if screen:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self._spin = QtCore.QTimer(self); self._spin.setInterval(30); self._spin.timeout.connect(self.update); self._spin.start()

    def begin(self, index: int):
        """Finish the step that is running and start this one."""
        self.done()
        self._current, self._started = index, time.monotonic()
        self.steps[index][0] = "active"
        self.repaint()

    def done(self):
        if self._current is not None:
            s = self.steps[self._current]
            s[0], s[2] = "done", time.monotonic() - self._started
            self._current = None
            self.repaint()

    def finish(self, delay_ms: int = 450, then=None):
        """Every row checked, seen for a moment, then close. The main window is already open underneath."""
        self.done()

        def close():
            self._spin.stop()
            self.close()
            if then:
                then()
        QtCore.QTimer.singleShot(delay_ms, close)

    # ------------------------------------------------------------ drawing
    def _background(self, ratio: float) -> QtGui.QPixmap:
        """Everything that does not change, drawn once: redrawing the spectrum for each turn of the ring would slow it."""
        pm = QtGui.QPixmap(int(W * ratio), int(H * ratio)); pm.setDevicePixelRatio(ratio); pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing | QtGui.QPainter.RenderHint.TextAntialiasing)
        card = QtCore.QRectF(0.5, 0.5, W - 1, H - 1)
        g = QtGui.QLinearGradient(card.topLeft(), card.bottomRight())
        g.setColorAt(0, QtGui.QColor("#1d242c")); g.setColorAt(1, QtGui.QColor("#13171c"))
        p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1)); p.setBrush(g); p.drawRoundedRect(card, 16, 16)
        outline = QtGui.QPainterPath(); outline.addRoundedRect(card, 16, 16)
        p.save(); p.setClipPath(outline); self._draw_trace(p); p.restore()
        tile = QtCore.QRectF(46, 78, 176, 176)
        p.setBrush(QtGui.QColor(SURFACE)); p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1.2)); p.drawRoundedRect(tile, 30, 30)
        QtSvg.QSvgRenderer(QtCore.QByteArray(ICON_SVG.encode())).render(p, tile.adjusted(18, 18, -18, -18))
        p.setFont(_font(10)); p.setPen(QtGui.QColor(TEXT2))
        p.drawText(QtCore.QRectF(240, 16, W - 240 - 24, 18), QtCore.Qt.AlignmentFlag.AlignRight, "Independent community software • not affiliated with Ocean Insight")
        p.setFont(_font(46, QtGui.QFont.Weight.DemiBold)); fm = QtGui.QFontMetrics(p.font())
        p.setPen(QtGui.QColor(TEXT)); p.drawText(262, 104, "Light")
        p.setPen(QtGui.QColor(LAMP)); p.drawText(262 + fm.horizontalAdvance("Light"), 104, "Keeper")
        p.setFont(_font(15)); p.setPen(QtGui.QColor(TEXT2)); p.drawText(263, 135, "Time-resolved spectra • unattended field logging")
        p.setFont(_font(12.5)); p.drawText(263, 166, f"Version {self.version} • Ocean Insight spectrometers via OceanDirect")
        p.end()
        return pm

    def _draw_trace(self, p: QtGui.QPainter):
        """The real daylight spectrum of the social card, along the bottom. Decoration: the splash opens without it."""
        try:
            with open(TRACE, newline="", encoding="utf-8") as fh:
                rows = [(float(a), float(b)) for a, b in list(csv.reader(fh))[1:]]
        except (OSError, ValueError):
            return
        if len(rows) < 2:
            return
        w0, w1 = rows[0][0], rows[-1][0]
        pts = [QtCore.QPointF((a - w0) / (w1 - w0) * W, H - 10 - b * 62) for a, b in rows]
        path = QtGui.QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        fill = QtGui.QPainterPath(path); fill.lineTo(W, H); fill.lineTo(0, H); fill.closeSubpath()
        wash = QtGui.QLinearGradient(0, 0, W, 0)
        for nm, col in WASH:
            c = QtGui.QColor(col); c.setAlpha(46)
            wash.setColorAt(min(1.0, max(0.0, (nm - w0) / (w1 - w0))), c)
        p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(wash); p.drawPath(fill)
        c = QtGui.QColor(LAMP); c.setAlpha(190); p.setPen(QtGui.QPen(c, 1.2)); p.setBrush(QtCore.Qt.BrushStyle.NoBrush); p.drawPath(path)

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        if self._back is None or self._back.devicePixelRatio() != ratio:
            self._back = self._background(ratio)
        p = QtGui.QPainter(self)
        p.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing | QtGui.QPainter.RenderHint.TextAntialiasing)
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source); p.drawPixmap(0, 0, self._back)
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
        now = time.monotonic()
        for i, (state, text, secs) in enumerate(self.steps):
            y = ROW_Y0 + i * ROW_DY
            self._draw_mark(p, state, y, now)
            p.setFont(_font(14)); p.setPen(QtGui.QColor(TEXT3 if state == "todo" else TEXT))
            p.drawText(TEXT_X, y + 5, text)
            if secs is not None:
                p.setFont(_font(12)); p.setPen(QtGui.QColor(TEXT2))
                p.drawText(QtCore.QRectF(RIGHT_X - 70, y - 9, 70, 18), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{secs:.1f} s")
        p.end()
        self.frames += 1

    @staticmethod
    def _draw_mark(p: QtGui.QPainter, state: str, y: int, now: float):
        r = QtCore.QRectF(MARK_X - 7, y - 7, 14, 14)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        if state == "done":
            pen = QtGui.QPen(QtGui.QColor(LAMP), 2.4); pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap); pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen); p.drawPolyline([QtCore.QPointF(MARK_X - 7, y), QtCore.QPointF(MARK_X - 2, y + 5), QtCore.QPointF(MARK_X + 7, y - 6)])
        elif state == "active":
            p.setPen(QtGui.QPen(QtGui.QColor(LINE), 2.4)); p.drawEllipse(r)
            pen = QtGui.QPen(QtGui.QColor(LAMP), 2.4); pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap); p.setPen(pen)
            p.drawArc(r, int(-(now * 360) % 360 * 16), 100 * 16)       # one turn a second, clockwise
        else:
            c = QtGui.QColor(TEXT3); p.setPen(QtGui.QPen(c, 1.5)); p.drawEllipse(r.adjusted(1.5, 1.5, -1.5, -1.5))


def _load_libraries(splash: SplashScreen | None):
    """Import the window's module on a thread while the splash keeps turning. Importing creates no window."""
    box = {}

    def job():
        try:
            import lightkeeper.ui  # noqa: F401
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=job, daemon=True, name="LightKeeperImport"); t.start()
    while t.is_alive():
        Q.QApplication.processEvents()
        t.join(0.015)
    if "error" in box:
        raise box["error"]


def start(argv=None, use_splash: bool = True):
    """Build and show everything; returns (app, window, splash). The caller runs app.exec()."""
    argv = sys.argv if argv is None else argv
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LightKeeper.Window")
    except Exception:
        pass
    app = Q.QApplication.instance() or Q.QApplication(argv)
    app.setWindowIcon(icon())
    if QtGui.QGuiApplication.platformName() == "offscreen":          # that platform sees no system fonts
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("segoeui.ttf", "seguisb.ttf"):
            if (fonts / name).exists():
                QtGui.QFontDatabase.addApplicationFont(str(fonts / name))
    splash = None
    if use_splash:
        splash = SplashScreen()
        splash.show(); splash.begin(0); Q.QApplication.processEvents()
    try:
        _load_libraries(splash)
        from . import theme, ui
        theme.apply(app)
        w = ui.Window(auto_confirm="--auto-confirm" in argv, progress=splash.begin if splash else None)
        if splash:
            splash.begin(4)
        w.show()
        Q.QApplication.processEvents()
    except BaseException:
        if splash:                       # never leave a stay-on-top splash behind a failure
            splash.close()
        raise
    if splash:
        splash.finish(then=lambda: (w.raise_(), w.activateWindow()))
    return app, w, splash


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        app, w, _ = start(argv, use_splash="--no-splash" not in argv and os.environ.get("LIGHTKEEPER_SPLASH", "1") != "0")
    except Exception:                    # pythonw has no console, so a start-up failure would otherwise vanish silently
        text = traceback.format_exc()
        print(text, file=sys.stderr)
        Q.QMessageBox.critical(None, f"{APP_NAME} could not start", text[-3000:])
        return 1
    return app.exec()
