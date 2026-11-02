"""LightKeeper lighthouse icon (window + tray) and the state badge used in the notification area."""
from __future__ import annotations

from PySide6 import QtCore, QtGui

BG, PANEL, TEXT, MUTED, TEAL, GOLD = "#101923", "#182532", "#e6edf3", "#93a6b8", "#43d8bf", "#f5bc66"
RED, ORANGE, PURPLE, BLUE, AMBER, GREY = "#e5534b", "#ff8c42", "#9b7be0", "#5b8fc9", "#e0a526", "#7d8b99"

ICON_SVG = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>
<circle cx='32' cy='32' r='31' fill='{PANEL}' stroke='{GOLD}' stroke-width='2'/>
<path d='M32 14 L60 4 L60 24 Z' fill='{GOLD}' opacity='0.55'/><path d='M32 14 L4 4 L4 24 Z' fill='{GOLD}' opacity='0.35'/>
<rect x='27' y='10' width='10' height='7' rx='1' fill='{GOLD}'/><path d='M26 18 L38 18 L41 56 L23 56 Z' fill='{TEXT}'/>
<path d='M25.3 27 L38.7 27 L39.3 34 L24.7 34 Z' fill='{RED}'/><path d='M24.2 42 L39.8 42 L40.4 49 L23.6 49 Z' fill='{RED}'/>
<rect x='18' y='56' width='28' height='3' rx='1' fill='{TEAL}'/></svg>"""

# tray badge colour per engine state (None = no badge)
BADGE = {"RECORDING": RED, "BURST": ORANGE, "STANDBY": BLUE, "DARK": PURPLE, "AWAIT_UNCOVER": AMBER, "POST_RUN": AMBER,
         "WAITING_DEVICE": GREY, "PREVIEW": None, "IDLE": None, "DISCONNECTED": GREY}


def _render(size: int, badge: str | None) -> QtGui.QImage:
    from PySide6 import QtSvg
    img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied); img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    QtSvg.QSvgRenderer(QtCore.QByteArray(ICON_SVG.encode())).render(p)
    if badge:
        r = size * 0.42
        p.setPen(QtGui.QPen(QtGui.QColor(BG), max(1.0, size / 16)))
        p.setBrush(QtGui.QColor(badge))
        p.drawEllipse(QtCore.QRectF(size - r - 0.5, size - r - 0.5, r, r))
    p.end()
    return img


def icon(badge: str | None = None) -> QtGui.QIcon:
    ic = QtGui.QIcon()
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ic.addPixmap(QtGui.QPixmap.fromImage(_render(s, badge)))
    return ic


def state_icon(state: str) -> QtGui.QIcon:
    return icon(BADGE.get(state))
