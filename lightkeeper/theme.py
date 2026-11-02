"""LightKeeper visual theme (0.3.0).

Instrument software used at dawn and dusk: a dark graphite ground that is kind to night vision, ONE accent (lighthouse
lamp amber) for primary actions and the live trace, and state colours used only for the state indicator. Qt's Fusion
style with a matching palette gives compact, consistent controls (the native Windows style mixed with a stylesheet
produced oversized spin-box arrows and uneven heights). Matplotlib is styled from the same tokens.

The one distinctive element is the wavelength strip under the spectrum: the device axis rendered in the colour of each
wavelength, fading to a dim NIR red past 700 nm, so the x-axis itself reads as light.
"""
from __future__ import annotations

import numpy as np
from PySide6 import QtGui, QtWidgets as Q

# ---------------------------------------------------------------- tokens
GROUND = "#15191e"      # window
SURFACE = "#1b2128"     # panels, plot faces
RAISED = "#242b34"      # inputs, buttons
LINE = "#2f3843"        # hairlines, borders
GRID = "#252d37"        # plot grid
TEXT = "#dde3ea"
TEXT2 = "#8e9aa8"       # secondary text
TEXT3 = "#5f6b78"       # disabled
LAMP = "#f2b24c"        # the accent: primary actions, live trace, focus
LAMP_DIM = "#a77a33"
# state colours (indicator only) and data series
RED, BLUE, VIOLET, GREEN, ORANGE = "#e5604f", "#5e93d1", "#9a86d8", "#4fb38a", "#f08a3c"
SERIES = [LAMP, "#6fb7d9", "#c58ad6", "#7cc48f", "#e07a6a"]

FONT = "Segoe UI"
BASE_PT = 9.5

STATE = {   # state -> (indicator colour, label)
    "DISCONNECTED": (TEXT3, "No spectrometer"), "IDLE": (TEXT2, "Connected"), "PREVIEW": (GREEN, "Live view, not recording"),
    "LAMP": (VIOLET, "Calibration acquisition"), "DARK": (VIOLET, "Dark set, keep the input covered"),
    "AWAIT_UNCOVER": (LAMP, "Uncover the input, then Continue"), "STANDBY": (BLUE, "Armed"),
    "RECORDING": (RED, "Recording"), "BURST": (ORANGE, "Recording a fast burst"),
    "POST_RUN": (LAMP, "Run finished, post-run darks needed"), "WAITING_DEVICE": (TEXT3, "Waiting for the spectrometer"),
}

STYLESHEET = f"""
* {{ font-family: '{FONT}'; font-size: {BASE_PT}pt; }}
QMainWindow, QDialog {{ background: {GROUND}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px; }}
QLabel#appname {{ font-size: 15pt; font-weight: 600; color: {TEXT}; }}
QLabel#runname {{ font-size: 11pt; color: {TEXT2}; }}
QLabel#secondary {{ color: {TEXT2}; }}
QLabel#metric_label {{ color: {TEXT2}; font-size: 8.5pt; }}
QLabel#metric {{ font-size: 13pt; font-weight: 600; color: {TEXT}; }}
QLabel#section {{ font-size: 10pt; font-weight: 600; color: {TEXT}; padding-top: 6px; }}
QLabel#clock {{ font-size: 12pt; color: {TEXT2}; }}
QFrame#setup {{ background: {SURFACE}; border-right: 1px solid {LINE}; }}
QFrame#strip {{ background: {SURFACE}; border-bottom: 1px solid {LINE}; }}
QFrame#hairline {{ background: {LINE}; max-height: 1px; min-height: 1px; border: none; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel#statechip {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 13px; padding: 3px 14px 3px 10px;
  font-size: 10pt; font-weight: 600; }}
QFrame#setup QLabel {{ color: {TEXT2}; }}
QFrame#setup QLabel#section {{ color: {TEXT}; }}
QFrame#setupfoot {{ background: {SURFACE}; }}
QLineEdit, QAbstractSpinBox, QComboBox, QDateTimeEdit {{
  background: {RAISED}; border: 1px solid {LINE}; border-radius: 4px; padding: 3px 6px; min-height: 20px;
  selection-background-color: {LAMP_DIM}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus {{ border-color: {LAMP}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0px; border: none; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ color: {TEXT3}; background: {SURFACE}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {LINE}; selection-background-color: {LINE}; }}
QPushButton {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 4px; padding: 5px 12px; }}
QPushButton:hover {{ border-color: {TEXT2}; }}
QPushButton:pressed {{ background: {LINE}; }}
QPushButton:disabled {{ color: {TEXT3}; border-color: {RAISED}; }}
QPushButton#primary {{ background: {LAMP}; color: #1a1408; border: 1px solid {LAMP}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #f6c26c; }}
QPushButton#primary:disabled {{ background: {RAISED}; color: {TEXT3}; border-color: {RAISED}; }}
QPushButton#danger {{ background: transparent; color: {RED}; border: 1px solid #6a3a35; }}
QPushButton#danger:hover {{ background: #3a2522; }}
QPushButton#danger:disabled {{ color: {TEXT3}; border-color: {RAISED}; }}
QPushButton#flat, QToolButton#flat {{ background: transparent; border: 1px solid transparent; color: {TEXT2}; padding: 4px 8px; }}
QPushButton#flat:hover, QToolButton#flat:hover {{ color: {TEXT}; border-color: {LINE}; }}
QToolButton#flat::menu-indicator {{ image: none; }}
QCheckBox {{ spacing: 6px; color: {TEXT}; }}
QCheckBox:disabled {{ color: {TEXT3}; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {LINE}; }}
QTabBar::tab {{ background: transparent; color: {TEXT2}; padding: 7px 14px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {LAMP}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QPlainTextEdit, QTextEdit {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 4px; color: {TEXT}; }}
QProgressBar {{ background: {RAISED}; border: none; border-radius: 3px; text-align: center; color: {TEXT}; max-height: 14px; }}
QProgressBar::chunk {{ background: {VIOLET}; border-radius: 3px; }}
QStatusBar {{ background: {SURFACE}; color: {TEXT2}; border-top: 1px solid {LINE}; }}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: {GROUND}; }}
QMenu {{ background: {RAISED}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 3px; }}
QMenu::item:selected {{ background: {LINE}; }}
QMenu::item:disabled {{ color: {TEXT2}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 6px; }}
QToolBar {{ background: transparent; border: none; spacing: 2px; }}
QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px; }}
QToolBar QToolButton:hover {{ border-color: {LINE}; }}
QToolBar QToolButton:checked {{ background: {RAISED}; border-color: {LAMP_DIM}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {LINE}; border-radius: 4px; min-width: 30px; }}
"""


