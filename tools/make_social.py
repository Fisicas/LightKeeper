"""Write the README hero / GitHub social preview and the 256 px icon from lightkeeper/icons.py and theme.py.

Outputs docs/images/social-preview.png (1280 x 640), docs/images/icon-256.png and LightKeeper.ico (the Windows icon
for shortcuts, 16 to 256 px, each size drawn from the vector so the small ones stay sharp).
The faint trace along the bottom is tools/social_trace.csv: a real daylight spectrum (Maya2000 Pro, forest-edge
diffuser, 1 Nov 2026, hourly median at 1 s integration minus the matched pre-run dark, with the hot pixels flagged in
the dark set interpolated over, at the native 0.44 nm pixel spacing, normalised), so the image needs no run folder to rebuild.

Usage:
  python -X utf8 tools/make_social.py
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from PySide6 import QtCore, QtGui, QtSvg  # noqa: E402
from PySide6 import QtWidgets as Q  # noqa: E402

from lightkeeper import VERSION  # noqa: E402
from lightkeeper.icons import ICON_SVG  # noqa: E402
from lightkeeper.theme import LAMP, LINE, SURFACE, TEXT, TEXT2  # noqa: E402

IMAGES = REPO / "docs" / "images"
ICO = REPO / "LightKeeper.ico"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
W, H = 1280, 640
FEATURES = ["Dark sets bracket every run", "Starts and stops on sun events", "Survives crashes, unplugs and restarts"]


def svg_image(size):
    img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    QtSvg.QSvgRenderer(QtCore.QByteArray(ICON_SVG.encode())).render(p); p.end()
    return img


def png_bytes(img):
    buf = QtCore.QBuffer(); buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG"); buf.close()
    return bytes(buf.data())


def write_ico(path, sizes=ICO_SIZES):
    """A multi-size .ico of PNG entries (Windows Vista and later), each size rendered from ICON_SVG."""
    pngs = [png_bytes(svg_image(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset, entries = 6 + 16 * len(sizes), b""
    for s, data in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    Path(path).write_bytes(head + entries + b"".join(pngs))


def load_fonts():
    """The offscreen platform sees no system fonts; register Segoe UI (Windows) when present."""
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("segoeui.ttf", "seguisb.ttf"):
        if (fonts / name).exists():
            QtGui.QFontDatabase.addApplicationFont(str(fonts / name))


def font(px, weight=QtGui.QFont.Weight.Normal):
    f = QtGui.QFont("Segoe UI"); f.setPixelSize(px); f.setWeight(weight)
    return f


def main():
    Q.QApplication.instance() or Q.QApplication([])
    load_fonts()
    IMAGES.mkdir(parents=True, exist_ok=True)
    svg_image(256).save(str(IMAGES / "icon-256.png"))
    write_ico(ICO)

    img = QtGui.QImage(W, H, QtGui.QImage.Format.Format_ARGB32_Premultiplied); img.fill(QtGui.QColor("#0e1216"))
    p = QtGui.QPainter(img); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    card = QtCore.QRectF(71, 0, W - 142, H + 40)
    g = QtGui.QLinearGradient(card.topLeft(), card.bottomRight())
    g.setColorAt(0, QtGui.QColor("#1d242c")); g.setColorAt(1, QtGui.QColor("#13171c"))
    p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1)); p.setBrush(g); p.drawRoundedRect(card, 28, 28)

    # icon tile
    tile = QtCore.QRectF(182, 120, 272, 272)
    p.setBrush(QtGui.QColor(SURFACE)); p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1.5)); p.drawRoundedRect(tile, 48, 48)
    p.drawImage(tile.adjusted(28, 28, -28, -28).toRect(), svg_image(432))

    # text block
    p.setPen(QtGui.QColor(TEXT2)); p.setFont(font(15))
    p.drawText(QtCore.QRectF(500, 34, 662, 24), QtCore.Qt.AlignmentFlag.AlignRight,
               "Independent community software • not affiliated with Ocean Insight")
    x = 548
    p.setFont(font(76, QtGui.QFont.Weight.DemiBold)); fm = QtGui.QFontMetrics(p.font())
    p.setPen(QtGui.QColor(TEXT)); p.drawText(x, 190, "Light")
    p.setPen(QtGui.QColor(LAMP)); p.drawText(x + fm.horizontalAdvance("Light"), 190, "Keeper")
    p.setFont(font(24)); p.setPen(QtGui.QColor(TEXT2))
    p.drawText(x, 240, "Time-resolved spectra • unattended field logging")
    p.setFont(font(20)); p.drawText(x, 300, f"Version {VERSION} • Ocean Insight spectrometers via OceanDirect")
    p.setFont(font(22))
    for i, line in enumerate(FEATURES):
        y = 368 + 41 * i
        pen = QtGui.QPen(QtGui.QColor(LAMP), 3.2); pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.drawPolyline([QtCore.QPointF(x, y - 7), QtCore.QPointF(x + 8, y + 1), QtCore.QPointF(x + 22, y - 15)])
        p.setPen(QtGui.QColor(TEXT)); p.drawText(x + 38, y + 1, line)

    # real daylight spectrum along the bottom, with a faint wavelength-colour wash under it
    wl, v = np.loadtxt(REPO / "tools" / "social_trace.csv", delimiter=",", skiprows=1, unpack=True)
    xs = 71 + (wl - wl[0]) / (wl[-1] - wl[0]) * (W - 142); ys = H - 18 - v * 125
    path = QtGui.QPainterPath(QtCore.QPointF(xs[0], ys[0]))
    for a, b in zip(xs[1:], ys[1:]):
        path.lineTo(a, b)
    fill = QtGui.QPainterPath(path); fill.lineTo(xs[-1], H); fill.lineTo(xs[0], H); fill.closeSubpath()
    wash = QtGui.QLinearGradient(71, 0, W - 71, 0)
    for nm, col in ((380, "#5a2d82"), (450, "#2d4fb8"), (500, "#1f9fb0"), (550, "#3fae4a"), (590, "#d8c13a"),
                    (630, "#e0702e"), (700, "#b0282a"), (800, "#4a1418"), (1050, "#2a1214")):
        c = QtGui.QColor(col); c.setAlpha(46)
        wash.setColorAt(min(1, max(0, (nm - wl[0]) / (wl[-1] - wl[0]))), c)
    p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(wash); p.drawPath(fill)
    c = QtGui.QColor(LAMP); c.setAlpha(190); p.setPen(QtGui.QPen(c, 1.7)); p.setBrush(QtCore.Qt.BrushStyle.NoBrush); p.drawPath(path)
    p.end()
    img.save(str(IMAGES / "social-preview.png"))
    print("wrote", IMAGES / "social-preview.png", IMAGES / "icon-256.png", ICO)


if __name__ == "__main__":
    main()