def palette() -> QtGui.QPalette:
    p = QtGui.QPalette()
    c = QtGui.QColor
    R = QtGui.QPalette.ColorRole
    for role, col in [(R.Window, GROUND), (R.WindowText, TEXT), (R.Base, RAISED), (R.AlternateBase, SURFACE),
                      (R.ToolTipBase, RAISED), (R.ToolTipText, TEXT), (R.Text, TEXT), (R.Button, RAISED), (R.ButtonText, TEXT),
                      (R.BrightText, "#ffffff"), (R.Highlight, LAMP_DIM), (R.HighlightedText, "#ffffff"), (R.Link, LAMP),
                      (R.PlaceholderText, TEXT3), (R.Mid, LINE), (R.Dark, GROUND), (R.Light, LINE)]:
        p.setColor(role, c(col))
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(QtGui.QPalette.ColorGroup.Disabled, role, c(TEXT3))
    return p


def apply(app: Q.QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setStyleSheet(STYLESHEET)
    f = QtGui.QFont(FONT); f.setPointSizeF(BASE_PT); app.setFont(f)


def mpl_rc() -> dict:
    return {"font.family": [FONT, "DejaVu Sans"], "font.size": 8.5, "text.color": TEXT, "axes.titlecolor": TEXT,
            "axes.titlesize": 9.5, "axes.titleweight": "semibold", "axes.titlelocation": "left", "axes.titlepad": 6,
            "axes.labelcolor": TEXT2, "axes.labelsize": 8.5, "axes.edgecolor": LINE, "axes.facecolor": SURFACE,
            "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
            "xtick.color": TEXT2, "ytick.color": TEXT2, "xtick.labelsize": 8, "ytick.labelsize": 8,
            "legend.frameon": False, "legend.fontsize": 8, "legend.labelcolor": TEXT2, "figure.facecolor": GROUND,
            "axes.formatter.use_mathtext": False, "axes.formatter.limits": (-5, 6)}


# ---------------------------------------------------------------- wavelength colour strip
def wavelength_rgb(nm: np.ndarray) -> np.ndarray:
    """Approximate perceived colour of monochromatic light (piecewise-linear after D. Bruton, 1996), RGB in 0..1, with
    intensity fall-off towards the eye's limits. Outside 380-700 nm the strip fades from deep violet / dim red into the
    surface colour, so UV and NIR read as 'not visible' rather than black bands. Display aid only, not colorimetry."""
    w = np.asarray(nm, float)
    r = np.zeros_like(w); g = np.zeros_like(w); b = np.zeros_like(w)
    m = (w >= 380) & (w < 440); r[m] = -(w[m] - 440) / 60; b[m] = 1
    m = (w >= 440) & (w < 490); g[m] = (w[m] - 440) / 50; b[m] = 1
    m = (w >= 490) & (w < 510); g[m] = 1; b[m] = -(w[m] - 510) / 20
    m = (w >= 510) & (w < 580); r[m] = (w[m] - 510) / 70; g[m] = 1
    m = (w >= 580) & (w < 645); r[m] = 1; g[m] = -(w[m] - 645) / 65
    m = (w >= 645); r[m] = 1
    f = np.ones_like(w)
    m = (w < 420); f[m] = 0.3 + 0.7 * np.clip((w[m] - 380) / 40, 0, 1)
    m = (w > 700); f[m] = 0.3 + 0.7 * np.clip((780 - w[m]) / 80, 0, 1)
    rgb = np.stack([r, g, b], -1) * f[:, None] ** 0.8
    bg = np.array(QtGui.QColor(SURFACE).getRgbF()[:3])
    # NIR: continue the fading red smoothly (no step at 780 nm), down to half of that by the end of the detector range
    nir = w > 780
    rgb[nir] = np.array([0.3 ** 0.8, 0, 0]) * (1 - 0.5 * np.clip((w[nir] - 780) / 340, 0, 1))[:, None]
    # UV: fade from the dim violet into the panel colour
    a = np.ones_like(w)
    a[w < 380] = np.clip(1 - (380 - w[w < 380]) / 40, 0, 1)
    return rgb * a[:, None] + bg * (1 - a[:, None])
