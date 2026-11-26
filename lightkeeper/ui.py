"""LightKeeper main window: PySide6 + matplotlib in the GammaKeeper style, with the spectra in front.

Layout: header (state banner, clock, next event) | left: spectrometer, live view and run plan | centre tabs:
Spectrum, Waterfall, Time series, Schedule, Calibration, Events | right: collection counters.
The window never talks to the spectrometer: every command goes through the Engine thread's queue.

0.2.0: a RUN is executed by a separate background recorder process (recorder.py, kept alive by supervisor.py) with its
own notification-area icon. The window starts it, then only attaches to it (link.py): closing the window, or the window
crashing, never stops a run. Live view and calibration still use the window's own Engine thread when no run is active.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import threading
import time
from bisect import bisect_left
from pathlib import Path

import matplotlib
matplotlib.use("QtAgg")
import matplotlib.colors  # noqa: E402,F401
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker  # noqa: E402,F401
import numpy as np  # noqa: E402
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets as Q  # noqa: E402

from . import APP_NAME, VERSION  # noqa: E402
from .calibration import LAMPS, fit_calibration, reference_lines, save_calibration  # noqa: E402
from .devices import SIM_ID, list_devices, shutdown_sdk  # noqa: E402
from . import clock  # noqa: E402
from .engine import SAT_LEVEL, Engine  # noqa: E402
from .icons import ICON_SVG, icon  # noqa: E402,F401
from .link import RecorderLink  # noqa: E402
from .locations import location_key, remember_path, resolve_path  # noqa: E402
from .procs import (app_home, entry_args, gui_python, live_sessions, needs_watching, open_runs, read_json, set_window_holds, spawn_detached,  # noqa: E402
                    supervisor_alive, watched_runs, watchdog_off,  # noqa: E402
                    write_json_atomic)
from .schedule import (SETUP_FIELDS, DEFAULT_LADDER_MS, DEFAULT_OUT_DIR, DEFAULT_SITE, SUN_EVENTS, RunPlan, estimates, next_clock_time, old_acquisition_defaults,  # noqa: E402
                       next_label, old_default_times, solar_day, sun_events)
from .storage import LOCAL_TZ, local_dt  # noqa: E402

from . import display, hotpix, runview, theme, weather  # noqa: E402
from .theme import BLUE, GREEN, LAMP, LINE, ORANGE, RED, SURFACE, TEXT, TEXT2, TEXT3, VIOLET  # noqa: E402

# legacy colour names used by the drawing code, mapped onto the theme tokens
BG, PANEL, MUTED, GOLD, TEAL, PURPLE = theme.GROUND, SURFACE, TEXT2, LAMP, theme.SERIES[1], VIOLET
STYLESHEET = theme.STYLESHEET
STATE = theme.STATE
matplotlib.rcParams.update(theme.mpl_rc())
LIVE_START_MS = 100       # live view on opening: fixed integration, one scan per update (no averaging)
RUN_STATES = ("DARK", "AWAIT_UNCOVER", "STANDBY", "RECORDING", "BURST", "POST_RUN", "WAITING_DEVICE")
RECENT_CMAP = matplotlib.colors.LinearSegmentedColormap.from_list("lk_recent", ["#3d4652", "#8c7448", LAMP])
THOUSANDS = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")


def style_ax(ax):
    """Re-apply the theme after ax.cla() (rcParams cover new axes)."""
    ax.set_facecolor(SURFACE)
    for k in ("top", "right"):
        ax.spines[k].set_visible(False)
    for k in ("left", "bottom"):
        ax.spines[k].set_color(LINE)
    ax.tick_params(colors=TEXT2, labelsize=8)
    ax.xaxis.label.set_color(TEXT2); ax.yaxis.label.set_color(TEXT2)
    ax.grid(True, color=theme.GRID, lw=0.6)


class Canvas(FigureCanvasQTAgg):
    def __init__(self, rows=1, cols=1, ratios=None, sharex=False):
        self.fig = Figure(facecolor=theme.GROUND, layout="constrained")
        super().__init__(self.fig)
        self.fig.get_layout_engine().set(h_pad=0.04, w_pad=0.06, hspace=0.02)
        gs = self.fig.add_gridspec(rows, cols, height_ratios=ratios)
        self.ax = []
        for r in range(rows):
            for c in range(cols):
                a = self.fig.add_subplot(gs[r, c], sharex=self.ax[0] if (sharex and self.ax) else None)
                style_ax(a); self.ax.append(a)


def plot_toolbar(canvas, parent):
    """Matplotlib navigation with only the actions used here (home, back, forward, pan, zoom, save)."""
    tb = NavigationToolbar2QT(canvas, parent, coordinates=False)
    for a in list(tb.actions()):
        if a.text() in ("Subplots", "Customize"):
            tb.removeAction(a)
    tb.setIconSize(QtCore.QSize(18, 18))
    return tb


class WavelengthCursor:
    """A line placed on a plot to read wavelengths: click or drag to place it; arrow keys move it one pixel (Shift: ten).
    It snaps to pixels, and survives the plots being redrawn (sync() puts it back after ax.cla())."""
    def __init__(self, canvas, toolbar, axes, xs, on_move, with_time=False):
        self.canvas, self.toolbar, self.axes, self.xs, self.on_move, self.with_time = canvas, toolbar, axes, xs, on_move, with_time
        self.on = False; self.x = None; self.y = None; self.drag = False; self.lines = []
        self.hint = ""             # 0.4.2: why the last click did not place the cursor (pan/zoom on), shown by on_move
        canvas.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        for name, fn in (("button_press_event", self._press), ("motion_notify_event", self._motion),
                         ("button_release_event", self._release), ("key_press_event", self._key)):
            canvas.mpl_connect(name, fn)

    def _tool_mode(self) -> str:
        return str(getattr(self.toolbar, "mode", "") or "")

    def enable(self, on):
        """0.4.2: ticking the cursor turns the toolbar's pan or zoom off, which would otherwise take every click."""
        self.on = bool(on); self.drag = False; self.hint = ""
        m = self._tool_mode()
        if self.on and m:
            (self.toolbar.pan if "pan" in m else self.toolbar.zoom)()
        self.sync(); self.canvas.draw_idle(); self.on_move()

    def sync(self):
        """Draw the lines for the current state. Plots that were cleared have lost theirs, so they are made afresh."""
        for ln in self.lines:
            try:
                ln.remove()
            except (ValueError, NotImplementedError, AttributeError):
                pass
        self.lines = []
        if not self.on or self.x is None:
            return
        for k, a in enumerate(self.axes()):
            self.lines.append(a.axvline(self.x, color=TEXT, lw=0.8, ls=(0, (3, 2)), gid="cursor", zorder=5))
            if self.with_time and self.y is not None:
                self.lines.append(a.axhline(self.y, color=TEXT, lw=0.8, ls=(0, (3, 2)), gid="cursor", zorder=5))
            if k == 0:             # 0.4.2: the wavelength on the plot itself, at the top of the line
                self.lines.append(a.annotate(f"{self.x:.2f} nm", xy=(self.x, 1.0), xycoords=("data", "axes fraction"),
                                             xytext=(4, -4), textcoords="offset points", ha="left", va="top", fontsize=8,
                                             color=TEXT, gid="cursor", zorder=6, annotation_clip=False,
                                             bbox=dict(boxstyle="round,pad=0.2", fc=SURFACE, ec=LINE, lw=0.6, alpha=0.9)))

    def _snap(self, x):
        xs = self.xs()
        return float(xs[int(np.argmin(np.abs(xs - x)))]) if xs is not None and len(xs) else float(x)

    def _place(self, x, y=None):
        self.x = self._snap(x)
        if y is not None:
            self.y = y
        self.sync(); self.canvas.draw_idle(); self.on_move()

    def _press(self, ev):
        if not self.on or ev.button != 1 or ev.dblclick or ev.xdata is None or ev.inaxes not in self.axes():
            return
        if self._tool_mode():      # the click belongs to pan/zoom: say so instead of doing nothing
            self.hint = "Pan or zoom is on: press its toolbar button again to place the cursor"; self.on_move(); return
        self.hint = ""
        self.canvas.setFocus(); self.drag = True; self._place(ev.xdata, ev.ydata)

    def _motion(self, ev):
        if self.drag and ev.xdata is not None and ev.inaxes in self.axes():
            self._place(ev.xdata, ev.ydata)

    def _release(self, ev):
        self.drag = False

    def _key(self, ev):
        if not self.on or self.x is None or not ev.key:
            return
        n = {"left": -1, "right": 1, "shift+left": -10, "shift+right": 10}.get(ev.key)
        xs = self.xs()
        if n and xs is not None and len(xs):
            self._place(float(xs[int(np.clip(np.argmin(np.abs(xs - self.x)) + n, 0, len(xs) - 1))]))


class JumpSlider(Q.QSlider):
    """A slider that goes where you click (a plain QSlider only steps towards it), and can be dragged from there."""
    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            o = Q.QStyleOptionSlider(); self.initStyleOption(o)
            h = self.style().subControlRect(Q.QStyle.ComplexControl.CC_Slider, o, Q.QStyle.SubControl.SC_SliderHandle, self).width()
            self.setValue(Q.QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(ev.position().x()) - h // 2, self.width() - h))
        super().mousePressEvent(ev)


def section(lay, title):
    t = Q.QLabel(title); t.setObjectName("section"); lay.addWidget(t)


def panel(title: str | None = None):
    """A plain titled block (no card): used by the Calibration tab."""
    f = Q.QFrame(); lay = Q.QVBoxLayout(f); lay.setContentsMargins(0, 0, 8, 0); lay.setSpacing(6)
    if title:
        section(lay, title)
    return f, lay


def form(lay):
    fl = Q.QFormLayout(); fl.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
    fl.setFieldGrowthPolicy(Q.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    fl.setHorizontalSpacing(10); fl.setVerticalSpacing(6)
    lay.addLayout(fl); return fl


def hbox(*items, stretch_first=True):
    w = Q.QWidget(); h = Q.QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
    for i, it in enumerate(items):
        if isinstance(it, str):
            lab = Q.QLabel(it); lab.setObjectName("secondary"); h.addWidget(lab)
        else:
            h.addWidget(it, 1 if (stretch_first and i == 0) else 0)
    return w


class SpinBox(Q.QDoubleSpinBox):
    """Spin box that ignores the mouse wheel unless it has focus (scrolling the setup panel must never change a value).
    Up/Down keys and the wheel step it once it is clicked. The arrow buttons are hidden by the theme (zero width; Qt's
    NoButtons collapses the text field under a stylesheet)."""
    def __init__(self):
        super().__init__()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class IntSpin(Q.QSpinBox):
    def __init__(self):
        super().__init__()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class Combo(Q.QComboBox):
    def __init__(self):
        super().__init__(); self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class DateTimeEdit(Q.QDateTimeEdit):
    def __init__(self, v):
        super().__init__(v); self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()

BACKOFF_MANY = 10    # more integration times than this under back-off: the pre-flight list says what that costs


def _backoff_txt(plan) -> str:
    return f", backing off in {plan.backoff_step_ms:g} ms steps to {plan.backoff_min_ms:g} ms" if plan.backoff_step_ms > 0 else ""


def site_text(lat, lon) -> str:
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


def site_problem(lat, lon) -> str:
    """Why sun times for this site would be wrong, or ''. Catches an unset site and a longitude whose solar noon is far
    from midday on the local clock (almost always a missing minus sign: west longitudes are negative)."""
    if (lat, lon) == (0.0, 0.0):
        return "Site not set (0°, 0°): sun times are for 0° N 0° E."
    def off(lo):
        noon = sun_events(dt.datetime.now(LOCAL_TZ).date(), lat, lo)["solar noon"]
        return noon, noon.hour + noon.minute / 60 - 12
    noon, h = off(lon)
    if abs(h) <= 3:
        return ""
    msg = f"Solar noon at this site is {noon:%H:%M} on the local clock ({LOCAL_TZ}), which does not fit the time zone. "
    if abs(off(-lon)[1]) <= 3:
        return msg + f"West longitudes are negative: did you mean {-lon:.5f}°?"
    return msg + "Check the longitude (west is negative) or set LIGHTKEEPER_TZ to the site's time zone."


def dspin(lo, hi, val, dec=1, step=1.0, suffix=""):
    s = SpinBox(); s.setRange(lo, hi); s.setDecimals(dec); s.setSingleStep(step); s.setValue(val)
    s.setKeyboardTracking(False)
    if suffix:
        s.setSuffix(" " + suffix)
    return s


SETUP_MIN_W, SETUP_MAX_W = 392, 520      # the setup panel: wide enough for its content, within reason


class Window(Q.QMainWindow):
    def __init__(self, auto_confirm: bool = False, progress=None):
        """progress(step): optional, told what is starting (the splash); events are not processed until the timers below can run."""
        super().__init__()
        self._started = progress is None          # a splash calls startup_finished() once it has closed
        progress = progress or (lambda step: None)
        progress(1)
        self.setWindowTitle(f"{APP_NAME} {VERSION}"); self.setWindowIcon(icon()); self.resize(1600, 960)
        self.auto_confirm = auto_confirm
        if os.environ.get("LIGHTKEEPER_HOME"):    # tests and portable setups keep their settings beside their app home
            self.settings = QtCore.QSettings(str(app_home() / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        else:
            self.settings = QtCore.QSettings("LightKeeper", "LightKeeper")
        self.engine = Engine(); self.engine.start(); self._holds_device = False
        self.state = "DISCONNECTED"; self.info = {}; self.wl_dev = None; self.axis = None; self.axis_label = "device axis"
        self.latest = None; self.latest_int = None; self.latest_t = None; self.darks = {}; self.valid = None; self.hot = None
        self.slots = []; self.spectra = []; self.spec_stride = 1; self._slot_count = 0; self._dirty_ts = False
        self.run_dir = None; self.plan = None; self.run_plan = None; self.run_times = None; self.cal = {}; self.cal_result = None; self.n_bursts = 0; self._last_ts_draw = 0.0
        self._dialog = False; self._last_live = 0; self._scan_new = False
        self._edge_layout = None
        self._repeat_save = None
        self._setup_scrolled = 0.0
        self._scrub_t = None; self._usable_cache = (None, 0); self._recent_shown = None; self._wf_shown = None
        self._applying_setup = False; self._setup_from = None; self._wx_log = []
        self.viewing = None; self._load = None; self.link = None; self._expect_attach = None; self._closed_run = False; self._a1_ylim = None; self._last_attach_try = 0.0
        # like OceanView: open the first attached spectrometer and show single 100 ms scans, without pressing Connect.
        # Off after the user disconnects; a device that failed to open is not retried until the attached set changes.
        self._auto = os.environ.get("LIGHTKEEPER_AUTOCONNECT", "1") != "0"; self._auto_live = False; self._auto_failed = None; self._last_auto_try = 0.0
        # 0.4.1: questions (resume a run) and opening a spectrometer wait until the splash has closed; a dialog shown under the
        # stay-on-top splash held start-up open, so the splash never closed. No splash (tests): started at once.
        self._on_started = []; self._switching = None
        # 0.4.2: time a connect was sent; until it ends (IDLE or DISCONNECTED) no second connect and no USB rescan, which
        # opened devices while the engine was opening one and reconnected it under the window (live view never started)
        self._pending_connect = 0.0
        self._build()
        self.setAcceptDrops(True); Q.QApplication.instance().installEventFilter(self)
        self.exp_mode.setCurrentIndex(1); self.int_spin.setValue(LIVE_START_MS)
        progress(2)
        self._apply_previous_setup()
        if self.cb_weather.isChecked():
            QtCore.QTimer.singleShot(8000, self._weather_sweep)     # runs that closed while no window was open
        progress(3)
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self._poll); self.timer.start(50)
        self.slow = QtCore.QTimer(self); self.slow.timeout.connect(self._slow_tick); self.slow.start(1000)
        if not self._try_attach():
            self._refresh_devices()
            self._when_started(lambda: QtCore.QTimer.singleShot(700, self._offer_resume))
        self._update_schedule()

    # =================================================================== layout
    def _build(self):
        root = Q.QWidget(); self.setCentralWidget(root)
        v = Q.QVBoxLayout(root); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        v.addWidget(self._header())
        body = Q.QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0); v.addLayout(body, 1)
        self.setup = self._left(); body.addWidget(self.setup)
        right = Q.QWidget(); rv = Q.QVBoxLayout(right); rv.setContentsMargins(12, 6, 12, 0); rv.setSpacing(0)
        self.range_bar = self._range_bar()
        self.tabs = Q.QTabWidget(); self.tabs.setDocumentMode(True); rv.addWidget(self.tabs); body.addWidget(right, 1)
        self._tab_spectrum(); self._tab_waterfall(); self._tab_timeseries(); self._tab_schedule(); self._tab_calibration(); self._tab_events()
        self.tabs.currentChanged.connect(self._place_range_bar)
        self.range_bar.setVisible(False)
        self.progress = Q.QProgressBar(); self.progress.setMaximumWidth(260); self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.clock_lbl = Q.QLabel(""); self.statusBar().addPermanentWidget(self.clock_lbl)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+B"), self, activated=lambda: self._set_setup_visible(not self.setup.isVisible()))
        QtGui.QShortcut(QtGui.QKeySequence("F11"), self, activated=self._toggle_full)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, activated=self._save_spectrum)
        self._set_setup_visible(self.settings.value("setup_visible", True, type=bool), remember=False)
        self._set_state("DISCONNECTED")

    def _place_range_bar(self, i):
        """The time-range bar sits at the top of the Waterfall or Time series page, under the tab strip, so the tabs never move."""
        page = self.tabs.widget(i)
        if self.tabs.tabText(i) in ("Waterfall", "Time series"):
            if self.range_bar.parent() is not page:
                page.layout().insertWidget(0, self.range_bar)
            self.range_bar.setVisible(True)
        else:
            self.range_bar.setVisible(False)

    def _range_bar(self):
        """Time range for the Waterfall and Time series tabs: the usable daylight (found automatically), all data, or custom."""
        f = Q.QFrame(); r = Q.QHBoxLayout(f); r.setContentsMargins(0, 4, 0, 6); r.setSpacing(8)
        lab = Q.QLabel("Time range"); lab.setObjectName("secondary"); r.addWidget(lab)
        self.range_mode = Q.QComboBox(); self.range_mode.addItems(["Usable daylight (auto)", "All data", "Custom"])
        self.range_mode.setToolTip(f"Usable daylight: where the 420–700 nm signal stays above {runview.USABLE_FRACTION:.0%} of the "
                                   f"day's bright level for {runview.USABLE_HOLD_S / 60:g} min. Before dawn and after dusk the "
                                   "spectra are dark noise, which swamps the waterfall and the ratios.")
        r.addWidget(self.range_mode)
        self.range_from = Q.QDateTimeEdit(); self.range_to = Q.QDateTimeEdit()
        for w_ in (self.range_from, self.range_to):
            w_.setDisplayFormat("yyyy-MM-dd HH:mm"); w_.setEnabled(False); w_.setKeyboardTracking(False)
        r.addWidget(self.range_from); t = Q.QLabel("to"); t.setObjectName("secondary"); r.addWidget(t); r.addWidget(self.range_to)
        self.btn_range_reset = Q.QPushButton("Reset"); self.btn_range_reset.setToolTip("Back to the usable daylight, found automatically")
        r.addWidget(self.btn_range_reset)
        self.range_info = Q.QLabel(""); self.range_info.setObjectName("secondary"); r.addWidget(self.range_info); r.addStretch(1)
        self.btn_open_rec = Q.QPushButton("Open recorded run…"); self.btn_open_rec.clicked.connect(self._open_recorded_run)
        r.addWidget(self.btn_open_rec)
        self.range_mode.currentIndexChanged.connect(self._range_mode_changed)
        self.btn_range_reset.clicked.connect(lambda: self.range_mode.setCurrentIndex(0))
        for w_ in (self.range_from, self.range_to):
            w_.dateTimeChanged.connect(lambda *_: self._redraw_ranged())
        return f

    def _open_recorded_run(self):
        if self.link is not None or self.state in RUN_STATES or self.run_plan is not None:
            Q.QMessageBox.information(self, APP_NAME, "A run is being recorded or watched. Open a recorded run when it has finished."); return
        if self._load is not None:
            return
        d = Q.QFileDialog.getExistingDirectory(self, "Recorded run folder (contains spectra_*.h5)",
                                               self._stored_location("recorded_run_dir") or self._output_dir())
        if not d:
            return
        if not list(Path(d).glob("spectra_*.h5")):
            Q.QMessageBox.warning(self, APP_NAME, f"{d}\n\nhas no spectra_*.h5 files."); return
        self._start_load(d)

    def _start_load(self, d):
        """Read the run in a worker thread (the files are opened read-only); _poll_load() picks up the result."""
        self._load = dict(dir=d, frac=0.0, result=None)
        def work(job=self._load):
            try:
                job["result"] = runview.load_run(job["dir"], progress=lambda f: job.__setitem__("frac", f))
            except Exception as e:
                job["result"] = e
        threading.Thread(target=work, daemon=True, name="LightKeeperRunLoad").start()
        self.progress.setVisible(True); self.progress.setFormat(f"reading {Path(d).name}  %p%"); self.progress.setValue(0)
        self._load_timer = QtCore.QTimer(self); self._load_timer.timeout.connect(self._poll_load); self._load_timer.start(150)

    def _poll_load(self):
        job = self._load; r = job["result"] if job else None
        if job is None:
            self._load_timer.stop(); return
        if r is None:
            self.progress.setValue(int(100 * job["frac"])); return
        self._load_timer.stop(); self._load = None; self.progress.setVisible(False)
        if isinstance(r, Exception):
            Q.QMessageBox.warning(self, APP_NAME, f"Could not read the run:\n{r!r}"); return
        self._remember_location("recorded_run_dir", r["dir"])
        self.slots = r["slots"]; self.spectra = list(r["spectra"]); self.spec_stride = r["stride"]; self._slot_count = len(self.slots)
        if self.axis is None or len(self.axis) != len(r["wl"]):
            self.axis = np.asarray(r["wl"]).copy(); self.wl_dev = self.axis.copy() if self.wl_dev is None else self.wl_dev
        if self.valid is None or len(self.valid) != len(r["wl"]):
            self.valid = r["valid"]
        self.viewing = r; self.run_dir = r["dir"]; self.a_open_run.setEnabled(True); self.a_close_rec.setEnabled(True)
        self.run_lbl.setText(f"Viewing {r['name']} (read-only)"); self.run_lbl.setToolTip(r["dir"])
        self.m["slots"].setText(f"{r['n_slots']:,}")
        self.range_mode.setCurrentIndex(0)
        a, b, txt = self._time_window()
        self._log(f"opened {r['name']} read-only: {r['n_slots']:,} slots in {len(r['files'])} files, darks from the {r['dark_source']}; "
                  f"showing {txt}")
        self._scrub_t = None; self._draw_recent()
        self._redraw_ranged(); self.tabs.setCurrentIndex(1)

    def _close_recorded_run(self):
        if self.viewing is None:
            return
        self.viewing = None; self.slots.clear(); self.spectra.clear(); self.spec_stride = 1; self._slot_count = 0
        self.run_lbl.setText("No run"); self.run_lbl.setToolTip(""); self.a_close_rec.setEnabled(False); self.m["slots"].setText("–")
        for a in list(self.c_ts.ax) + list(self.c_wf.ax):
            a.cla(); style_ax(a)
        self._wf_shown = None; self._draw_recent()
        self.c_ts.draw_idle(); self.c_wf.draw_idle(); self.range_info.setText("")

    def _range_mode_changed(self, i):
        custom = i == 2
        for w_ in (self.range_from, self.range_to):
            w_.setEnabled(custom)
        if custom and self.slots:            # start the custom range from what is shown now
            a, b, _ = self._time_window(mode=0)
            a = a or self.slots[0]["t"]; b = b or self.slots[-1]["t"]
            for w_, v in ((self.range_from, a), (self.range_to, b)):
                w_.blockSignals(True)
                w_.setDateTime(QtCore.QDateTime(QtCore.QDate(v.year, v.month, v.day), QtCore.QTime(v.hour, v.minute)))
                w_.blockSignals(False)
        self._redraw_ranged()

    def _redraw_ranged(self):
        self._draw_timeseries(); self._draw_waterfall()

    def _time_window(self, mode=None):
        """(start, end, text) of the time range shown; start/end None = no limit."""
        if not self.slots:
            self.range_info.setText(""); return None, None, ""
        mode = self.range_mode.currentIndex() if mode is None else mode
        first, last = self.slots[0]["t"], self.slots[-1]["t"]
        if mode == 1:
            return None, None, f"all data, {first:%H:%M}–{last:%H:%M}"
        if mode == 2:
            q = lambda w_: dt.datetime(w_.date().year(), w_.date().month(), w_.date().day(), w_.time().hour(), w_.time().minute(), tzinfo=LOCAL_TZ)
            a, b = q(self.range_from.dateTime()), q(self.range_to.dateTime())
            return a, b, f"custom, {a:%H:%M}–{b:%H:%M}"
        w_ = runview.usable_window([s["t"] for s in self.slots], [s["par"] for s in self.slots])
        if w_ is None:
            return None, None, f"all data, {first:%H:%M}–{last:%H:%M} (no usable daylight found yet)"
        a, b = w_
        if (last - b).total_seconds() <= runview.USABLE_HOLD_S:       # still light at the end: keep the newest slots
            b = last
        return a, b, f"usable daylight, {a:%H:%M}–{b:%H:%M} (auto)"

    def _in_window(self, items, key):
        a, b, txt = self._time_window()
        self.range_info.setText(f"Showing {txt}" if txt else "")
        if self.range_mode.currentIndex() != 2 and self.slots:     # the from/to fields always show what is drawn
            for w_, v in ((self.range_from, a or self.slots[0]["t"]), (self.range_to, b or self.slots[-1]["t"])):
                w_.blockSignals(True)
                w_.setDateTime(QtCore.QDateTime(QtCore.QDate(v.year, v.month, v.day), QtCore.QTime(v.hour, v.minute)))
                w_.blockSignals(False)
        if a is None:
            return items, txt
        return [x for x in items if a <= key(x) <= b], txt

    def _header(self):
        f = Q.QFrame(); f.setObjectName("strip"); outer = Q.QVBoxLayout(f); outer.setContentsMargins(16, 10, 16, 10); outer.setSpacing(8)
        top = Q.QHBoxLayout(); top.setSpacing(10); outer.addLayout(top)
        ic = Q.QLabel(); ic.setPixmap(icon().pixmap(26, 26)); top.addWidget(ic)
        t = Q.QLabel(APP_NAME); t.setObjectName("appname"); top.addWidget(t)
        self.run_lbl = Q.QLabel("No run"); self.run_lbl.setObjectName("runname"); top.addWidget(self.run_lbl)
        top.addStretch(1)
        self.state_lbl = Q.QLabel(""); self.state_lbl.setObjectName("statechip"); top.addWidget(self.state_lbl)
        self.clock = Q.QLabel(""); self.clock.setObjectName("clock"); self.clock.setMinimumWidth(80)
        self.clock.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter); top.addWidget(self.clock)
        top.addSpacing(8)
        self.btn_setup = Q.QPushButton("Hide setup"); self.btn_setup.setObjectName("flat"); self.btn_setup.setToolTip("Show or hide the setup panel (Ctrl+B)")
        self.btn_setup.clicked.connect(lambda: self._set_setup_visible(not self.setup.isVisible())); top.addWidget(self.btn_setup)
        more = Q.QToolButton(); more.setText("More"); more.setObjectName("flat"); more.setPopupMode(Q.QToolButton.ToolButtonPopupMode.InstantPopup)
        m = Q.QMenu(more)
        self.a_open_run = m.addAction("Open run folder", lambda: self.run_dir and os.startfile(self.run_dir))
        m.addAction("Resume an unfinished run…", self._resume_dialog)
        m.addAction("Open a recorded run (read-only)…", self._open_recorded_run)
        m.addAction("Fetch weather for a run…", self._weather_dialog)
        m.addAction("Open CzernyScope…", self._open_spectrometer_live)
        self.a_close_rec = m.addAction("Close the recorded run", self._close_recorded_run); self.a_close_rec.setEnabled(False)
        m.addSeparator()
        m.addAction("Open log folder", lambda: os.startfile(str(app_home() / "logs")))
        m.addAction("About LightKeeper", self._about)
        more.setMenu(m); top.addWidget(more)
        fs = Q.QPushButton("Full screen"); fs.setObjectName("flat"); fs.setToolTip("F11"); fs.clicked.connect(self._toggle_full); top.addWidget(fs)
        # metrics
        row = Q.QHBoxLayout(); row.setSpacing(28); outer.addLayout(row); self.m = {}
        for key, lab in [("slots", "Slots recorded"), ("int", "Integration, ms"), ("n", "Scans per slot"), ("peak", "Peak counts"),
                         ("sat", "Saturated slots"), ("bursts", "Bursts"), ("darks", "Dark sets"), ("size", "Data written, MB"),
                         ("temp", "Detector, °C")]:
            col = Q.QVBoxLayout(); col.setSpacing(0)
            a = Q.QLabel(lab); a.setObjectName("metric_label"); b = Q.QLabel("–"); b.setObjectName("metric")
            col.addWidget(a); col.addWidget(b); row.addLayout(col); self.m[key] = b
        row.addStretch(1)
        nv = Q.QVBoxLayout(); nv.setSpacing(0)
        self.next_lbl = Q.QLabel(""); self.next_lbl.setObjectName("secondary"); self.next_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.link_lbl = Q.QLabel(""); self.link_lbl.setObjectName("metric_label"); self.link_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        nv.addWidget(self.next_lbl); nv.addWidget(self.link_lbl); row.addLayout(nv)
        return f

    def _open_spectrometer_live(self):
        if self._spectrometer_live_open():
            self.statusBar().showMessage("CzernyScope is already open.", 10000)
            return
        if self.link is not None or self._expect_attach is not None or live_sessions() or watched_runs():
            Q.QMessageBox.information(self, APP_NAME, "CzernyScope needs the USB instrument. "
                                      "Wait for LightKeeper recordings to finish before opening it.")
            return
        if self.state != "DISCONNECTED" or self.engine.state != "DISCONNECTED" or self._pending_connect:
            Q.QMessageBox.information(self, APP_NAME, "Disconnect the spectrometer in LightKeeper first.")
            return

        source = None
        configured = os.environ.get("SPECTROMETER_LIVE_SOURCE") or self.settings.value("spectrometer_live_source", "", type=str)
        if importlib.util.find_spec("spectrometer_app") is None:
            project = Path(configured) if configured else None
            if project is None or not (project / "spectrometer_app" / "__main__.py").is_file():
                folder = Q.QFileDialog.getExistingDirectory(
                    self, "Choose the CzernyScope project folder", configured
                )
                if not folder:
                    return
                project = Path(folder)
            source = next((p for p in (project, project / "src")
                           if (p / "spectrometer_app" / "__main__.py").is_file()), None)
            if source is None:
                Q.QMessageBox.warning(self, APP_NAME, "Choose the folder containing the CzernyScope application.")
                return

        try:
            log = app_home() / "logs" / "spectrometer_live.log"
            process = spawn_detached(
                [gui_python(), "-X", "utf8", "-m", "spectrometer_app"], log, working_dir=source
            )
        except OSError as error:
            Q.QMessageBox.warning(self, APP_NAME, f"Could not open CzernyScope.\n\n{error}")
            return
        self._spectrometer_live = process
        previous_auto = self._auto
        self._auto = False
        if source is not None:
            self.settings.setValue("spectrometer_live_source", str(source))
        self.statusBar().showMessage("Opened CzernyScope.", 10000)

        def check_start():
            code = process.poll()
            if self._spectrometer_live is process and code is not None and code != 0:
                self._auto = previous_auto
                Q.QMessageBox.warning(self, APP_NAME,
                                      f"CzernyScope exited during startup (code {code}).\n\nLog: {log}")

        QtCore.QTimer.singleShot(1500, self, check_start)

    def _spectrometer_live_open(self):
        process = getattr(self, "_spectrometer_live", None)
        return process is not None and process.poll() is None

    def _left(self):
        frame = Q.QFrame(); frame.setObjectName("setup"); frame.setFixedWidth(SETUP_MIN_W)
        fv = Q.QVBoxLayout(frame); fv.setContentsMargins(0, 0, 0, 0); fv.setSpacing(0)
        sc = Q.QScrollArea(); sc.setWidgetResizable(True); sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Keep the viewport width constant as word-wrapped text and optional sections change height.
        sc.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        sc.verticalScrollBar().setSingleStep(12)
        sc.verticalScrollBar().valueChanged.connect(lambda _: setattr(self, "_setup_scrolled", time.monotonic()))
        self.setup_scroll = sc
        w = Q.QWidget(); sc.setWidget(w); lay = Q.QVBoxLayout(w); lay.setContentsMargins(16, 8, 14, 12); lay.setSpacing(6)
        fv.addWidget(sc, 1)

        section(lay, "Spectrometer")
        self.dev_combo = Combo(); lay.addWidget(self.dev_combo)
        self.dev_combo.activated.connect(self._device_picked)
        self.btn_refresh = Q.QPushButton("Refresh"); self.btn_connect = Q.QPushButton("Connect")
        lay.addWidget(hbox(self.btn_refresh, self.btn_connect, stretch_first=False))
        self.cb_edc = Q.QCheckBox("Electric dark correction"); self.cb_nlc = Q.QCheckBox("Nonlinearity correction")
        self.cb_edc.setChecked(True); self.cb_nlc.setChecked(True); lay.addWidget(self.cb_edc); lay.addWidget(self.cb_nlc)
        self.dev_info = Q.QLabel("Close OceanView first: only one program can use the spectrometer.")
        self.dev_info.setObjectName("secondary"); self.dev_info.setWordWrap(True); lay.addWidget(self.dev_info)

        section(lay, "Live view")
        f = form(lay)
        self.exp_mode = Combo(); self.exp_mode.addItems(["Automatic", "Fixed"]); f.addRow("Exposure", self.exp_mode)
        self.int_spin = dspin(1, 60000, 500, 1, 10, "ms"); f.addRow("Integration", self.int_spin)
        self.btn_preview = Q.QPushButton("Start live view"); self.btn_apply = Q.QPushButton("Apply")
        lay.addWidget(hbox(self.btn_preview, self.btn_apply, stretch_first=False))
        self.btn_dark = Q.QPushButton("Take dark set…")
        self.btn_dark.setToolTip("Cover the input: 5 scans at the longest integration time (Acquisition, 'to') find the hot and "
                                 "cold pixels for this spectrometer, plus a matched dark for the live view. No run is needed.")
        lay.addWidget(self.btn_dark)

        section(lay, "Run")
        f = form(lay)
        self.setup_src = Q.QLabel(""); self.setup_src.setObjectName("secondary"); self.setup_src.setWordWrap(True); self.setup_src.setVisible(False)
        f.addRow(self.setup_src)
        self.btn_setup = Q.QToolButton(); self.btn_setup.setText("Load setup from a run…")
        self.btn_setup.setPopupMode(Q.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_setup.setToolTip("Copy the settings of an earlier run into this panel. You can also drop a run folder or its "
                                  "run_metadata.json anywhere on the window.")
        self.setup_menu = Q.QMenu(self.btn_setup); self.setup_menu.aboutToShow.connect(self._fill_setup_menu)
        self.btn_setup.setMenu(self.setup_menu)
        f.addRow(self.btn_setup)
        self.label_edit = Q.QLineEdit(""); self.label_edit.setPlaceholderText("e.g. rooftop_south_diffuser"); f.addRow("Label", self.label_edit)
        self.geom_edit = Q.QLineEdit(""); self.geom_edit.setPlaceholderText("e.g. 50 um fiber + diffuser, facing south"); f.addRow("Geometry", self.geom_edit)
        self.geom_edit.setCursorPosition(0)
        self.notes_edit = Q.QLineEdit(""); self.notes_edit.setPlaceholderText("Sky, window, anything unusual"); f.addRow("Notes", self.notes_edit)
        # 0.3.1 structured setup; a spin box at its minimum shows "not set" and is stored as None
        self.diff_edit = Q.QLineEdit(""); self.diff_edit.setPlaceholderText("Diffuser make, model or your label"); f.addRow("Diffuser", self.diff_edit)
        self.setup_spins = {}
        for key, name, lo, hi, val, dec, suf in (("fiber_core_um", "Fiber core", 0, 2000, None, 0, "µm"),
                                                 ("diffuser_thickness_mm", "Diffuser thickness", 0, 100, None, 2, "mm"),
                                                 ("fiber_diffuser_mm", "Fiber–diffuser", 0, 1000, None, 1, "mm"),
                                                 ("view_az_deg", "View azimuth", 0, 360, None, 1, "°"),
                                                 ("view_el_deg", "View elevation", -90, 90, None, 1, "°"),
                                                 ("site_alt_m", "Site altitude", -500, 9000, None, 0, "m")):
            step = 10 ** -dec
            s = dspin(lo - step, hi, lo - step if val is None else val, dec, step, suf); s.setSpecialValueText("not set")
            self.setup_spins[key] = s; f.addRow(name, s)
        self.window_cb = Q.QComboBox(); self.window_cb.addItems(["", "open", "closed", "screen"]); f.addRow("Window", self.window_cb)
        self.photo_edit = Q.QLineEdit(""); self.photo_edit.setPlaceholderText("optional; copied into the run folder at Start")
        b = Q.QPushButton("Browse…")
        b.clicked.connect(lambda: self.photo_edit.setText(Q.QFileDialog.getOpenFileName(
            self, "Setup photo", "", "Images (*.jpg *.jpeg *.png *.heic);;All files (*)")[0] or self.photo_edit.text()))
        f.addRow("Setup photo", hbox(self.photo_edit, b))
        self._out_location = self._location_record("out_dir", DEFAULT_OUT_DIR)
        self._out_text = str(resolve_path(self._out_location) or "")
        self.out_edit = Q.QLineEdit(self._out_text); b = Q.QPushButton("Browse…")
        self.out_edit.setPlaceholderText("Reconnect the saved drive or choose a folder")
        self.out_edit.editingFinished.connect(self._commit_output_location)
        b.clicked.connect(self._browse_output_location)
        self.out_edit.textChanged.connect(lambda t: self.out_edit.setToolTip(t)); self.out_edit.setToolTip(self.out_edit.text())
        f.addRow("Save in", hbox(self.out_edit, b))

        section(lay, "Schedule")
        self.f_sched = f = form(lay)
        events = list(SUN_EVENTS)
        self.start_mode = Combo(); self.start_mode.addItems(["Now, after the pre-run darks", "At a set time", "At a sun event"])
        self.start_mode.setCurrentIndex(2); f.addRow("Start", self.start_mode)
        now = QtCore.QDateTime.currentDateTime()
        self.start_edit = DateTimeEdit(now.addSecs(600)); self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm"); self.start_edit.setCalendarPopup(True)
        f.addRow("Start time", self.start_edit)
        self.start_event = Combo(); self.start_event.addItems(events[:4]); self.start_event.setCurrentText("nautical dawn (-12°)")
        self.start_off = dspin(-240, 240, 0, 0, 5, "min"); self.start_off.setFixedWidth(84)
        self.row_start_event = hbox(self.start_event, self.start_off); f.addRow("Start event", self.row_start_event)
        self.stop_mode = Combo(); self.stop_mode.addItems(["At a set time", "At a sun event"]); self.stop_mode.setCurrentIndex(1); f.addRow("Stop", self.stop_mode)
        stop = QtCore.QDateTime(now.date(), QtCore.QTime(19, 15)); stop = stop if stop > now else stop.addDays(1)
        self.stop_edit = DateTimeEdit(stop); self.stop_edit.setDisplayFormat("yyyy-MM-dd HH:mm"); self.stop_edit.setCalendarPopup(True)
        f.addRow("Stop time", self.stop_edit)
        self.stop_event = Combo(); self.stop_event.addItems(events[4:]); self.stop_event.setCurrentText("nautical dusk (-12°)")
        self.stop_off = dspin(-240, 240, 0, 0, 5, "min"); self.stop_off.setFixedWidth(84)
        self.row_stop_event = hbox(self.stop_event, self.stop_off); f.addRow("Stop event", self.row_stop_event)
        self.when_lbl = Q.QLabel(""); self.when_lbl.setObjectName("secondary"); self.when_lbl.setWordWrap(True); lay.addWidget(self.when_lbl)

        section(lay, "Acquisition")
        f = form(lay)
        self.slot_spin = dspin(0.05, 600, 1.0, 2, 0.5, "s"); f.addRow("Time slot", self.slot_spin)
        self.run_exp = Combo(); self.run_exp.addItems(["Fixed", "Automatic"])
        self.run_exp.setToolTip("Fixed: every slot at this integration time (default). Automatic: the time moves along the "
                                "ladder between the limits below to keep the signal in range.")
        self.run_int = dspin(1, 60000, 500, 1, 10, "ms")
        self.run_int.setToolTip("Fixed integration time, or the starting time under automatic exposure")
        f.addRow("Exposure", hbox(self.run_exp, self.run_int, stretch_first=False))
        self.min_spin = dspin(1, 60000, 500, 1, 1, "ms"); self.max_spin = dspin(1, 60000, 3000, 0, 100, "ms")
        self.auto_range_row = hbox(self.min_spin, "to", self.max_spin, stretch_first=False)
        f.addRow("Auto range", self.auto_range_row)
        self.run_exp.currentIndexChanged.connect(lambda i: self.auto_range_row.setEnabled(i == 1))
        self.auto_range_row.setEnabled(False)
        self.backoff_step = dspin(0, 1000, 10, 0, 5, "ms"); self.backoff_min = dspin(1, 60000, 10, 0, 5, "ms")
        self.backoff_step.setToolTip("Fixed exposure: after a slot that reaches the step-down level (55,000 counts on a 16-bit "
                                     "detector such as the Maya2000 Pro, 3,437 on a 12-bit USB650), step the integration time down by "
                                     "this much (down to the floor), and back up towards the fixed time after 30 s that would stay "
                                     "under two thirds of full scale. Every step gets a dark set. 0 = off.")
        self.backoff_row = hbox(self.backoff_step, "steps down to", self.backoff_min, stretch_first=False)
        f.addRow("Back off", self.backoff_row)
        self.run_exp.currentIndexChanged.connect(lambda i: self.backoff_row.setEnabled(i == 0))
        self.burst_every = dspin(0, 1440, 15, 0, 15, "min"); self.burst_off = dspin(0, 1439, 7, 0, 5, "min")
        self.burst_every.setToolTip("0 = no bursts")
        self.burst_row1 = hbox(self.burst_every, "at minute", self.burst_off, stretch_first=False)
        f.addRow("Burst every", self.burst_row1)
        self.burst_s = dspin(1, 3600, 60, 0, 10, "s"); self.burst_int = dspin(1, 1000, 50, 0, 1, "ms")
        self.burst_int.setToolTip("The burst's own integration time; it gets its own dark (never longer than the slot time in use). "
                                  "When it reaches the slot time, the slots go on through the burst with no gap.")
        self.burst_row2 = hbox(self.burst_s, "at", self.burst_int, stretch_first=False)
        f.addRow("Burst length", self.burst_row2)
        self.burst_min = dspin(-100000, 60000, 0, 0, 100, "counts")
        self.burst_min.setToolTip("Skip a burst when the raw p99.5 predicted for it is below this. Raw counts sit below zero at night "
                                  "(about -100 at 10 ms), so 0 is about 100 counts above dark. Skips are logged.")
        f.addRow("Burst minimum", self.burst_min)
        self.cb_all = Q.QCheckBox("Save every frame")
        self.cb_all.setToolTip("Keep every individual spectrum of the whole run, not only those in bursts. At 10 ms this is about "
                               "20 GB a day after compression. Scheduled bursts are not needed then.")
        self.cb_all.toggled.connect(lambda on: (self.burst_row1.setEnabled(not on), self.burst_row2.setEnabled(not on), self.burst_min.setEnabled(not on)))
        f.addRow("", self.cb_all)
        self.cb_pre = Q.QCheckBox("Before"); self.cb_post = Q.QCheckBox("After")
        self.cb_pre.setChecked(True); self.cb_post.setChecked(True)
        f.addRow("Dark sets", hbox(self.cb_pre, self.cb_post, stretch_first=False))

        self.btn_adv = Q.QPushButton("Show advanced settings"); self.btn_adv.setObjectName("flat"); self.btn_adv.setCheckable(True)
        lay.addWidget(self.btn_adv, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.adv = Q.QWidget(); av = Q.QVBoxLayout(self.adv); av.setContentsMargins(0, 0, 0, 0); f = form(av)
        self.ladder_edit = Q.QLineEdit(",".join(f"{x:g}" for x in DEFAULT_LADDER_MS)); self.ladder_edit.setCursorPosition(0)
        self.ladder_edit.setToolTip("Integration times (ms) auto-exposure may use; every one gets a dark set"); f.addRow("Ladder, ms", self.ladder_edit)
        self.dark_s = dspin(0.1, 120, 30, 1, 1, "s"); f.addRow("Dark time each", self.dark_s)
        self.long_dark = dspin(0, 60000, 3000, 0, 100, "ms")
        self.long_dark.setToolTip("Fixed exposure also takes darks at this time, so the dark-drift model has the dark current at "
                                  "two integration times. 0 = off.")
        f.addRow("Long dark (fixed)", self.long_dark)
        self.cb_sd = Q.QCheckBox("Store the per-slot standard deviation"); self.cb_sd.setChecked(True); f.addRow("", self.cb_sd)
        self.lat = dspin(-90, 90, float(self.settings.value("site_lat", DEFAULT_SITE[0])), 5, 0.01, "°")
        self.lon = dspin(-180, 180, float(self.settings.value("site_lon", DEFAULT_SITE[1])), 5, 0.01, "°")
        f.addRow("Site", hbox(self.lat, self.lon, stretch_first=False))
        self.lat.setToolTip("Latitude in degrees: north positive, south negative")
        self.lon.setToolTip("Longitude in degrees: east positive, WEST NEGATIVE (the Americas are negative)")
        self.site_lbl = Q.QLabel(""); self.site_lbl.setObjectName("secondary"); self.site_lbl.setWordWrap(True); f.addRow("", self.site_lbl)
        for w_ in (self.lat, self.lon):
            w_.valueChanged.connect(lambda *_: (self._show_site(), self._site_changed()))
        self._show_site()
        self.cb_logon = Q.QCheckBox("Resume after a Windows restart"); self.cb_logon.setChecked(True)
        self.cb_logon.setToolTip("At the next logon. A per-run entry in HKCU\\...\\Run, removed when the run closes or is interrupted on purpose.")
        f.addRow("", self.cb_logon)
        self.cb_weather = Q.QCheckBox("Fetch public weather after a run closes")
        self.cb_weather.setToolTip("Writes weather_vNN.json beside the run, after recording, so it cannot affect an acquisition. This "
                                   "sends the site, rounded to two decimals, and the dates to Open-Meteo and the US National Weather "
                                   "Service, and a nearby airport code to the Iowa Environmental Mesonet.")
        self.cb_weather.setChecked(self.settings.value("fetch_weather", False, type=bool))
        self.cb_weather.toggled.connect(lambda on: self.settings.setValue("fetch_weather", on))
        f.addRow("", self.cb_weather)
        lay.addWidget(self.adv); self.adv.setVisible(False)
        self.btn_adv.toggled.connect(lambda on: (self.adv.setVisible(on), self.btn_adv.setText("Hide advanced settings" if on else "Show advanced settings")))
        lay.addStretch(1)

        foot = Q.QFrame(); foot.setObjectName("setupfoot"); fl = Q.QVBoxLayout(foot); fl.setContentsMargins(16, 10, 14, 12); fl.setSpacing(6)
        line = Q.QFrame(); line.setObjectName("hairline"); fv.addWidget(line)
        self.btn_start = Q.QPushButton("Start run…"); self.btn_start.setObjectName("primary")
        self.btn_stop = Q.QPushButton("Stop run"); self.btn_stop.setObjectName("danger")
        fl.addWidget(hbox(self.btn_start, self.btn_stop))
        self.btn_resume = Q.QPushButton("Resume an unfinished run…"); self.btn_resume.setObjectName("flat"); fl.addWidget(self.btn_resume, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.btn_resume.clicked.connect(self._resume_dialog)
        fv.addWidget(foot)

        self.btn_refresh.clicked.connect(self._refresh_devices); self.btn_connect.clicked.connect(self._connect)
        self.btn_preview.clicked.connect(self._toggle_preview); self.btn_apply.clicked.connect(self._apply_preview)
        self.btn_dark.clicked.connect(self._live_dark)
        self.btn_start.clicked.connect(self._start_run); self.btn_stop.clicked.connect(self._stop_run)
        self.exp_mode.currentIndexChanged.connect(lambda *_: self.int_spin.setEnabled(True))
        for wdg in (self.start_mode, self.stop_mode):
            wdg.currentIndexChanged.connect(self._schedule_rows)
        for wdg in (self.start_mode, self.stop_mode, self.start_event, self.stop_event):
            wdg.currentIndexChanged.connect(self._update_schedule)
        for wdg in (self.start_off, self.stop_off):
            wdg.valueChanged.connect(self._update_schedule)
        for wdg in (self.start_edit, self.stop_edit):
            wdg.dateTimeChanged.connect(self._update_schedule)
        for wdg in (self.slot_spin, self.min_spin, self.max_spin, self.burst_every, self.burst_off, self.burst_s, self.burst_int, self.dark_s,
                    self.run_int, self.long_dark, self.backoff_step, self.backoff_min, self.burst_min):
            wdg.valueChanged.connect(self._update_schedule)
        self.run_exp.currentIndexChanged.connect(self._update_schedule)
        for wdg in (self.cb_sd, self.cb_pre, self.cb_post, self.cb_all):
            wdg.toggled.connect(self._update_schedule)
        self.ladder_edit.editingFinished.connect(self._update_schedule)
        self._schedule_rows()
        w.ensurePolished()
        need = w.minimumSizeHint().width() + sc.verticalScrollBar().sizeHint().width() + 2 * sc.frameWidth()
        frame.setFixedWidth(min(SETUP_MAX_W, max(SETUP_MIN_W, need)))
        w.setSizePolicy(Q.QSizePolicy.Policy.Ignored, Q.QSizePolicy.Policy.Preferred)
        return frame

    def _sat(self) -> float:
        """Saturation level of the connected spectrometer (12-bit USB650: 3,749; 16-bit: 60,000)."""
        return float((self.info or {}).get("saturation_level_counts") or SAT_LEVEL)

    def _store_location(self, key: str, record: dict):
        self.settings.setValue(key + "_location", json.dumps(record))
        self.settings.setValue(key, record["path"])

    def _location_record(self, key: str, default: str = "") -> dict:
        saved = self.settings.value(key + "_location", "", type=str)
        if saved:
            try:
                record = json.loads(saved)
                if isinstance(record, dict) and isinstance(record.get("path"), str):
                    return record
            except ValueError:
                pass
            return {"path": ""}
        legacy = self.settings.value(key, default, type=str)
        if legacy and (not self.settings.contains(key) or Path(legacy).exists()):
            record = remember_path(legacy)
            self._store_location(key, record)
            return record
        return {"path": legacy, "unverified_legacy_path": bool(legacy)}

    def _remember_location(self, key: str, path: str | Path) -> dict:
        record = remember_path(path)
        self._store_location(key, record)
        return record

    def _stored_location(self, key: str, default: str = "") -> str:
        record = self._location_record(key, default)
        path = str(resolve_path(record) or "")
        if path and path != record["path"]:
            self._store_location(key, dict(record, path=path))
        return path

    def _output_dir(self) -> str:
        text = self.out_edit.text()
        if text != self._out_text:  # Leave partially typed text and its cursor alone.
            return text.strip()
        self._out_text = str(resolve_path(self._out_location) or "")
        if self._out_text != text:
            self.out_edit.setText(self._out_text)
        if self._out_text and self._out_text != self._out_location["path"]:
            self._out_location = dict(self._out_location, path=self._out_text)
            self._store_location("out_dir", self._out_location)
        return self._out_text

    def _commit_output_location(self):
        text = self.out_edit.text()
        if text != self._out_text:
            self._out_location = self._remember_location("out_dir", text.strip())
            self._out_text = text

    def _browse_output_location(self):
        chosen = Q.QFileDialog.getExistingDirectory(self, "Save runs in", self._output_dir())
        if chosen:
            self.out_edit.setText(chosen)
            self._commit_output_location()

    def _apply_previous_setup(self):
        """Fill the setup panel from the newest run in the Save in folder, so setting up a run takes one press of Start.
        Dates and times are worked out afresh; a label ending in 'day N' counts up. Every field can still be edited."""
        last, _ = self._last_runs("")
        p = (last or {}).get("plan")
        if not p or last.get("_folder") == self._setup_from:
            return
        self._apply_setup(last, last.get("_folder"), "Setup copied from the previous run")

    # 0.4.0: a setup from any earlier run: the Load setup menu, or a run_metadata.json / run folder dropped on the window
    @staticmethod
    def _read_setup(path) -> tuple[dict | None, str]:
        """(metadata with a 'plan', name) from a run folder, run_metadata.json, run_state.json or a bare plan .json."""
        path = Path(path)
        if path.is_dir():
            path = path / "run_metadata.json"
        m = read_json(path) if path.suffix.lower() == ".json" and path.is_file() else None
        if not isinstance(m, dict):
            return None, ""
        name = path.parent.name if path.name in ("run_metadata.json", "run_state.json") else path.name
        if isinstance(m.get("plan"), dict):
            return m, name
        if "slot_s" in m and "label" in m:            # a plan file (pending/plan_*.json, or saved by hand)
            return dict(plan=m, app=""), name
        return None, name

    def _import_setup(self, path) -> bool:
        """Ask, then replace the setup panel with an earlier run's settings. False if nothing was applied."""
        if self.state in RUN_STATES or self.link is not None:
            Q.QMessageBox.information(self, APP_NAME, "A run is open, so its setup cannot be replaced now.\n\n"
                                      "Load the setup again after the run has finished."); return False
        meta, name = self._read_setup(path)
        if meta is None:
            Q.QMessageBox.warning(self, APP_NAME, f"No run settings found in\n{path}\n\nDrop a run folder, its run_metadata.json, "
                                  "or a plan .json."); return False
        p = meta["plan"]
        when = str(meta.get("planned_start_local") or p.get("resolved_start_local") or "")[:16].replace("T", " ")
        exp = "automatic exposure" if p.get("auto_exposure") else f"fixed {float(p.get('int_ms', 0)):g} ms"
        dev = meta.get("device") or {}
        lines = [f"Run: {name}", f"Label: {p.get('label', '')}", f"Recorded: {when or 'unknown'}",
                 f"Spectrometer: {dev.get('model', '?')} {dev.get('serial', '')}".rstrip(), f"Exposure: {exp}",
                 f"Geometry: {p.get('geometry') or '-'}", f"Site: {p.get('site_lat')}, {p.get('site_lon')}"]
        if not self._ask("Load setup", "Replace the current setup with this run's settings?\n\n" + "\n".join(lines) +
                         "\n\nStart and stop keep their rule (fixed time or sun event); the dates are worked out afresh. "
                         "Everything can still be edited before Start.", "Replace setup", "Keep current"):
            return False
        self._apply_setup(meta, str(path), f"Setup copied from {name}")
        self._remember_setup_source(str(path))
        return True

    def _remember_setup_source(self, path: str):
        record = remember_path(path)
        key = location_key(record)
        recent = [x for x in self._recent_setup_locations() if location_key(x) != key]
        self.settings.setValue("setup_recent_locations", json.dumps([record, *recent[:9]]))

    def _recent_setup_locations(self) -> list[dict]:
        saved = self.settings.value("setup_recent_locations", "", type=str)
        if saved:
            try:
                return [x for x in json.loads(saved) if isinstance(x, dict) and x.get("path")]
            except (TypeError, ValueError):
                return []
        legacy = self.settings.value("setup_recent", []) or []
        if isinstance(legacy, str):
            legacy = [legacy]
        recent = [remember_path(path) if Path(path).exists() else {"path": path} for path in legacy if path]
        if recent:
            self.settings.setValue("setup_recent_locations", json.dumps(recent))
        return recent

    def _fill_setup_menu(self):
        """Recent runs in the Save in folder, setups loaded before, and Browse."""
        m = self.setup_menu; m.clear()
        metas = self._run_metas()[::-1][:12]
        if metas:
            m.addSection("Runs in the Save in folder")
        for meta in metas:
            p = meta.get("plan", {})
            exp = "auto" if p.get("auto_exposure") else f"{float(p.get('int_ms', 0)):g} ms"
            dev = (meta.get("device") or {}).get("model", "")
            a = m.addAction(f"{meta['_folder']}   ({dev} {exp})".replace("  ( ", "  ("))
            a.triggered.connect(lambda _=False, d=meta["_dir"]: self._import_setup(d))
        here = {Path(meta["_dir"]) for meta in metas}
        rec = [path for x in self._recent_setup_locations()
               if (path := resolve_path(x)) and path.exists() and path not in here and path.parent not in here]
        if rec:
            m.addSection("Loaded before")
            for x in rec[:8]:
                a = m.addAction(Path(x).parent.name if Path(x).name == "run_metadata.json" else Path(x).name)
                a.setToolTip(str(x)); a.triggered.connect(lambda _=False, x=x: self._import_setup(x))
        m.addSeparator()
        m.addAction("Browse for run_metadata.json…", self._browse_setup)

    def _browse_setup(self):
        fn, _ = Q.QFileDialog.getOpenFileName(self, "Load setup from a run", self._output_dir(),
                                              "Run settings (run_metadata.json *.json)")
        if fn:
            self._import_setup(fn)

    @staticmethod
    def _dropped_setup(ev):
        """The first local run folder or .json in a drag, or None."""
        md = ev.mimeData()
        if not md or not md.hasUrls():
            return None
        for u in md.urls():
            f = Path(u.toLocalFile()) if u.isLocalFile() else None
            if f and (f.suffix.lower() == ".json" or (f.is_dir() and (f / "run_metadata.json").exists())):
                return f
        return None

    def eventFilter(self, obj, ev):
        """Drops of a run's settings anywhere on the window, line edits and plots included."""
        et = ev.type()
        if et in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove, QtCore.QEvent.Type.Drop) and \
                isinstance(obj, Q.QWidget) and obj.window() is self and self._dropped_setup(ev) is not None:
            ev.setDropAction(QtCore.Qt.DropAction.CopyAction); ev.accept()
            if et == QtCore.QEvent.Type.Drop:
                f = self._dropped_setup(ev)
                QtCore.QTimer.singleShot(0, lambda: (self.raise_(), self.activateWindow(), self._import_setup(f)))
            return True
        return super().eventFilter(obj, ev)

    def _apply_setup(self, last: dict, source: str, headline: str):
        p = last["plan"]
        self._setup_from = source
        self._applying_setup = True
        try:
            def put(key, fn):
                if key in p and p[key] is not None:
                    fn(p[key])
            put("label", lambda v: self.label_edit.setText(next_label(str(v))))
            put("geometry", self.geom_edit.setText); put("notes", self.notes_edit.setText); put("diffuser_id", self.diff_edit.setText)
            for k, s in self.setup_spins.items():
                if k in p:
                    s.setValue(s.minimum() if p[k] is None else float(p[k]))
            put("window_state", self.window_cb.setCurrentText)
            put("setup_photo", lambda v: self.photo_edit.setText(v if v and Path(v).exists() else ""))
            for k, s in (("slot_s", self.slot_spin), ("int_ms", self.run_int), ("min_int_ms", self.min_spin), ("max_int_ms", self.max_spin),
                         ("backoff_step_ms", self.backoff_step), ("backoff_min_ms", self.backoff_min), ("burst_every_min", self.burst_every),
                         ("burst_offset_min", self.burst_off), ("burst_s", self.burst_s), ("burst_max_int_ms", self.burst_int), ("burst_min_counts", self.burst_min),
                         ("dark_s_per_int", self.dark_s), ("long_dark_ms", self.long_dark), ("site_lat", self.lat), ("site_lon", self.lon)):
                put(k, lambda v, s=s: s.setValue(float(v)))
            put("auto_exposure", lambda v: self.run_exp.setCurrentIndex(1 if v else 0))
            put("ladder_ms", lambda v: self.ladder_edit.setText(",".join(f"{float(x):g}" for x in v)))
            for k, c in (("edc", self.cb_edc), ("nlc", self.cb_nlc), ("store_sd", self.cb_sd), ("pre_darks", self.cb_pre),
                         ("post_darks", self.cb_post), ("auto_resume_logon", self.cb_logon), ("save_all_frames", self.cb_all)):
                put(k, lambda v, c=c: c.setChecked(bool(v)))
            if old_acquisition_defaults(last.get("app", ""), p):      # old defaults, not a choice: the new ones apply from here on
                self.burst_every.setValue(15); self.burst_off.setValue(7); self.burst_s.setValue(60); self.dark_s.setValue(30); self.burst_min.setValue(0)
                if float(p.get("backoff_min_ms", 0)) >= float(p.get("int_ms", 0)):    # a back-off with nowhere to go does nothing
                    self.backoff_step.setValue(0)
            # the schedule: the same choice of start and stop, with the dates worked out afresh
            old_start, old_stop = old_default_times(last.get("app", ""), p)
            now = dt.datetime.now(LOCAL_TZ); to_q = lambda t: QtCore.QDateTime(QtCore.QDate(t.year, t.month, t.day), QtCore.QTime(t.hour, t.minute))  # noqa: E731
            start = now
            if not old_start:
                if p.get("start_now"):
                    self.start_mode.setCurrentIndex(0)
                elif p.get("start_event") and self.start_event.findText(p["start_event"]) >= 0:
                    self.start_mode.setCurrentIndex(2); self.start_event.setCurrentText(p["start_event"])
                    self.start_off.setValue(float(p.get("start_offset_min", 0)))
                elif p.get("start_local"):
                    start = next_clock_time(p["start_local"], now); self.start_mode.setCurrentIndex(1); self.start_edit.setDateTime(to_q(start))
            if not old_stop:
                if p.get("stop_event") and self.stop_event.findText(p["stop_event"]) >= 0:
                    self.stop_mode.setCurrentIndex(1); self.stop_event.setCurrentText(p["stop_event"])
                    self.stop_off.setValue(float(p.get("stop_offset_min", 0)))
                elif p.get("stop_local"):
                    self.stop_mode.setCurrentIndex(0); self.stop_edit.setDateTime(to_q(next_clock_time(p["stop_local"], start)))
        finally:
            self._applying_setup = False
        self.label_edit.setCursorPosition(0); self.geom_edit.setCursorPosition(0)
        self.auto_range_row.setEnabled(self.run_exp.currentIndex() == 1); self.backoff_row.setEnabled(self.run_exp.currentIndex() == 0)
        self._schedule_rows(); self._show_site()
        self.setup_src.setText(headline); self.setup_src.setToolTip(f"{self._setup_from}\nDates and times are new. Edit anything before pressing Start.")
        self.setup_src.setVisible(True)
        self._log(f"{headline.lower()}, {self._setup_from}")
        self._update_schedule()

    def _schedule_rows(self, *_):
        """Show only the schedule fields that apply to the chosen start and stop modes."""
        f = self.f_sched; sm = self.start_mode.currentIndex(); stm = self.stop_mode.currentIndex()
        f.setRowVisible(self.start_edit, sm == 1); f.setRowVisible(self.row_start_event, sm == 2)
        f.setRowVisible(self.stop_edit, stm == 0); f.setRowVisible(self.row_stop_event, stm == 1)

    def _set_setup_visible(self, on, remember=True):
        self.setup.setVisible(bool(on)); self.btn_setup.setText("Hide setup" if on else "Show setup")
        if remember:
            self.settings.setValue("setup_visible", bool(on))

    def _toggle_full(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _about(self):
        Q.QMessageBox.about(self, f"About {APP_NAME}", f"<b>{APP_NAME} {VERSION}</b><br>Time-resolved spectroscopy logger for Ocean "
                            "spectrometers (OceanDirect SDK).<br><br>Runs are recorded by a background process with its own "
                            "notification-area icon; this window can be closed at any time.<br><br>"
                            f"Logs: {app_home() / 'logs'}")

    # ---- tabs
    def _tab_spectrum(self):
        w = Q.QWidget(); v = Q.QVBoxLayout(w); v.setContentsMargins(0, 6, 0, 0); v.setSpacing(4)
        self.c_spec = Canvas(3, 1, [2.0, 0.06, 1.0], sharex=True)
        self.ax_strip = self.c_spec.ax.pop(1)
        r = Q.QHBoxLayout(); r.setSpacing(8)
        self.spec_tb = plot_toolbar(self.c_spec, self); r.addWidget(self.spec_tb)
        sep = Q.QFrame(); sep.setFrameShape(Q.QFrame.Shape.VLine); sep.setStyleSheet(f"color: {LINE};"); r.addWidget(sep)
        self.cb_auto = Q.QCheckBox("Auto-scale"); self.cb_auto.setChecked(True)
        self.cb_auto.setToolTip("Fit both axes to the data on every update. Zooming with the wheel or the toolbar turns it off; "
                                "double-click the plot to turn it back on.")
        r.addWidget(self.cb_auto)
        self.btn_fity = Q.QPushButton("Fit y"); self.btn_fity.setToolTip("Stretch the y-axes to the data in the visible wavelength range")
        self.btn_fity.clicked.connect(self._fit_y_now); r.addWidget(self.btn_fity)
        self.btn_fitxy = Q.QPushButton("Fit X/Y")
        self.btn_fitxy.setToolTip("Restore the display wavelength range and fit both y-axes.")
        self.btn_fitxy.clicked.connect(self._fit_xy_now); r.addWidget(self.btn_fitxy)
        self.btn_full = Q.QPushButton("Full scale")
        self.btn_full.setToolTip("Display wavelength range, counts from 0 to just above the detector's largest count (64,000 on a "
                                 "Maya2000 Pro, 4,095 on a USB650), so the saturation line is always in view. It stays at full "
                                 "scale until you zoom, press Fit y or tick Auto-scale.")
        self.btn_full.clicked.connect(self._full_scale); r.addWidget(self.btn_full)
        disp = Q.QToolButton(); disp.setText("Display"); disp.setObjectName("flat"); disp.setPopupMode(Q.QToolButton.ToolButtonPopupMode.InstantPopup)
        dm = Q.QMenu(disp)
        self.cb_dark = dm.addAction("Subtract the matched dark"); self.cb_rate = dm.addAction("Counts per second")
        self.cb_log = dm.addAction("Logarithmic y-axis"); self.cb_mask = dm.addAction("Hide hot and cold pixels")
        self.cb_edges = dm.addAction("Show all detector channels")
        self.cb_cal = dm.addAction("Calibrated wavelength axis")
        for a in (self.cb_dark, self.cb_rate, self.cb_log, self.cb_mask, self.cb_edges, self.cb_cal):
            a.setCheckable(True)
        self.cb_edges.toggled.connect(self._fit_xy_now)
        self.cb_mask.setChecked(self.settings.value("hide_hot_pixels", True, type=bool)); self.cb_cal.setEnabled(False)
        self.cb_mask.toggled.connect(lambda on: (self.settings.setValue("hide_hot_pixels", on), self._draw_waterfall()))
        dm.addSeparator()
        hint = dm.addAction("Wheel stretches y, Shift+wheel zooms x, Ctrl+wheel both; double-click resets"); hint.setEnabled(False)
        hint2 = dm.addAction("Pointing at a plot shows the wavelength, pixel and counts there"); hint2.setEnabled(False)
        disp.setMenu(dm); r.addWidget(disp)
        self.btn_save_spec = Q.QPushButton("Save spectrum")
        self.btn_save_spec.setToolTip("Save the current live scan (Ctrl+S) as a two-column .dpt plus a .json. "
                                      "The export includes all detector channels and remembers the last chosen folder.")
        self.btn_save_spec.clicked.connect(self._save_spectrum); r.addWidget(self.btn_save_spec)
        self.btn_repeat_spec = Q.QPushButton("Repeat save…")
        self.btn_repeat_spec.setToolTip("Save a series of distinct live scans as .dpt and .json files.")
        self.btn_repeat_spec.clicked.connect(self._toggle_repeat_save); r.addWidget(self.btn_repeat_spec)
        self._repeat_timer = QtCore.QTimer(self)
        self._repeat_timer.setSingleShot(True)
        self._repeat_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._repeat_timer.timeout.connect(self._save_next_spectrum)
        self.cb_spec_cur = Q.QCheckBox("Cursor")
        self.cb_spec_cur.setToolTip("Click or drag on a plot to read the wavelength. Arrow keys move one pixel, Shift+arrow ten.")
        r.addWidget(self.cb_spec_cur)
        # 0.4.2: the cursor and pointer readout gets its own line under the toolbar (long text must not widen the window)
        self.spec_cur_info = Q.QLabel(""); self.spec_cur_info.setSizePolicy(Q.QSizePolicy.Policy.Ignored, Q.QSizePolicy.Policy.Fixed)
        self.spec_cur_info.setFixedHeight(self.spec_cur_info.fontMetrics().height() + 4)
        for c in (self.cb_auto, self.cb_dark, self.cb_rate, self.cb_log, self.cb_mask, self.cb_cal):
            c.toggled.connect(lambda *_: self._draw_live(force=True))
        self.live_info = Q.QLabel(""); self.live_info.setObjectName("secondary"); self.live_info.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.live_info.setSizePolicy(Q.QSizePolicy.Policy.Ignored, Q.QSizePolicy.Policy.Preferred); r.addWidget(self.live_info, 1)   # long text must not widen the window
        v.addLayout(r); v.addWidget(self.spec_cur_info); v.addWidget(self.c_spec, 1)
        tr = Q.QHBoxLayout(); tr.setContentsMargins(8, 0, 8, 2); tr.setSpacing(8)
        self.scrub_first = Q.QLabel(""); self.scrub_first.setObjectName("secondary"); tr.addWidget(self.scrub_first)
        self.scrub = JumpSlider(QtCore.Qt.Orientation.Horizontal); self.scrub.setEnabled(False)
        self.scrub.setToolTip("Slide through the recorded slots. Far left: start of usable light. Far right: present, or the end of the run.")
        self.scrub.valueChanged.connect(self._scrub_moved); tr.addWidget(self.scrub, 1)
        self.scrub_last = Q.QLabel(""); self.scrub_last.setObjectName("secondary"); tr.addWidget(self.scrub_last)
        v.addLayout(tr)
        self._scrub_timer = QtCore.QTimer(self); self._scrub_timer.setSingleShot(True); self._scrub_timer.setInterval(40)
        self._scrub_timer.timeout.connect(self._draw_recent)
        self.spec_cursor = WavelengthCursor(self.c_spec, self.spec_tb, lambda: list(self.c_spec.ax) + [self.ax_strip], self._cursor_pixels,
                                            self._spec_cursor_text)
        self.cb_spec_cur.toggled.connect(self.spec_cursor.enable)
        self._hover_i = None; self._live_shown = None; self._recent_full = None; self._full_hold = False
        self.c_spec.mpl_connect("motion_notify_event", self._spec_hover)
        self.c_spec.mpl_connect("figure_leave_event", lambda ev: self._spec_hover(None))
        self.cb_auto.toggled.connect(lambda on: setattr(self, "_full_hold", self._full_hold and not on))
        self.c_spec.mpl_connect("scroll_event", self._on_scroll)
        self.c_spec.mpl_connect("button_press_event", self._on_click)
        self.c_spec.mpl_connect("button_release_event", self._on_release)
        a0, a1 = self.c_spec.ax
        self.l_live, = a0.plot([], [], color=LAMP, lw=0.9, label="Current scan")
        self.l_dark, = a0.plot([], [], color=VIOLET, lw=0.7, label="Matched dark")
        self.l_sat = a0.axhline(SAT_LEVEL, color=RED, ls=(0, (4, 3)), lw=0.8, alpha=0.8, label="Saturation, 60,000")
        a0.set_title("Live scan"); a0.set_ylabel("Counts"); a0.legend(loc="upper right")
        a0.tick_params(labelbottom=False)
        s = self.ax_strip
        s.set_axis_off()
        a1.set_title("Recent slots, newest brightest"); a1.set_xlabel("Wavelength (nm)"); a1.set_ylabel("Dark-corrected counts/s")
        a1.text(0.5, 0.5, "Recorded slots appear here during a run", transform=a1.transAxes, ha="center", va="center", color=TEXT3)
        a1.set_yticks([])
        self.tabs.addTab(w, "Spectrum")

    def _draw_strip(self):
        x = self._x()[self._spectrum_channels()] if self.latest is not None or self.axis is not None else None
        if x is None or len(x) < 2:
            return
        xl = self.c_spec.ax[0].get_xlim()
        s = self.ax_strip; s.cla(); s.set_axis_off()
        s.imshow(theme.wavelength_rgb(x)[None, :, :], aspect="auto", extent=[x[0], x[-1], 0, 1], interpolation="bilinear")
        s.set_ylim(0, 1)
        self.c_spec.ax[0].set_xlim(xl)

    def _tab_waterfall(self):
        w = Q.QWidget(); v = Q.QVBoxLayout(w); v.setContentsMargins(0, 6, 0, 0); r = Q.QHBoxLayout()
        self.wf_mode = Q.QComboBox(); self.wf_mode.addItems(["Spectral shape, normalised at 780–820 nm", "Log₁₀ counts/s", "Ratio to the first slot", "Raw counts, as recorded"])
        self.wf_mode.currentIndexChanged.connect(lambda *_: self._draw_waterfall())
        self.c_wf = Canvas(1, 1); self.wf_tb = plot_toolbar(self.c_wf, self); r.addWidget(self.wf_tb); r.addSpacing(8)
        lab = Q.QLabel("Show"); lab.setObjectName("secondary"); r.addWidget(lab); r.addWidget(self.wf_mode); r.addSpacing(8)
        self.cb_wf_cur = Q.QCheckBox("Cursor"); self.cb_wf_cur.setToolTip("Click or drag on the plot to read the wavelength, time and value. Arrow keys move one pixel, Shift+arrow ten.")
        r.addWidget(self.cb_wf_cur); self.wf_cur_info = Q.QLabel(""); r.addWidget(self.wf_cur_info); r.addStretch(1); v.addLayout(r)
        self.wf_cursor = WavelengthCursor(self.c_wf, self.wf_tb, lambda: list(self.c_wf.ax), lambda: None if self._wf_shown is None else self._wf_shown[0],
                                          self._wf_cursor_text, with_time=True)
        self.cb_wf_cur.toggled.connect(self.wf_cursor.enable)
        v.addWidget(self.c_wf, 1); self.wf_cbar = None
        self.c_wf.ax[0].set_title("Recorded spectra over time")
        self.tabs.addTab(w, "Waterfall")

    TS_SERIES = (("par", "420–700 nm rate", 1.4), ("rfr", "R:FR", 1.0), ("bg", "B:G", 1.0),
                 ("frshare", "Far-red share", 1.0), ("nirred", "NIR/red", 1.0), ("o2a", "O₂-A depth", 1.0),
                 ("int", "Integration and scans", 1.0))
    TS_DEFAULTS = ("par", "rfr", "bg", "int")

    def _tab_timeseries(self):
        w = Q.QWidget(); v = Q.QVBoxLayout(w); v.setContentsMargins(0, 6, 0, 0)
        self.c_ts = Canvas(1, 1)
        r = Q.QHBoxLayout(); r.addWidget(plot_toolbar(self.c_ts, self)); r.addSpacing(12); r.addWidget(Q.QLabel("Show:"))
        saved = self.settings.value("ts_shown", None)
        saved = [saved] if isinstance(saved, str) else (list(saved) if saved else self.TS_DEFAULTS)
        if not self.settings.value("ts_defaults_044", False, type=bool):
            if set(saved) == {"par", "rfr", "nirred", "o2a", "int"}:
                saved = self.TS_DEFAULTS
            self.settings.setValue("ts_defaults_044", True)
        self.ts_cbs = {}
        for k, lab, _ in self.TS_SERIES:
            cb = Q.QCheckBox(lab); cb.setChecked(k in saved); cb.toggled.connect(self._ts_layout); self.ts_cbs[k] = cb; r.addWidget(cb)
        r.addStretch(1); v.addLayout(r); v.addWidget(self.c_ts, 1)
        self._ts_twin = None
        self._ts_layout()
        self.tabs.addTab(w, "Time series")

    def _ts_layout(self, *_):
        """0.4.0: one panel per chosen series; the chosen ones share the height. At least one stays on."""
        keys = [k for k, _, _ in self.TS_SERIES if self.ts_cbs[k].isChecked()]
        if not keys:
            cb = self.sender() if isinstance(self.sender(), Q.QCheckBox) else self.ts_cbs["par"]
            cb.blockSignals(True); cb.setChecked(True); cb.blockSignals(False)
            keys = [k for k, _, _ in self.TS_SERIES if self.ts_cbs[k].isChecked()]
        self.settings.setValue("ts_shown", keys)
        fig = self.c_ts.fig; fig.clear(); self._ts_twin = None
        ratios = [r for k, _, r in self.TS_SERIES if k in keys]
        gs = fig.add_gridspec(len(keys), 1, height_ratios=ratios)
        axs = []
        for i in range(len(keys)):
            a = fig.add_subplot(gs[i, 0], sharex=axs[0] if axs else None); style_ax(a); axs.append(a)
        self.c_ts.ax = axs; self.ts_ax = dict(zip(keys, axs))
        titles = dict(par="420–700 nm rate (counts/s, instrument-relative)", rfr="R:FR counts (655–665 / 725–735)",
                      bg="B:G counts (420–490 / 500–570 nm)", frshare="Far-red share of counts (700–750 / 400–750 nm)",
                      nirred="NIR/red (780–820 / 665–680)", o2a="O₂-A apparent depth (760 nm)", int="Integration time and scans per slot")
        for k, a in self.ts_ax.items():
            a.set_title(titles[k], loc="left", fontsize=9)
        if self.slots:
            self._draw_timeseries()
        else:
            self.c_ts.draw_idle()

    def _tab_schedule(self):
        w = Q.QWidget(); h = Q.QHBoxLayout(w); h.setContentsMargins(0, 6, 0, 0)
        left = Q.QVBoxLayout(); left.setContentsMargins(0, 0, 0, 0)
        self.sch_notice = Q.QLabel("Updating the schedule for the new site…"); self.sch_notice.setStyleSheet(f"color: {ORANGE}; padding: 4px 0")
        self.sch_notice.setVisible(False); left.addWidget(self.sch_notice)
        self.c_sch = Canvas(1, 1); left.addWidget(self.c_sch, 1); h.addLayout(left, 3)
        self.sch_text = Q.QTextBrowser(); self.sch_text.setMinimumWidth(360); self.sch_text.setMaximumWidth(460)
        self.sch_text.setStyleSheet(f"QTextBrowser {{ background: {SURFACE}; border: none; padding: 10px; }}"); h.addWidget(self.sch_text, 1)
        self.tabs.addTab(w, "Schedule")

    def _tab_calibration(self):
        w = Q.QWidget(); h = Q.QHBoxLayout(w); h.setContentsMargins(0, 8, 0, 0)
        p, l = panel("Emission-line calibration"); p.setFixedWidth(340); f = form(l)
        self.lamp_combo = Combo(); self.lamp_combo.addItems(list(LAMPS)); f.addRow("Lamp", self.lamp_combo)
        self.cal_int = dspin(1, 60000, 50, 1, 5, "ms"); f.addRow("Integration", self.cal_int)
        self.cal_n = IntSpin(); self.cal_n.setRange(1, 1000); self.cal_n.setValue(50); f.addRow("Scans", self.cal_n)
        self.cal_minrel = dspin(0.001, 1, 0.02, 3, 0.01); f.addRow("Min. NIST rel. intensity", self.cal_minrel)
        self.cal_iso = dspin(0.1, 5, 1.0, 2, 0.1, "nm"); f.addRow("Isolation", self.cal_iso)
        self.cal_snr = dspin(3, 500, 15, 0, 5); f.addRow("Peak S/N", self.cal_snr)
        self.cal_deg = IntSpin(); self.cal_deg.setRange(1, 4); self.cal_deg.setValue(3); f.addRow("Polynomial degree", self.cal_deg)
        self.cal_btn = {}
        for key, txt, fn, obj in [("dark", "1. Acquire dark, lamp off", lambda: self._cal_acquire("dark"), ""),
                                  ("lamp", "2. Acquire lamp", lambda: self._cal_acquire("lamp"), ""),
                                  ("fit", "3. Fit to NIST lines", self._cal_fit, ""), ("save", "Save calibration", self._cal_save, ""),
                                  ("use", "Use for the display axis", self._cal_use, ""), ("load", "Load calibration…", self._cal_load, "flat")]:
            b = Q.QPushButton(txt); b.clicked.connect(fn); b.setObjectName(obj); l.addWidget(b); self.cal_btn[key] = b
        self.cal_btn["dark"].setToolTip("Lamp off or input covered. Darks at every integration time of the ladder and the "
                                        "calibration time, and the hot and cold pixels from the longest one.")
        self.cal_darks = {}; self._cal_saved = False
        self.cal_int.valueChanged.connect(lambda *_: self._cal_steps())
        self.lamp_combo.currentIndexChanged.connect(lambda *_: (self.cal.pop("lamp", None), setattr(self, "cal_result", None), self._cal_steps()))
        self.cal_text = Q.QPlainTextEdit(); self.cal_text.setReadOnly(True); self.cal_text.setStyleSheet("font-family: Consolas; font-size: 9pt;")
        l.addWidget(self.cal_text, 1)
        h.addWidget(p)
        v = Q.QVBoxLayout(); self.c_cal = Canvas(2, 1, [2.2, 1], sharex=True)
        r = Q.QHBoxLayout(); r.addWidget(plot_toolbar(self.c_cal, self))
        b = Q.QPushButton("Fit y"); b.setToolTip("Stretch the y-axes to the data in the visible wavelength range"); b.clicked.connect(self._cal_fit_y)
        r.addWidget(b); hint = Q.QLabel("Wheel stretches y, Shift+wheel zooms x, Ctrl+wheel both; double-click resets"); hint.setObjectName("secondary")
        r.addWidget(hint); r.addStretch(1); v.addLayout(r); v.addWidget(self.c_cal, 1); h.addLayout(v, 1)
        self.c_cal.mpl_connect("scroll_event", self._on_scroll); self.c_cal.mpl_connect("button_press_event", self._on_click)
        self.c_cal.ax[0].set_title("Lamp spectrum, dark-subtracted, with NIST reference lines")
        self.c_cal.ax[1].set_title("Fit residual (NIST − fit)")
        self.tabs.addTab(w, "Calibration")
        self._cal_steps()

    def _cal_step(self) -> str:
        """The next calibration step: darks covering the calibration time -> lamp -> fit -> save."""
        us = int(round(self.cal_int.value() * 1000))
        if not any(abs(k - us) <= max(1, us * 0.01) for k in self.cal_darks):
            return "dark"
        lamp = self.cal.get("lamp")
        if lamp is None or abs(lamp["int_us"] - us) > max(1, us * 0.01):
            return "lamp"
        if not (self.cal_result and self.cal_result.get("ok")):
            return "fit"
        return "" if self._cal_saved else "save"

    def _cal_steps(self):
        nxt = self._cal_step()
        for k, b in self.cal_btn.items():
            if k == "load":
                continue
            want = "primary" if k == nxt else ""
            if b.objectName() != want:
                b.setObjectName(want); b.style().unpolish(b); b.style().polish(b)

    def _tab_events(self):
        self.log_box = Q.QPlainTextEdit(); self.log_box.setReadOnly(True); self.log_box.setStyleSheet("font-family: Consolas; font-size: 9pt;")
        self.tabs.addTab(self.log_box, "Log")

    # =================================================================== state & controls
    def _set_state(self, s, detail=""):
        prev = self.state
        self.state = s
        col, txt = STATE.get(s, (TEXT3, s.title()))
        self.state_lbl.setText(f"<span style='color:{col}; font-size:13pt'>●</span>&nbsp;&nbsp;{txt}")
        run_active = s in RUN_STATES or self.link is not None
        connected = s != "DISCONNECTED" and self.link is None
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        self.btn_connect.setEnabled(not run_active and s != "LAMP" and self._expect_attach is None)
        self.btn_refresh.setEnabled(not connected and not run_active and self._expect_attach is None)
        self.btn_resume.setEnabled(self.link is None and self._expect_attach is None and s in ("DISCONNECTED", "IDLE", "PREVIEW"))
        self.btn_preview.setEnabled(s in ("IDLE", "PREVIEW")); self.btn_preview.setText("Stop live view" if s == "PREVIEW" else "Start live view")
        self.btn_apply.setEnabled(s in ("IDLE", "PREVIEW"))
        self.btn_dark.setEnabled(s in ("IDLE", "PREVIEW") and self.link is None)
        self.btn_start.setEnabled(s in ("IDLE", "PREVIEW") and self.link is None)
        self.btn_stop.setEnabled(self.link is not None and s in ("STANDBY", "RECORDING", "BURST", "AWAIT_UNCOVER"))
        if s != "PREVIEW" and self._repeat_save is not None:
            self._stop_repeat_save("Live preview stopped")
        self.btn_repeat_spec.setEnabled(s == "PREVIEW" and self.link is None)
        self.a_open_run.setEnabled(self.run_dir is not None)
        # GammaKeeper behaviour: the setup panel steps aside while a run is being watched
        if s in RUN_STATES and prev not in RUN_STATES and self.link is not None:
            self._set_setup_visible(False, remember=False)
        if detail:
            self.statusBar().showMessage(detail)

    def _refresh_devices(self):
        if self._spectrometer_live_open():
            self.statusBar().showMessage("Close CzernyScope before refreshing devices.", 10000)
            return
        if self.link is not None or live_sessions():
            self._log("device list not refreshed: a background recorder holds a spectrometer"); return
        devs, err = list_devices(True)
        self.dev_combo.clear()
        for i, lab in devs:
            self.dev_combo.addItem(lab, i)
        if err:
            self._log(f"device search: {err}")

    def _connect(self):
        if self._spectrometer_live_open():
            self.statusBar().showMessage("Close CzernyScope before connecting here.", 10000)
            return
        if self.state == "DISCONNECTED":
            self._pending_connect = time.time()
            self.engine.send("connect", self.dev_combo.currentData(), self.cb_edc.isChecked(), self.cb_nlc.isChecked())
        else:
            self._auto = False             # the user chose to let go of the spectrometer: do not grab it again
            self.engine.send("disconnect")

    def _toggle_preview(self):
        on = self.state != "PREVIEW"
        self.engine.send("preview", on, self.exp_mode.currentIndex() == 0, self.int_spin.value() if self.exp_mode.currentIndex() == 1 else None)

    def _save_spectrum(self):
        if self.latest is None or self.wl_dev is None:
            self.statusBar().showMessage("Nothing to save: start the live view first.", 8000); return
        snapshot = self._spectrum_snapshot()
        folder = self._spectrum_folder()
        path = folder / f"{self._spectrum_stem(snapshot['time'])}.dpt"
        if not self.auto_confirm:
            chosen, _ = Q.QFileDialog.getSaveFileName(self, "Save spectrum as", str(path),
                                                      "Data point table (*.dpt);;All files (*)",
                                                      options=Q.QFileDialog.Option.DontConfirmOverwrite)
            if not chosen:
                return
            path = Path(chosen) if Path(chosen).suffix else Path(chosen).with_suffix(".dpt")
        if self._write_spectrum(path, snapshot):
            self._remember_location("spectra_dir", str(path.parent))

    def _spectrum_folder(self):
        return Path(self._stored_location("spectra_dir") or
                    QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.DocumentsLocation))

    def _spectrum_time(self):
        return local_dt(int(self.latest_t)) if self.latest_t else dt.datetime.now(LOCAL_TZ)

    def _spectrum_stem(self, t):
        # Drop the leading date of the run name when it repeats the scan date.
        name = Path(self.run_dir).name if self.run_dir else self.label_edit.text().strip()
        name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_")
        if name.startswith(f"{t:%Y-%m-%d}_"):
            name = name[11:]
        return f"{t:%Y-%m-%d_%H-%M-%S}_{name}" if name else f"{t:%Y-%m-%d_%H-%M-%S}"

    def _toggle_repeat_save(self):
        if self._repeat_save is not None:
            self._stop_repeat_save("Stopped")
            return
        if self.state != "PREVIEW" or self.latest is None:
            self.statusBar().showMessage("Start live view before saving a series.", 8000)
            return
        dialog = Q.QDialog(self)
        dialog.setWindowTitle("Repeat save")
        layout = Q.QFormLayout(dialog)
        folder = Q.QLineEdit(str(self._spectrum_folder()))
        browse = Q.QPushButton("Browse…")

        def choose_folder():
            chosen = Q.QFileDialog.getExistingDirectory(dialog, "Spectrum series folder", folder.text())
            if chosen:
                folder.setText(chosen)

        browse.clicked.connect(choose_folder)
        layout.addRow("Save in", hbox(folder, browse))
        count = IntSpin(); count.setRange(2, 10000); count.setValue(10)
        interval = dspin(0.1, 3600, 1, 1, 0.1, " s")
        interval.setToolTip("Minimum time between saves. Each file uses a fresh scan; a slow exposure can extend the interval.")
        layout.addRow("Scans", count)
        layout.addRow("Interval", interval)
        buttons = Q.QDialogButtonBox(Q.QDialogButtonBox.StandardButton.Ok | Q.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(Q.QDialogButtonBox.StandardButton.Ok).setText("Start")
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() and folder.text().strip() and self.state == "PREVIEW" and self.latest is not None:
            self._start_repeat_save(Path(folder.text().strip()), count.value(), interval.value())

    def _start_repeat_save(self, folder, count, seconds):
        self._repeat_save = dict(folder=folder, count=count, interval_ms=round(seconds * 1000),
                                 saved=0, last=None, id=time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 10**9:09d}")
        self.btn_repeat_spec.setText("Stop repeat")
        self._save_next_spectrum()

    def _stop_repeat_save(self, reason):
        series, self._repeat_save = self._repeat_save, None
        self._repeat_timer.stop()
        self.btn_repeat_spec.setText("Repeat save…")
        self.statusBar().showMessage(f"{reason}. Saved {series['saved']} of {series['count']} spectra.", 10000)

    def _save_next_spectrum(self):
        series = self._repeat_save
        if series is None:
            return
        if self.state != "PREVIEW" or self.latest is None:
            self._stop_repeat_save("Live preview stopped")
            return
        scan = self.latest_t
        if scan is None or scan == series["last"]:
            self._repeat_timer.start(75)
            return
        snapshot = self._spectrum_snapshot()
        stem = self._spectrum_stem(snapshot["time"])
        path = series["folder"] / f"{stem}_series_{series['id']}_{series['saved'] + 1:04d}.dpt"
        if not self._write_spectrum(path, snapshot, series):
            if self._repeat_save is not None:
                self._stop_repeat_save("Repeat save failed")
            return
        if not series["saved"]:
            self._remember_location("spectra_dir", str(series["folder"]))
        series["last"] = scan
        series["saved"] += 1
        if series["saved"] >= series["count"]:
            self._stop_repeat_save("Repeat save complete")
        else:
            self._repeat_timer.start(series["interval_ms"])

    def _spectrum_snapshot(self):
        """Capture the scan and settings before any file dialog can process newer scans."""
        us = int(self.latest_int or 0); raw = np.asarray(self.latest, float); y = raw.copy()
        d = self.darks.get(us); sub = self.cb_dark.isChecked() and d is not None
        if sub:
            y = y - d
        bridged = []
        if self.cb_mask.isChecked() and self.valid is not None and not self.valid.all():
            y = hotpix.fill(y, self.valid); bridged = [int(i) for i in np.flatnonzero(~self.valid)]
        rate = self.cb_rate.isChecked() and us > 0
        if rate:
            y = y * 1e6 / us
        return dict(time=self._spectrum_time(), integration=us, raw=raw, dark=d if sub else None,
                    xy=np.column_stack((self._x(), y)), rate=rate, bridged=bridged, info=dict(self.info),
                    axis=self.axis_label, plan=self._plan(), label=self.label_edit.text().strip())

    def _write_spectrum(self, path, snapshot, series=None):
        """Save one full detector readout and the selected processing metadata."""
        t, us, plan = snapshot["time"], snapshot["integration"], snapshot["plan"]
        d, rate, bridged = snapshot["dark"], snapshot["rate"], snapshot["bridged"]
        sub = d is not None
        info, axis = snapshot["info"], snapshot["axis"]
        folder = path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            original = path
            suffix = 2
            while path.exists() or path.with_suffix(".json").exists():
                path = folder / f"{original.stem}_{suffix}{original.suffix}"
                suffix += 1
            meta = dict(app=f"{APP_NAME} {VERSION}", file=path.name, columns=["wavelength_nm", "counts_per_s" if rate else "counts"],
                        scan_time_local=t.isoformat(), integration_ms=us / 1000, scans_averaged=1,
                        device={key: info.get(key) for key in ("model", "serial", "kind", "electric_dark_correction",
                                                               "nonlinearity_correction", "pixels")},
                        wavelength_axis=axis, dark_subtracted=sub, counts_per_second=bool(rate),
                        hot_cold_pixels_bridged=bridged, label=snapshot["label"], notes=plan.notes,
                        geometry=plan.geometry, setup={k_: getattr(plan, k_) for k_ in SETUP_FIELDS},
                        site=dict(lat=plan.site_lat, lon=plan.site_lon),
                        raw_counts=[round(float(v), 2) for v in snapshot["raw"]],
                        dark_counts=[round(float(v), 2) for v in d] if sub else None)
            if series:
                meta["series"] = dict(id=series["id"], index=series["saved"] + 1, planned_scans=series["count"],
                                      minimum_interval_s=series["interval_ms"] / 1000)
            hdr = [f"{APP_NAME} {VERSION} spectrum", f"scan {t:%Y-%m-%d %H:%M:%S %Z}, {us / 1000:g} ms, 1 scan",
                   f"device {info.get('model', '')} {info.get('serial', '')}, electric dark "
                   f"{info.get('electric_dark_correction')}, nonlinearity {info.get('nonlinearity_correction')}",
                   f"{'dark-subtracted' if sub else 'raw (no dark subtracted)'}; {len(bridged)} hot/cold pixels bridged; "
                   f"wavelength axis: {axis}",
                   f"label: {meta['label']}; geometry: {plan.geometry}; notes: {plan.notes}",
                   f"details: {path.with_suffix('.json').name}",
                   f"columns: wavelength_nm, {'counts_per_s' if rate else 'counts'}"]
            sidecar = path.with_suffix(".json")
            created = []
            try:
                with path.open("x", encoding="utf-8") as spectrum:
                    created.append(path)
                    with sidecar.open("x", encoding="utf-8") as details:
                        created.append(sidecar)
                        np.savetxt(spectrum, snapshot["xy"], delimiter=",", fmt=["%.4f", "%.4f"], header="\n".join(hdr), comments="# ")
                        json.dump(meta, details, indent=1, default=str)
            except Exception:
                for incomplete in created:
                    incomplete.unlink(missing_ok=True)
                raise
        except Exception as e:
            Q.QMessageBox.warning(self, APP_NAME, f"Spectrum not saved: {e!r}")
            return False
        self._log(f"spectrum saved: {path} ({us / 1000:g} ms, {'dark-subtracted' if sub else 'raw'}"
                  f"{', ' + str(len(bridged)) + ' hot/cold pixels bridged' if bridged else ''})")
        self.statusBar().showMessage(f"Saved {path.name} in {folder}", 10000)
        return True

    def _site_changed(self):
        """A new site changes every sun time: say so, then redraw the schedule (it used to wait for the 30 s refresh)."""
        if not hasattr(self, "sch_notice") or self._applying_setup:
            return
        self.sch_notice.setVisible(True); self.sch_text.setHtml(f"<p style='color:{ORANGE}'>Updating the schedule for the new site…</p>")
        QtCore.QTimer.singleShot(40, lambda: (self._update_schedule(), self.sch_notice.setVisible(False)))

    def _last_runs(self, label):
        """(newest run's metadata, newest run's metadata with the same label) in the Save in folder, or None."""
        metas = self._run_metas()
        same = [m for m in metas if m.get("plan", {}).get("label") == label]
        return (metas[-1] if metas else None), (same[-1] if same else None)

    def _run_metas(self) -> list:
        """Metadata of the runs in the Save in folder, oldest first ('_folder' and '_dir' added)."""
        metas = []
        for root in (self._output_dir(), self._stored_location("setup_fallback_dir")):
            try:
                for d in Path(root).iterdir() if root else ():
                    m = read_json(d / "run_metadata.json") if d.is_dir() else None
                    if m and m.get("planned_start_local"):
                        m["_folder"] = d.name; m["_dir"] = str(d)
                        metas.append(m)
            except Exception:
                pass
            if metas:
                break
        metas.sort(key=lambda m: m["planned_start_local"])
        return metas

    def _preflight(self, plan, e) -> list:
        """[(level, text)], level ok / warn / bad: what to check before an unattended run."""
        out = []
        dl = e["dark_ladder_ms"]
        if plan.auto_exposure:
            out.append(("warn", f"Automatic exposure over {', '.join(f'{x:g}' for x in e['ladder_ms'])} ms: each change can step "
                                "the band depths (handoff B2/B3). Fixed exposure avoids it."))
        else:
            out.append(("ok", f"Fixed exposure at {plan.int_ms:g} ms" + _backoff_txt(plan)))
            if len(e["ladder_ms"]) > BACKOFF_MANY:   # 0.3.11: each step needs a dark, and it steps once per slot
                out.append(("warn", f"Back off makes {len(e['ladder_ms'])} integration times: each dark set takes about "
                                    f"{e['dark_session_s']:.0f} s, and reaching {plan.backoff_min_ms:g} ms takes "
                                    f"{(len(e['ladder_ms']) - 1) * plan.slot_s:.0f} s. Use a larger step or a shorter exposure."))
        if e["bursts"]:
            lvl = "warn" if e["burst_int_ms"] > (e["ladder_ms"][0] if plan.auto_exposure else plan.int_ms) else "ok"
            out.append((lvl, f"{len(e['bursts'])} bursts at {e['burst_int_ms']:g} ms, with their own {e['burst_int_ms']:g} ms dark"
                             + (" (longer than the shortest slot time)" if lvl == "warn" else "")))
        if plan.save_all_frames:
            out.append(("ok", f"Every frame saved: about {e['data_gb_expected']:.0f} GB for this run"))
        if not (plan.pre_darks or plan.post_darks):
            out.append(("bad", "No dark sets planned"))
        elif len(dl) < 2:
            out.append(("warn", f"Darks at one integration time only ({dl[0]:g} ms): the dark-drift model needs two (set Long dark)"))
        else:
            out.append(("ok", f"Darks at {', '.join(f'{x:g}' for x in dl)} ms"
                              + ("" if plan.pre_darks and plan.post_darks else f" ({'pre' if plan.pre_darks else 'post'}-run only)")))
        sp = site_problem(plan.site_lat, plan.site_lon)
        out.append(("bad" if sp and (plan.start_event or plan.stop_event) else "warn", sp) if sp else
                   ("ok", f"Site {site_text(plan.site_lat, plan.site_lon)}, time zone {LOCAL_TZ}"))
        empty = [lab for k, lab in (("fiber_core_um", "fiber core"), ("diffuser_id", "diffuser"), ("diffuser_thickness_mm", "diffuser thickness"),
                                    ("fiber_diffuser_mm", "fiber-diffuser distance"), ("view_az_deg", "view azimuth"),
                                    ("view_el_deg", "view elevation"), ("site_alt_m", "site altitude"), ("window_state", "window"))
                 if getattr(plan, k, None) in (None, "")]
        if not plan.geometry:
            empty.append("geometry")
        if not plan.setup_photo:
            empty.append("setup photo")
        out.append(("warn", "Not filled: " + ", ".join(empty)) if empty else ("ok", "Setup fields filled"))
        try:
            ck = clock.sntp_query(timeout=1.5)
        except Exception as ex:
            ck = dict(ok=False, error=repr(ex))
        if ck.get("ok"):
            off = ck["offset_s"]
            out.append(("warn" if abs(off) > clock.WARN_OFFSET_S else "ok", f"Clock offset {off:+.2f} s"))
        else:
            out.append(("warn", "Clock not checked (no network?)"))
        need, free = e.get("data_bytes_expected", 0) / 1e9, e["disk_free_gb"]
        if free == free:                     # not NaN
            out.append(("bad" if need > free else "warn" if need > 0.5 * free else "ok",
                        f"Disk: about {need:.2f} GB needed, {free:.0f} GB free"))
        else:
            out.append(("bad", f"Disk: the drive for {plan.out_dir} was not found. Is it connected?"))
        last, same = self._last_runs(plan.label)
        if last:
            lp = last.get("plan", {})
            if (bool(lp.get("edc", True)), bool(lp.get("nlc", True))) != (plan.edc, plan.nlc):
                out.append(("warn", f"Electric dark / nonlinearity {plan.edc}/{plan.nlc} differ from the last run "
                                    f"({lp.get('edc')}/{lp.get('nlc')}, {Path(str(lp.get('out_dir', ''))).name or 'previous run'})"))
        if same and (same.get("plan", {}).get("diffuser_id") or "") != (plan.diffuser_id or ""):
            out.append(("warn", f"Diffuser '{plan.diffuser_id or '(empty)'}' differs from the last '{plan.label}' run "
                                f"('{same['plan'].get('diffuser_id') or '(empty)'}')"))
        ts = str(self.info.get("temperature_source", ""))
        if ts and not ts.startswith("none"):
            out.append(("ok", f"Temperature logged every slot ({ts})"))
        else:
            out.append(("warn", "No readable temperature on this spectrometer: not logged (the Maya2000 Pro has no sensor the SDK can read)"))
        return out

    def _show_site(self):
        lat, lon = self.lat.value(), self.lon.value(); bad = site_problem(lat, lon)
        self.site_lbl.setText(site_text(lat, lon) + (f"\n{bad}" if bad else ""))
        self.site_lbl.setStyleSheet(f"color: {ORANGE}" if bad else "")

    def _live_dark(self):
        hot_ms = max(self.max_spin.value(), self.int_spin.value())
        live_ms = (self.latest_int or 0) / 1000 or None
        txt = (f"Cover the input completely, then press Record.\n\n5 scans at {hot_ms:g} ms"
               + (f" and 5 at {live_ms:g} ms (the live view's current time)" if live_ms and abs(live_ms - hot_ms) > 1e-6 else "")
               + " find this spectrometer's hot and cold pixels and give the live view a matched dark. Nothing is saved to a run.")
        if self._ask("Dark set", txt, "Record", "Cancel"):
            self.engine.send("live_dark", int(hot_ms * 1000), int(live_ms * 1000) if live_ms else None, 5)

    def _apply_preview(self):
        self.engine.send("preview", self.state == "PREVIEW", self.exp_mode.currentIndex() == 0, self.int_spin.value())

    def _plan(self) -> RunPlan:
        for k, v in (("site_lat", self.lat.value()), ("site_lon", self.lon.value())):
            self.settings.setValue(k, v)
        out_dir = self._output_dir()
        location = self._out_location if self.out_edit.text() == self._out_text else remember_path(out_dir)
        try:
            ladder = [float(x) for x in self.ladder_edit.text().replace(";", ",").split(",") if x.strip()]
        except ValueError:
            ladder = list(DEFAULT_LADDER_MS)
        return RunPlan(label=self.label_edit.text().strip() or "run", notes=self.notes_edit.text(), geometry=self.geom_edit.text(),
                       out_dir=out_dir, out_dir_location=location, start_now=self.start_mode.currentIndex() == 0,
                       start_event=self.start_event.currentText() if self.start_mode.currentIndex() == 2 else "",
                       start_offset_min=self.start_off.value(),
                       stop_event=self.stop_event.currentText() if self.stop_mode.currentIndex() == 1 else "",
                       stop_offset_min=self.stop_off.value(),
                       start_local=self.start_edit.dateTime().toPython().replace(tzinfo=LOCAL_TZ).isoformat(),
                       stop_local=self.stop_edit.dateTime().toPython().replace(tzinfo=LOCAL_TZ).isoformat(),
                       slot_s=self.slot_spin.value(), auto_exposure=self.run_exp.currentIndex() == 1, int_ms=self.run_int.value(),
                       long_dark_ms=self.long_dark.value(), backoff_step_ms=self.backoff_step.value(), backoff_min_ms=self.backoff_min.value(),
                       min_int_ms=self.min_spin.value(), max_int_ms=self.max_spin.value(), ladder_ms=ladder,
                       edc=self.cb_edc.isChecked(), nlc=self.cb_nlc.isChecked(), store_sd=self.cb_sd.isChecked(),
                       burst_every_min=self.burst_every.value(), burst_offset_min=self.burst_off.value(), burst_s=self.burst_s.value(),
                       burst_max_int_ms=self.burst_int.value(), burst_min_counts=self.burst_min.value(), save_all_frames=self.cb_all.isChecked(),
                       dark_s_per_int=self.dark_s.value(), pre_darks=self.cb_pre.isChecked(),
                       post_darks=self.cb_post.isChecked(), site_lat=self.lat.value(), site_lon=self.lon.value(),
                       auto_resume_logon=self.cb_logon.isChecked(), diffuser_id=self.diff_edit.text().strip(),
                       window_state=self.window_cb.currentText(), setup_photo=self.photo_edit.text().strip(),
                       **{k: (None if s.value() <= s.minimum() else s.value()) for k, s in self.setup_spins.items()})

    def _ask(self, title, text, ok="OK", cancel="Cancel") -> bool:
        if self.auto_confirm:
            self._log(f"(auto-confirm) {title}: {text.splitlines()[0]}"); return True
        box = Q.QMessageBox(self); box.setWindowTitle(title); box.setText(text); box.setIcon(Q.QMessageBox.Icon.Information)
        bo = box.addButton(ok, Q.QMessageBox.ButtonRole.AcceptRole)
        if cancel:
            box.addButton(cancel, Q.QMessageBox.ButtonRole.RejectRole)
        self._dialog = True
        try:
            box.exec()
        finally:
            self._dialog = False
        return box.clickedButton() is bo

    def _start_run(self):
        self._commit_output_location()
        plan = self._plan()
        if not plan.out_dir:
            Q.QMessageBox.warning(self, APP_NAME, "Reconnect the saved data drive or choose a folder in Save in.")
            return
        bad = site_problem(plan.site_lat, plan.site_lon) if (plan.start_event or plan.stop_event) else ""
        if bad and (plan.site_lat, plan.site_lon) == (0.0, 0.0):
            Q.QMessageBox.warning(self, APP_NAME, bad + "\n\nSet the latitude and longitude under Show advanced settings → Site.")
            return
        if bad and not self._ask("Check the site", bad + "\n\nStart with these sun times anyway?", "Start anyway", "Cancel"):
            return
        e = estimates(plan, len(self.wl_dev) if self.wl_dev is not None else 2068, int(self.info.get("min_int_us_used", 8000)), int(self.info.get("max_int_us", 6e7)))
        if e["stop"] <= max(e["start"], dt.datetime.now(LOCAL_TZ)):
            Q.QMessageBox.warning(self, APP_NAME, "The stop time must be in the future and after the start time."); return
        self.tabs.setCurrentIndex(3)
        txt = (f"Label: {plan.label}\nStart: {'now (after pre-run darks)' if plan.start_now else e['start'].strftime('%Y-%m-%d %H:%M:%S')}"
               f"{'  (' + plan.start_event + f' {plan.start_offset_min:+g} min)' if plan.start_event and not plan.start_now else ''}\n"
               f"{'Stop event: ' + plan.stop_event + f' {plan.stop_offset_min:+g} min' + chr(10) if plan.stop_event else ''}"
               f"Stop: {e['stop']:%Y-%m-%d %H:%M}  ({e['duration_h']:.2f} h, {e['slots']:,} slots of {plan.slot_s:g} s)\n"
               + ("Every frame saved\n" if plan.save_all_frames else f"Bursts: {len(e['bursts'])} × {plan.burst_s:g} s at {e['burst_int_ms']:g} ms\n") +
               f"Exposure: {'automatic, ' + ', '.join(f'{x:g}' for x in e['ladder_ms']) + ' ms' if plan.auto_exposure else 'fixed at ' + format(plan.int_ms, 'g') + ' ms' + _backoff_txt(plan)}\n"
               f"Dark sets: {'pre ' if plan.pre_darks else ''}{'post' if plan.post_darks else ''} · ~{e['dark_session_s']:.0f} s each at {', '.join(f'{x:g}' for x in e['dark_ladder_ms'])} ms\n"
               f"Estimated data: about {e['data_gb_expected']:.2f} GB (free {e['disk_free_gb']:.0f} GB)")
        checks = self._preflight(plan, e)
        sym = {"ok": "✔", "warn": "⚠", "bad": "✖"}
        txt += "\n\nPre-flight checks\n" + "\n".join(f"{sym[lv]}  {t}" for lv, t in checks)
        worst = "bad" if any(lv == "bad" for lv, _ in checks) else "warn" if any(lv == "warn" for lv, _ in checks) else "ok"
        self._log("pre-flight: " + "; ".join(f"{lv} {t}" for lv, t in checks))
        txt += "\n\n" + ("Start anyway?" if worst == "bad" else "Start this run?")
        if not self._ask("Start run" if worst != "bad" else "Start run: problems found", txt,
                         "Start anyway" if worst == "bad" else "Start", "Cancel"):
            return
        if plan.pre_darks and not self._ask("Pre-run darks",
                                            "Cover the input completely (foil + cap) and keep it covered until LightKeeper asks you to uncover it.\n\n"
                                            f"The dark set takes about {e['dark_session_s']:.0f} s.", "Record dark set", "Cancel"):
            return
        serial = str(self.info.get("serial") or "")
        if not serial:
            Q.QMessageBox.warning(self, APP_NAME, "The spectrometer's serial number is unknown; connect it first."); return
        dev_id = self.dev_combo.currentData()
        self.slots.clear(); self.spectra.clear(); self.spec_stride = 1; self._slot_count = 0; self.n_bursts = 0
        if not plan.start_now:          # freeze what was confirmed: the recorder must not re-resolve a sun event later
            plan.resolved_start_local = e["start"].isoformat()
        plan.resolved_stop_local = e["stop"].isoformat()
        self.plan = plan
        self._release_local_device()
        pf = app_home() / "pending" / f"plan_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        write_json_atomic(pf, plan.to_dict())
        args = ["--supervisor", "--plan", str(pf), "--serial", serial]
        if isinstance(dev_id, int) and dev_id >= 0:
            args += ["--device-id", str(dev_id)]
        self._launch_background(args, serial)

    def _release_local_device(self):
        """The background recorder must be able to open the spectrometer: close it here and release the SDK."""
        if self.engine.state != "DISCONNECTED":
            self.engine.send("disconnect")
            t0 = time.time()
            while self.engine.state != "DISCONNECTED" and time.time() - t0 < 20:
                Q.QApplication.processEvents(); time.sleep(0.05)
        shutdown_sdk()

    def _launch_background(self, args, serial):
        log = app_home() / "logs" / f"launch_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        try:
            spawn_detached(entry_args(*args), log)
        except Exception as e:
            Q.QMessageBox.critical(self, APP_NAME, f"Could not start the background recorder:\n{e!r}"); return
        self._expect_attach = (serial, time.time() + 45)
        self._log(f"background recorder starting for {serial}: {' '.join(args)}")
        self.dev_combo.clear(); self.dev_combo.addItem(f"{serial} - handed to the background recorder", None)
        self._set_state("DISCONNECTED", "starting the background recorder…")

    def _cmd(self, name, *args):
        """Run commands go to the background recorder when attached, else to the window's own engine."""
        if self.link is not None:
            try:
                self.link.send(name, *args)
            except Exception as e:
                self._log(f"ERROR sending '{name}' to the recorder: {e!r}")
        else:
            self.engine.send(name, *args)

    def _stop_run(self):
        if self._ask("Stop run", "Stop the run now? Recording ends after the current slot; you will then be asked for the post-run dark set.", "Stop run", "Keep running"):
            self._cmd("stop")

    def _ask_uncover(self):
        if not self._started:
            self._when_started(self._ask_uncover)
            return
        if self.state != "AWAIT_UNCOVER" or self._dialog:
            return
        self._ask("Uncover the input", "Pre-run dark set recorded.\n\nRemove the cover WITHOUT moving the fiber/diffuser, then press Continue.\n"
                  "Recording begins at the scheduled start time.", "Continue", None)
        if self.state == "AWAIT_UNCOVER":
            self._cmd("continue")

    def _ask_post_dark(self):
        if not self._started:
            self._when_started(self._ask_post_dark)
            return
        if self.state != "POST_RUN" or self._dialog:
            return
        if self._ask("Post-run dark set", "Recording has finished.\n\nCover the input completely, then press Record to take the post-run dark set "
                     "over all integration times (the input must stay covered until it finishes).", "Record dark set", "Skip the post-run darks"):
            self._cmd("post_dark")
        elif self.state == "POST_RUN":
            self._cmd("finish")

    # =================================================================== events
    def _poll(self):
        for src in (self.engine, self.link):
            if src is None:
                continue
            for _ in range(300):
                try:
                    e = src.evt.get_nowait()
                except Exception:
                    break
                self._handle(e)
                if src is not self.engine and self.link is not src:   # detached while handling
                    break
        if self._scan_new and time.time() - self._last_live > 0.2 and not self._scrolling_setup():
            self._draw_live()

    def _handle(self, e):
        if True:
            t = e["type"]
            if t == "snapshot":
                self._apply_snapshot(e); return
            if t == "detached":
                self._on_detached(); return
            if t == "scan":
                self.latest, self.latest_int, self.latest_t = e["y"], e["int_us"], e.get("t_ns"); self._scan_new = True
            elif t == "slot":
                self._on_slot(e)
            elif t == "state":
                self._set_state(e["state"], e.get("detail", ""))
                if e["state"] == "IDLE" or (e["state"] == "DISCONNECTED" and self._switching != "closing"):
                    self._pending_connect = 0.0           # that connect has ended, one way or the other
                if self._switching == "closing" and e["state"] == "DISCONNECTED":
                    self._switching = "opening"       # the old spectrometer closed; the new one opens next
                elif self._switching == "opening" and e["state"] == "DISCONNECTED":
                    self._switching = None; self._auto_live = False    # it did not open: Connect, or pick another
                elif self._auto_live and e["state"] == "IDLE":
                    self._switching = None
                    self._auto_live = False; self._auto_failed = None
                    self.engine.send("preview", True, False, self.int_spin.value())
                elif self._auto_live and e["state"] == "DISCONNECTED":
                    self._auto_failed = self._auto_live; self._auto_live = False
                if e["state"] == "AWAIT_UNCOVER":
                    QtCore.QTimer.singleShot(50, self._ask_uncover)
                if e["state"] not in ("DARK", "LAMP"):
                    self.progress.setVisible(False)
            elif t == "need_post_dark":
                QtCore.QTimer.singleShot(50, self._ask_post_dark)
            elif t == "device":
                if self.info.get("serial") != e["info"].get("serial") or not np.array_equal(self.wl_dev, e["wavelengths"]):
                    self.latest = self.latest_int = self.latest_t = None
                    self.darks = {}
                    self._scan_new = False
                    self._edge_layout = None
                self.info = e["info"]; self.wl_dev = np.asarray(e["wavelengths"]); self.axis = self.wl_dev.copy(); self.valid = np.ones(len(self.axis), bool)
                self.hot = None   # the engine follows with this spectrometer's saved list, if it has one
                i = self.info
                onoff = lambda v: "on" if v else ("off" if v is False else "not supported")
                self.dev_info.setText(f"{i.get('model', '')} {i.get('serial', '')}, {i.get('pixels')} pixels, "
                                      f"{i.get('wl_first', self.wl_dev[0]):.1f}–{i.get('wl_last', self.wl_dev[-1]):.1f} nm. "
                                      f"Shortest integration {i.get('min_int_us_used', 0) / 1000:g} ms (SDK reports {i.get('sdk_min_int_us', 0) / 1000:g} ms). "
                                      f"Electric dark {onoff(i.get('electric_dark_correction'))}, nonlinearity {onoff(i.get('nonlinearity_correction'))}.")
                self.l_sat.set_label(f"Saturation, {self._sat():,.0f}")
                if i.get("serial") and not str(i.get("serial")).upper().startswith("SIM"):
                    self.settings.setValue("last_serial", str(i.get("serial")))
                if self.c_spec.ax[0].get_legend() is not None:
                    self.c_spec.ax[0].legend(loc="upper right")
                self._log(f"connected: {json.dumps(i, default=str)}"); self._update_schedule()
            elif t == "log":
                self._log(e["text"])
                if "burst_start" in e["text"]:
                    self.n_bursts += 1; self.m["bursts"].setText(str(self.n_bursts))
            elif t == "clock":
                txt = (f"Clock offset {e['offset_s']:+.2f} s" if e["ok"] else "Clock check failed (no network?)") + \
                      ("" if e["w32time"] else "; Windows time service not running")
                self.clock_lbl.setText(txt); self.clock_lbl.setStyleSheet(f"color: {ORANGE if e['warn'] else TEXT2}")
                self.clock_lbl.setToolTip("SNTP check against pool.ntp.org at Start, hourly and at Stop")
            elif t == "error":
                self._log("ERROR " + e["text"]); self.statusBar().showMessage(e["text"], 15000)
            elif t == "progress":
                self.progress.setVisible(True); self.progress.setValue(int(100 * e["frac"])); self.progress.setFormat(e["what"])
            elif t == "darks":
                self.darks = {int(k): np.asarray(v) for k, v in e["darks"].items()}; self._update_mask()
            elif t == "hot_pixels":
                self.hot = {k: v for k, v in e.items() if k != "type"}; self._update_mask(); self._draw_live(force=True)
                self.m["darks"].setText(str(int(self.m["darks"].text()) + 1 if self.m["darks"].text().isdigit() else 1))
            elif t == "run":
                if self.viewing is not None:
                    self._close_recorded_run()
                self.run_dir = e["dir"]; self.run_lbl.setText(Path(e["dir"]).name); self.run_lbl.setToolTip(e["dir"]); self.m["darks"].setText("0")
                self.a_open_run.setEnabled(True)
                self.run_times = (dt.datetime.fromisoformat(e["t_start"]), dt.datetime.fromisoformat(e["t_stop"]))
                self.run_plan = dict(e["plan"])  # snapshot of the plan the engine is executing; form edits no longer apply
                self._update_schedule()
            elif t == "run_closed":
                self._log(f"run closed: {e['dir']} rows {e['rows']}"); self._closed_run = True
                self._set_setup_visible(self.settings.value("setup_visible", True, type=bool), remember=False)
                self.run_plan = None; self.run_times = None; self._update_schedule()
                self._apply_previous_setup()        # the next run follows from the one that just closed
                if self.cb_weather.isChecked():
                    self._fetch_weather([e["dir"]])
            elif t == "lamp_result":
                self.cal[e["kind"]] = e; self._log(f"calibration {e['kind']}: {e['n']} scans at {e['int_us'] / 1000:g} ms")
                if e["kind"] == "lamp":
                    self.cal_result = None; self._cal_y = None; self._cal_saved = False
                self._draw_cal(); self._cal_steps()
            elif t == "dark_set_done":
                self.cal_darks.update({int(k): np.asarray(v) for k, v in e["darks"].items()}); self._cal_steps()
            elif t == "int":
                self.m["int"].setText(f"{e['int_us'] / 1000:g}")
            elif t == "run_interrupted":
                self._log(f"run interrupted (resumable): {e['dir']}"); self._closed_run = True
                self.run_plan = None; self.run_times = None; self._update_schedule()

    # =================================================================== background recorder: attach / detach / resume
    def _try_attach(self, serial=None) -> bool:
        if self.link is not None:
            return True
        for sess in live_sessions():
            if serial and sess.get("serial") != serial:
                continue
            try:
                link = RecorderLink(sess)
            except Exception as e:
                self._log(f"could not attach to recorder pid {sess.get('pid')}: {e!r}"); continue
            self.link = link; self._expect_attach = None; self._closed_run = False
            self.link_lbl.setText(f"Background recorder for {sess.get('serial')}, pid {sess.get('pid')}")
            self.dev_combo.clear(); self.dev_combo.addItem(f"{sess.get('serial')} - held by the background recorder", None)
            self._log(f"attached to background recorder pid {sess.get('pid')} ({sess.get('serial')}); closing this window will not stop it")
            return True
        return False

    def _apply_snapshot(self, snap):
        self.viewing = None; self.a_close_rec.setEnabled(False)
        self.slots.clear(); self.spectra.clear(); self.spec_stride = 1; self._slot_count = 0
        if snap.get("device"):
            self._handle(dict(type="device", **snap["device"]))
        if snap.get("run"):
            self._handle(dict(type="run", **snap["run"]))
        if snap.get("darks"):
            self._handle(dict(type="darks", darks=snap["darks"]))
        if snap.get("hot"):
            self._handle(dict(type="hot_pixels", **snap["hot"]))
        for ev in snap.get("history", []):
            self._on_slot(ev)
        if snap.get("scan"):
            self.latest, self.latest_int, self.latest_t = snap["scan"]["y"], snap["scan"]["int_us"], snap["scan"].get("t_ns"); self._scan_new = True
        c = snap.get("counters", {})
        self.n_bursts = int(c.get("bursts", 0)); self.m["bursts"].setText(str(self.n_bursts)); self.m["darks"].setText(str(c.get("dark_sets", 0)))
        if snap.get("int_us"):
            self.m["int"].setText(f"{snap['int_us'] / 1000:g}")
        if snap.get("logs"):
            self._log("---- recorder log (replayed on attach) ----")
            for line in snap["logs"]:
                self._log(line)
            self._log("---- live ----")
        self._set_state(snap.get("state", "DISCONNECTED"), snap.get("detail", ""))
        if snap.get("state") == "AWAIT_UNCOVER":
            QtCore.QTimer.singleShot(50, self._ask_uncover)
        elif snap.get("state") == "POST_RUN":
            QtCore.QTimer.singleShot(50, self._ask_post_dark)
        self._dirty_ts = True; self._draw_live(force=True) if self.latest is not None else None

    def _on_detached(self):
        if self.link is None:
            return
        self.link.close(); self.link = None; self.link_lbl.setText("")
        closed = self._closed_run
        self._set_state("DISCONNECTED", "run closed; the recorder released the spectrometer" if closed else
                        "lost contact with the background recorder - re-attaching automatically if it restarts")
        self._log("detached from the background recorder" + (" (run finished)" if closed else ""))
        if closed:
            self.dev_combo.clear(); self.dev_combo.addItem("(press Refresh: spectrometer released)", None)

    def _offer_resume(self):
        if self.link is not None or self._expect_attach is not None:
            return
        runs = [r for r in open_runs() if not supervisor_alive(r["run_dir"])]
        if not runs:
            self._auto_connect(); return
        r = runs[-1]; st = r["state"]
        txt = (f"An unfinished run was found:\n\n{Path(r['run_dir']).name}\nstatus: {st.get('status')} (last phase {st.get('phase')})\n"
               f"last recorded slot: {st.get('last_slot_local') or '-'}\nscheduled stop: {st.get('planned_stop_local')}\n\n"
               "Resume it now? It continues as a new segment until the scheduled stop, or goes straight to the post-run darks "
               "if that time has passed.")
        if self._ask("Resume run", txt, "Resume now", "Not now"):
            self._resume(r["run_dir"], st.get("serial", "")); return
        if needs_watching(r):                  # 'Not now' means it: the watchdog must not resume it behind your back
            watchdog_off(r["run_dir"], "'Not now' in the window")
        self._auto_connect()

    def _auto_connect(self, rescan=False):
        """Connect the first attached Ocean spectrometer (never the simulator) and start the live view."""
        if not self._started or not self._auto or self.link is not None or self._expect_attach is not None or self.state != "DISCONNECTED":
            return
        if time.time() - self._pending_connect < 60:      # a connect is still on its way
            return
        if rescan:
            if live_sessions() or watched_runs():   # 0.3.11: a run the watchdog is about to resume needs the spectrometer free
                return
            devs, _ = list_devices(True)
            if [i for i, _ in devs if i != SIM_ID] and [i for i, _ in devs] != [self.dev_combo.itemData(k) for k in range(self.dev_combo.count())]:
                self.dev_combo.clear()
                for i, lab in devs:
                    self.dev_combo.addItem(lab, i)
        ids = [self.dev_combo.itemData(k) for k in range(self.dev_combo.count())]
        # 0.4.1: only spectrometers that could be named (opened briefly while listing); an unnamed one is in use or faulty
        real = [i for i in ids if isinstance(i, str)]
        if not real or tuple(real) == self._auto_failed:
            return
        pick = self._last_used(real)           # 0.4.1: the spectrometer used last, if attached; else the first found
        self.dev_combo.setCurrentIndex(ids.index(pick)); self._auto_live = tuple(real)
        self._log(f"opening {self.dev_combo.currentText().strip()} automatically; live view at {LIVE_START_MS} ms")
        self._pending_connect = time.time()
        if len(real) > 1:
            self.statusBar().showMessage(f"{len(real)} spectrometers attached: opened {self.dev_combo.currentText().split('  (')[0].strip()}. "
                                         "Pick another in the Spectrometer list to switch.", 15000)
        self.engine.send("connect", pick, self.cb_edc.isChecked(), self.cb_nlc.isChecked())

    def _when_started(self, fn):
        """Run fn now if start-up is over, else when the splash has closed."""
        if self._started:
            fn()
        elif fn not in self._on_started:
            self._on_started.append(fn)

    def startup_finished(self):
        """Called once the splash has closed (splash.start): now the window may ask questions."""
        if self._started:
            return
        self._started = True
        for fn in self._on_started:
            fn()
        self._on_started.clear()

    def _last_used(self, real):
        """Of the attached spectrometer ids, the one whose serial was used last (settings 'last_serial'), else the first."""
        last = str(self.settings.value("last_serial", "") or "")
        for i in real:
            if last and f"[{last}]" in self.dev_combo.itemText(self.dev_combo.findData(i)):
                return i
        return real[0]

    def _device_picked(self, k):
        """0.4.1: choosing another spectrometer in the list while one is open switches to it (not during a run)."""
        dev_id = self.dev_combo.itemData(k)
        if dev_id is None or self.state == "DISCONNECTED" or self.state in RUN_STATES or self.link is not None:
            return                            # disconnected: Connect opens the one chosen
        cur = (self.info or {}).get("serial", "")
        if cur and f"[{cur}]" in self.dev_combo.itemText(k):
            return
        self._log(f"switching to {self.dev_combo.itemText(k).strip()}")
        self._auto_live = (dev_id,)           # start the live view again once it is open
        self._switching = "closing"; self._pending_connect = time.time()
        self.engine.send("connect", dev_id, self.cb_edc.isChecked(), self.cb_nlc.isChecked())

    def _resume_dialog(self):
        runs = [r for r in open_runs() if not supervisor_alive(r["run_dir"])]
        if runs:
            names = [f"{Path(r['run_dir']).name}  ({r['state'].get('status')}, stop {r['state'].get('planned_stop_local', '')[:16]})" for r in runs]
            item, ok = Q.QInputDialog.getItem(self, "Resume run", "Unfinished runs:", names, len(names) - 1, False)
            if not ok:
                return
            r = runs[names.index(item)]; self._resume(r["run_dir"], r["state"].get("serial", "")); return
        d = Q.QFileDialog.getExistingDirectory(self, "Run folder to resume (contains run_state.json)", self._output_dir())
        if not d:
            return
        st = read_json(Path(d) / "run_state.json")
        if not st or st.get("status") == "closed":
            Q.QMessageBox.information(self, APP_NAME, "That folder has no unfinished LightKeeper 0.2 run (run_state.json missing or closed)."); return
        self._resume(d, st.get("serial", ""))

    def _resume(self, run_dir, serial):
        if self._spectrometer_live_open():
            Q.QMessageBox.information(self, APP_NAME, "Close CzernyScope before resuming a recording.")
            return
        if self._try_attach(serial):
            return
        self._release_local_device()
        self.slots.clear(); self.spectra.clear(); self.spec_stride = 1; self._slot_count = 0
        self._launch_background(["--supervisor", "--resume", str(run_dir)], serial)

    def _log(self, text):
        self.log_box.appendPlainText(text)
        try:                                   # the window's log also goes to app_home/logs, one file per day
            with open(app_home() / "logs" / f"window_{dt.date.today():%Y%m%d}.log", "a", encoding="utf-8") as fh:
                fh.write(f"{dt.datetime.now():%H:%M:%S} {text}\n")
        except Exception:
            pass

    def _fetch_weather(self, dirs):
        """On its own thread: the network never holds up this window, and the recorder is a separate process."""
        def job():
            for d in dirs:
                try:
                    p = weather.fetch_for_run(d)
                    self._wx_log.append(f"weather saved: {p}")
                except Exception as e:
                    self._wx_log.append(f"weather not fetched for {Path(d).name}: {e}")
        threading.Thread(target=job, daemon=True, name="LightKeeperWeather").start()

    def _weather_sweep(self):
        root = self._output_dir()
        dirs = weather.missing_runs(root) if root else []
        if dirs:
            self._log(f"fetching weather for {', '.join(d.name for d in dirs)}")
            self._fetch_weather(dirs)

    def _weather_dialog(self):
        d = Q.QFileDialog.getExistingDirectory(self, "Fetch weather for this run folder", self._output_dir())
        if d:
            self._log(f"fetching weather for {Path(d).name} (sends the site, rounded to two decimals, and the dates)")
            self._fetch_weather([d])

    def _slow_tick(self):
        while self._wx_log:
            self._log(self._wx_log.pop(0))
        self.clock.setText(dt.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"))
        holds = self.link is None and self.engine.state != "DISCONNECTED"
        if holds != self._holds_device:
            self._holds_device = holds; set_window_holds(holds)
        if self.link is None and (self._expect_attach is not None or (self.state == "DISCONNECTED" and time.time() - self._last_attach_try > 3)):
            self._last_attach_try = time.time()
            if self._try_attach(self._expect_attach[0] if self._expect_attach else None):
                pass
            elif self._expect_attach is not None and time.time() > self._expect_attach[1]:
                self._expect_attach = None; self._set_state("DISCONNECTED")
                Q.QMessageBox.warning(self, APP_NAME, "The background recorder did not start. See the logs in\n"
                                      f"{app_home() / 'logs'}\n\nThen press Refresh and Connect.")
        if self.state == "DISCONNECTED" and self.link is None and self._expect_attach is None and time.time() - self._last_auto_try > 5:
            self._last_auto_try = time.time(); self._auto_connect(rescan=True)    # a spectrometer plugged in later
        nxt = self._next_event()
        self.next_lbl.setText(nxt)
        if self._scrolling_setup():
            return
        if self._dirty_ts and time.time() - self._last_ts_draw > 4:
            self._dirty_ts = False; self._last_ts_draw = time.time(); self._draw_timeseries(); self._draw_waterfall()
        if int(time.time()) % 30 == 0:
            self._update_schedule()

    def _scrolling_setup(self):
        return (self.setup.isVisible() and (self.setup_scroll.verticalScrollBar().isSliderDown()
                                           or time.monotonic() - self._setup_scrolled < 0.15))

    def _next_event(self) -> str:
        now = dt.datetime.now(LOCAL_TZ)
        rt = getattr(self, "run_times", None)
        if self.state == "STANDBY" and rt:
            m = max(0, (rt[0] - now).total_seconds()) / 60
            return f"Recording starts {rt[0]:%a %H:%M:%S} (in {m // 60:.0f} h {m % 60:.0f} min), stops {rt[1]:%H:%M:%S}"
        if self.state in ("RECORDING", "BURST") and rt:
            nb = getattr(self, "_next_burst", "")
            s = f"Stops {rt[1]:%H:%M:%S}, {(rt[1] - now).total_seconds() / 3600:.2f} h left"
            if nb:
                s = f"Next burst {dt.datetime.fromisoformat(nb):%H:%M}. " + s
            return s
        return ""

    # =================================================================== data handling
    def _rate(self, mean, int_us):
        d = self.darks.get(int(int_us))
        if d is None:
            m = (self.wl_dev > 205) & (self.wl_dev < 290)
            base = np.median(mean[m]) if m.any() else 0.0
            return (mean - base) / (int_us / 1e6)
        return (mean - d) / (int_us / 1e6)

    def _on_slot(self, e):
        r = self._rate(np.asarray(e["mean"], float), e["int_us"])
        metrics = runview.band_metrics(r[None, :], runview.band_masks(self.axis, self.valid))
        bg = frshare = np.nan
        if not e["nsat"] and int(e["int_us"]) in self.darks:
            weights = runview.broad_band_weights(self.axis, self.valid)
            blue_green, far_red_share = runview.broad_metrics(r[None, :], weights, np.ones(1, bool))
            bg, frshare = float(blue_green[0]), float(far_red_share[0])
        self.slots.append(dict(t=local_dt(e["t_ns"]), int_ms=e["int_us"] / 1000, n=e["n"], peak=e["bmax"], nsat=e["nsat"],
                               **{name: float(values[0]) for name, values in metrics.items()}, bg=bg, frshare=frshare))
        self._slot_count += 1
        if self._slot_count % self.spec_stride == 0:
            self.spectra.append((local_dt(e["t_ns"]), r.astype(np.float32), np.asarray(e["mean"], np.float32)))
            if len(self.spectra) > 1500:
                self.spectra = self.spectra[::2]; self.spec_stride *= 2
        self._next_burst = e.get("next_burst", "")
        self.m["slots"].setText(f"{e['n_slots']:,}"); self.m["size"].setText(f"{e['size_mb']:.0f}"); self.m["int"].setText(f"{e['int_us'] / 1000:g}")
        self.m["n"].setText(str(e["n"])); self.m["peak"].setText(f"{e['bmax']:,.0f}"); self.m["sat"].setText(str(e["n_sat_slots"]))
        self.m["temp"].setText(f"{e['temp_c']:.1f}" if e.get("temp_c") is not None else "–")
        self._dirty_ts = True

    def _update_mask(self):
        """Pixels hidden by 'Hide hot and cold pixels': the list found from the dark sets (hotpix), or, for a run without
        one (recorded by LightKeeper 0.3.3 or earlier), outliers in the longest-integration mean dark."""
        if self.axis is None:
            return
        bad = np.zeros(len(self.axis), bool)
        if self.hot:
            idx = [i for i in list(self.hot.get("hot", [])) + list(self.hot.get("cold", [])) if 0 <= int(i) < len(bad)]
            bad[np.asarray(idx, int)] = True
            src = self.hot.get("source", "dark sets")
        elif self.darks:
            from scipy.ndimage import median_filter
            us = max(self.darks); d = self.darks[us]
            ex = d - median_filter(d, 15, mode="nearest"); core = ex[30:-30]
            sg = 1.4826 * np.median(np.abs(core - np.median(core)))
            bad = np.abs(ex) > 6 * sg; bad[:30] = bad[-30:] = False
            src = f"{us / 1000:g} ms mean dark"
        else:
            self.valid = np.ones(len(self.axis), bool); return
        self.valid = ~bad
        self.cb_mask.setToolTip(f"{int(bad.sum())} pixels, from the {src}")
        self._log(f"hot/cold pixels from the {src}: {int(bad.sum())} hidden at "
                  f"{', '.join(f'{x:.1f}' for x in self.axis[bad][:40])} nm")

    # =================================================================== drawing
    def _x(self):
        if self.viewing is not None and self.latest is None:
            return self.viewing["wl"]
        return self.axis if self.axis is not None else np.arange(len(self.latest))

    def _spectrum_channels(self, recorded=False):
        if self.cb_edges.isChecked():
            return slice(None)
        if self.viewing is not None and (recorded or self.latest is None):
            x = self.viewing["wl"]
            model = self.viewing.get("model", "")
            raw = self.spectra[-1][2] if self.spectra else None
        else:
            x = self._x()
            model = self.info.get("model", "")
            raw = self.latest if self.latest is not None else (self.spectra[-1][2] if self.spectra else None)
        key = (model, len(x), float(x[0]), float(x[-1]))
        if self._edge_layout is not None and self._edge_layout[0] == key:
            return self._edge_layout[1]
        channels = display.display_channels(model, len(x), raw)
        if channels.start is not None:
            self._edge_layout = (key, channels)
        return channels

    def _spectrum_mask(self, n, valid, recorded=False):
        show = np.zeros(n, bool)
        show[self._spectrum_channels(recorded)] = True
        if self.cb_mask.isChecked() and valid is not None and len(valid) == n:
            show &= valid
        return show

    def _draw_live(self, force=False):
        if self.latest is None:
            return
        self._scan_new = False; self._last_live = time.time()
        y = np.asarray(self.latest, float); x = self._x(); us = self.latest_int or 1
        d = self.darks.get(int(us))
        channels = self._spectrum_channels()
        show = self._spectrum_mask(len(y), self.valid)
        yy = y - d if (self.cb_dark.isChecked() and d is not None) else y
        k = 1e6 / us if self.cb_rate.isChecked() else 1.0
        a0, a1 = self.c_spec.ax
        self.l_live.set_data(x[show], yy[show] * k)
        if d is not None and not self.cb_dark.isChecked():
            self.l_dark.set_data(x[show], d[show] * k)
        else:
            self.l_dark.set_data([], [])
        self.l_sat.set_ydata([self._sat() * k] * 2); self.l_sat.set_visible(not self.cb_dark.isChecked())
        a0.set_yscale("log" if self.cb_log.isChecked() else "linear"); a0.set_ylabel("Counts/s" if k != 1 else "Counts")
        if not self.cb_log.isChecked():
            a0.yaxis.set_major_formatter(THOUSANDS)
        xx = x[channels]
        key = (len(xx), float(xx[0]), float(xx[-1]))
        if force or getattr(self, "_strip_key", None) != key:
            self._strip_key = key; self._draw_strip()
        self._live_shown = (yy * k, show, "counts/s" if k != 1 else "counts")
        if self.cb_auto.isChecked():
            a0.set_xlim(xx[0], xx[-1]); self._fit_y(a0)
        elif self._full_hold:
            self._apply_full_scale()
        peak = float(np.max(y[show])) if show.any() else 0
        hidden = int((~show[channels]).sum())
        edge_info = f", {len(x) - len(xx)} terminal channels hidden" if len(xx) < len(x) else ""
        self.live_info.setText(f"{us / 1000:g} ms, peak {peak:,.0f} counts, {'matched dark available' if d is not None else 'no matched dark'}, "
                               + (f"{hidden} hot/cold pixels hidden, " if hidden else
                                  ("hot pixels shown, " if not self.cb_mask.isChecked() else "no hot-pixel list (take a dark set), "))
                               + self.axis_label + edge_info)
        if force or len(self.spectra) and getattr(self, "_drawn_spec", 0) != len(self.spectra):
            self._draw_recent()
        self.spec_cursor.sync(); self._spec_cursor_text()
        self.c_spec.draw_idle()

    def _draw_recent(self):
        """The lower Spectrum plot: the 24 stored slots up to the slider's position, newest brightest. The slider runs from
        the start of usable light to the newest slot; at the far right it stays on the newest slot as new ones arrive."""
        if self.axis is None and self.latest is None:
            return
        x = self.viewing["wl"] if self.viewing is not None else self._x()
        a0, a1 = self.c_spec.ax
        if self.latest is None and self.cb_auto.isChecked():          # a recorded run with no live scan above it
            xx = x[self._spectrum_channels(recorded=True)]
            a0.set_xlim(xx[0], xx[-1]); self._strip_key = None; self._draw_strip()
        valid = self.viewing["valid"] if self.viewing is not None else self.valid
        show = self._spectrum_mask(len(x), valid, recorded=True)
        xl = a0.get_xlim()
        first, i = self._sync_scrub()
        recent = self.spectra[max(first, i - 23):i + 1] if self.spectra else []
        self._recent_shown = (recent[-1][0], x[show], recent[-1][1][show]) if recent else None
        self._recent_full = (recent[-1][0], np.asarray(recent[-1][1], float), show) if recent else None
        a1.cla(); style_ax(a1)
        a1.set_title("Recent slots, newest brightest" if self._scrub_t is None or not recent else f"Slots up to {recent[-1][0]:%H:%M:%S}, newest brightest")
        cm = RECENT_CMAP
        for j, sp in enumerate(recent):
            a1.plot(x[show], sp[1][show], lw=0.6, color=cm(j / max(len(recent) - 1, 1)))
        if not recent:
            a1.text(0.5, 0.5, "Recorded slots appear here during a run", transform=a1.transAxes, ha="center", va="center", color=TEXT3)
            a1.set_yticks([])
        if recent:
            a1.text(0.99, 0.95, f"{recent[0][0]:%H:%M:%S} → {recent[-1][0]:%H:%M:%S}", transform=a1.transAxes, ha="right", va="top", color=MUTED, fontsize=8)
        a1.set_xlabel(f"Wavelength (nm, {self.axis_label})"); a1.set_ylabel("Dark-corrected counts/s")
        a1.yaxis.set_major_formatter(THOUSANDS)
        self._drawn_spec = len(self.spectra)
        a0.set_xlim(xl)
        if self._a1_ylim is not None and not self.cb_auto.isChecked():
            a1.set_ylim(self._a1_ylim)
        else:
            self._fit_y(a1, xl)
            if not self.cb_auto.isChecked():      # Auto-scale off: hold this scale, so the slider shows the light changing
                self._a1_ylim = a1.get_ylim()
        self.spec_cursor.sync(); self._spec_cursor_text()
        self.c_spec.draw_idle()

    def _usable_first(self) -> int:
        """Index in self.spectra of the first slot of usable light (0 if none is found yet). Worked out once a minute of slots."""
        key = (len(self.slots) // 60, len(self.spectra))
        if self._usable_cache[0] != key:
            w_ = runview.usable_window([s["t"] for s in self.slots], [s["par"] for s in self.slots]) if len(self.slots) > 2 else None
            times = [sp[0] for sp in self.spectra]
            self._usable_cache = (key, min(bisect_left(times, w_[0]), max(len(times) - 1, 0)) if w_ else 0)
        return self._usable_cache[1]

    def _sync_scrub(self):
        """Fit the slider to the stored slots and return (first index, index shown). It follows a time, not an index,
        because the store thins itself out as a run grows."""
        n = len(self.spectra)
        if not n:
            self._scrub_t = None
            self.scrub.blockSignals(True); self.scrub.setRange(0, 0); self.scrub.blockSignals(False); self.scrub.setEnabled(False)
            self.scrub_first.setText(""); self.scrub_last.setText("")
            return 0, 0
        first = self._usable_first()
        i = n - 1 if self._scrub_t is None else min(n - 1, max(first, bisect_left([sp[0] for sp in self.spectra], self._scrub_t)))
        self.scrub.blockSignals(True); self.scrub.setRange(first, n - 1); self.scrub.setValue(i); self.scrub.blockSignals(False)
        self.scrub.setEnabled(n - 1 > first)
        self.scrub_first.setText(f"{self.spectra[first][0]:%H:%M}"); self.scrub_last.setText(f"{self.spectra[-1][0]:%H:%M}")
        return first, i

    def _scrub_moved(self, v):
        self._scrub_t = None if v >= self.scrub.maximum() else self.spectra[v][0]
        self._scrub_timer.start()

    def _cursor_pixels(self):
        return None if self.axis is None and self.latest is None else self._x()

    def _pixel_at(self, x):
        """Index of the pixel nearest wavelength x on the axis shown, or None."""
        xs = self._cursor_pixels()
        if xs is None or not len(xs) or x is None:
            return None
        return int(np.argmin(np.abs(np.asarray(xs, float) - float(x))))

    def _readout(self, i) -> list:
        """0.4.2: wavelength, pixel and the values at exactly pixel i (not the nearest pixel still drawn, which is a
        neighbour when a hot pixel is hidden)."""
        xs = self._cursor_pixels(); parts = [f"<b>{float(xs[i]):.2f} nm</b> (pixel {i})"]
        if self._live_shown is not None and i < len(self._live_shown[0]):
            v, show, unit = self._live_shown
            hidden = "terminal channel" if i not in range(len(xs))[self._spectrum_channels()] else "hot/cold pixel"
            parts.append(f"live {v[i]:,.1f} {unit}" if show[i] else f"live: hidden {hidden}")
        if (self._recent_full is not None and i < len(self._recent_full[1])
                and (self.viewing is None or self.latest is None or np.array_equal(self.viewing["wl"], xs))):
            t, v, show = self._recent_full
            parts.append(f"{t:%H:%M:%S} slot {v[i]:,.1f} counts/s" if show[i] else f"{t:%H:%M:%S} slot: hidden pixel")
        return parts

    def _spec_hover(self, ev):
        """Pointing at a plot reads the wavelength there, with no click needed."""
        axes = list(self.c_spec.ax) + [self.ax_strip]
        i = self._pixel_at(ev.xdata) if ev is not None and ev.inaxes in axes and ev.xdata is not None else None
        if i != self._hover_i:
            self._hover_i = i; self._spec_cursor_text()

    def _spec_cursor_text(self):
        c = self.spec_cursor; out = []
        if c.on and c.x is not None:
            i = self._pixel_at(c.x)
            if i is not None:
                out.append("Cursor " + ", ".join(self._readout(i)))
        elif c.on:
            out.append(c.hint or "Click a plot to place the cursor")
        if c.on and c.x is not None and c.hint:
            out.append(c.hint)
        if self._hover_i is not None and self._cursor_pixels() is not None and self._hover_i < len(self._cursor_pixels()):
            out.append("Pointer " + ", ".join(self._readout(self._hover_i)))
        self.spec_cur_info.setText("  ·  ".join(out))

    def _wf_cursor_text(self):
        c = self.wf_cursor
        if not c.on or c.x is None or self._wf_shown is None:
            self.wf_cur_info.setText("Click the plot to place the cursor" if c.on else ""); return
        xs, tn, Z, unit = self._wf_shown
        i = int(np.argmin(np.abs(xs - c.x))); j = int(np.argmin(np.abs(tn - c.y))) if c.y is not None else len(tn) - 1
        self.wf_cur_info.setText(f"<b>{xs[i]:.2f} nm</b>, {mdates.num2date(tn[j], tz=LOCAL_TZ):%H:%M:%S}, {Z[j, i]:,.3g} {unit}")

    # ---- y-axis scaling of the Spectrum tab (0.2.0)
    def _fit_y(self, ax, xlim=None):
        """Fit the y-axis to the plotted spectra (the saturation line is ignored), optionally within an x range."""
        ys = []
        for ln in ax.get_lines():
            if ln is self.l_sat or ln.get_gid() in ("sat", "cursor") or not ln.get_visible():
                continue
            xd = np.asarray(ln.get_xdata(), float); yd = np.asarray(ln.get_ydata(), float)
            if yd.size == 0 or xd.size != yd.size:
                continue
            m = np.isfinite(yd)
            if xlim is not None:
                lo, hi = sorted(xlim); m &= (xd >= lo) & (xd <= hi)
            ys.append(yd[m])
        y = np.concatenate(ys) if ys else np.array([])
        if ax.get_yscale() == "log":
            y = y[y > 0]
            if y.size:
                ax.set_ylim(y.min() / 1.3, y.max() * 1.3)
            return
        if y.size:
            lo, hi = float(y.min()), float(y.max()); span = (hi - lo) or max(abs(hi), 1.0)
            ax.set_ylim(lo - 0.04 * span, hi + 0.06 * span)

    def _manual_view(self):
        if self.cb_auto.isChecked():
            self.cb_auto.blockSignals(True); self.cb_auto.setChecked(False); self.cb_auto.blockSignals(False)

    def _on_scroll(self, ev):
        ax = ev.inaxes
        if ev.xdata is None or ev.ydata is None or not (ax in self.c_spec.ax or ax in self.c_cal.ax):
            return
        canvas = self.c_spec if ax in self.c_spec.ax else self.c_cal
        f = 1 / 1.25 if ev.button == "up" else 1.25
        mods = set(getattr(ev, "modifiers", None) or ()) | ({ev.key} if ev.key else set())
        do_x = bool(mods & {"shift", "ctrl", "control"}); do_y = not (mods & {"shift"}) or bool(mods & {"ctrl", "control"})

        def zoom(get, put, c, log):
            lo, hi = get()
            if log:
                lo, hi, c = np.log10(max(lo, 1e-12)), np.log10(max(hi, 1e-12)), np.log10(max(c, 1e-12))
            lo, hi = c - (c - lo) * f, c + (hi - c) * f
            put((10 ** lo, 10 ** hi) if log else (lo, hi))
        if do_y:
            zoom(ax.get_ylim, ax.set_ylim, ev.ydata, ax.get_yscale() == "log")
            if ax is self.c_spec.ax[1]:
                self._a1_ylim = ax.get_ylim()
        if do_x:
            zoom(ax.get_xlim, ax.set_xlim, ev.xdata, False)
        if canvas is self.c_spec:
            self._manual_view(); self._full_hold = False
        canvas.draw_idle()

    def _on_click(self, ev):
        if ev.dblclick and ev.inaxes in self.c_cal.ax:
            self._draw_cal(); return
        if ev.dblclick and ev.inaxes in self.c_spec.ax:
            self._a1_ylim = None; self._full_hold = False
            if self.cb_auto.isChecked():
                self._draw_live(force=True)
            else:
                self.cb_auto.setChecked(True)

    def _on_release(self, ev):
        """A pan/zoom made with the toolbar is kept (Auto-scale unticks itself)."""
        if getattr(self.spec_tb, "mode", "") and ev.inaxes in self.c_spec.ax:
            self._manual_view(); self._full_hold = False
            self._a1_ylim = self.c_spec.ax[1].get_ylim()

    def _cal_fit_y(self):
        a0, a1 = self.c_cal.ax; xl = a0.get_xlim()
        self._fit_y(a0, xl); self._fit_y(a1, xl); self.c_cal.draw_idle()

    def _full_scale(self):
        """0.4.2: the whole spectrum against the detector's full range, held while the live view updates."""
        if self.latest is None and self.axis is None:
            return
        x = self._x()[self._spectrum_channels()]; a0 = self.c_spec.ax[0]
        a0.set_xlim(float(x[0]), float(x[-1]))
        self._manual_view(); self._full_hold = True
        self._apply_full_scale(); self.spec_cursor.sync(); self.c_spec.draw_idle()

    def _apply_full_scale(self):
        """y from 0 (or the lowest value, if below 0) to 3 % above the largest count the detector can give, in the units shown."""
        a0 = self.c_spec.ax[0]
        top = float((self.info or {}).get("native_max_counts") or 65535.0)
        k = 1e6 / (self.latest_int or 1) if self.cb_rate.isChecked() else 1.0
        yd = np.asarray(self.l_live.get_ydata(), float); yd = yd[np.isfinite(yd)]
        if a0.get_yscale() == "log":
            pos = yd[yd > 0]
            a0.set_ylim(max(float(pos.min()) / 1.3, 1e-3) if pos.size else 1.0, top * k * 1.15)
        else:
            lo = min(0.0, float(yd.min())) if yd.size else 0.0
            hi = top * k * 1.03
            a0.set_ylim(lo - 0.02 * (hi - lo), hi)

    def _fit_y_now(self):
        self._full_hold = False
        a0, a1 = self.c_spec.ax; xl = a0.get_xlim()
        self._fit_y(a0, xl); self._fit_y(a1, xl); self._a1_ylim = a1.get_ylim()
        self.c_spec.draw_idle()

    def _fit_xy_now(self):
        if self.latest is None and self.axis is None:
            return
        self._full_hold = False
        self._a1_ylim = None
        x = self._x()[self._spectrum_channels()]
        self.c_spec.ax[0].set_xlim(float(x[0]), float(x[-1]))
        if self.latest is not None:
            self._draw_live(force=True)
        else:
            self._draw_strip()
            self._draw_recent()
        self._fit_y_now()
        self.spec_cursor.sync()

    def _draw_timeseries(self):
        if not self.slots:
            return
        shown, rtxt = self._in_window(self.slots, lambda s: s["t"])
        if not shown:
            return
        S = shown[::max(1, len(shown) // 6000)]
        t = [s["t"] for s in S]
        for a in self.c_ts.ax:
            a.cla(); style_ax(a)
        A = self.ts_ax; first = next(iter(A))
        tag = lambda k, txt: txt + (f" · {rtxt}" if k == first else "")   # noqa: E731
        if "par" in A:
            a = A["par"]; a.plot(t, [s["par"] for s in S], color=GOLD, lw=0.8); a.set_yscale("log")
            a.set_title(tag("par", "420–700 nm rate (counts/s, instrument-relative)"), loc="left", fontsize=9)
        if "rfr" in A:
            A["rfr"].plot(t, [s["rfr"] for s in S], color=RED, lw=0.8); A["rfr"].set_title(tag("rfr", "R:FR counts (655–665 / 725–735)"), loc="left", fontsize=9)
        if "bg" in A:
            A["bg"].plot(t, [s.get("bg", np.nan) for s in S], color=BLUE, lw=0.8)
            A["bg"].set_title(tag("bg", "B:G counts (420–490 / 500–570 nm)"), loc="left", fontsize=9)
        if "frshare" in A:
            A["frshare"].plot(t, [100 * s.get("frshare", np.nan) for s in S], color=PURPLE, lw=0.8)
            A["frshare"].set_title(tag("frshare", "Far-red share of counts (700–750 / 400–750 nm)"), loc="left", fontsize=9)
            A["frshare"].set_ylabel("%")
        if "nirred" in A:
            A["nirred"].plot(t, [s["nirred"] for s in S], color=TEAL, lw=0.8); A["nirred"].set_title(tag("nirred", "NIR/red (780–820 / 665–680)"), loc="left", fontsize=9)
        if "o2a" in A:
            A["o2a"].plot(t, [s["o2a"] for s in S], color=BLUE, lw=0.8)
            A["o2a"].set_title(tag("o2a", "O₂-A apparent depth (760 nm; shoulders 754–756 / 770–772)"), loc="left", fontsize=9)
        if getattr(self, "_ts_twin", None) is not None:
            try:
                self._ts_twin.remove()
            except Exception:
                pass
            self._ts_twin = None
        if "int" in A:
            a = A["int"]
            a.step(t, [s["int_ms"] for s in S], color=GOLD, lw=0.8, where="mid"); a.set_yscale("log"); a.set_ylabel("ms")
            b = self._ts_twin = a.twinx(); b.plot(t, [s["n"] for s in S], ".", ms=1.5, color=MUTED); b.tick_params(colors=MUTED, labelsize=8)
            sat = [s["t"] for s in S if s["nsat"] > 0]
            if sat:
                a.plot(sat, [S[0]["int_ms"]] * len(sat), "x", color=RED, ms=4)
            a.set_title(tag("int", "integration (ms, gold) · scans per slot (grey) · saturated slots (red ×)"), loc="left", fontsize=9)
        for a in self.c_ts.ax[:-1]:
            a.tick_params(labelbottom=False)
        self.c_ts.ax[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
        self.c_ts.draw_idle()

    def _draw_waterfall(self):
        if len(self.spectra) < 2 or self.axis is None:
            return
        shown, rtxt = self._in_window(self.spectra, lambda sp: sp[0])
        if len(shown) < 2:
            return
        a = self.c_wf.ax[0]; a.cla(); style_ax(a)
        mode = self.wf_mode.currentIndex()
        R = np.array([sp[2 if mode == 3 else 1] for sp in shown], float); t = [sp[0] for sp in shown]
        valid = self.viewing["valid"] if self.viewing is not None else self.valid
        if mode != 3 and self.cb_mask.isChecked() and valid is not None:
            R = hotpix.fill(R, valid)
        x = self.viewing["wl"] if self.viewing is not None else self.axis
        axis_label = "device axis" if self.viewing is not None else self.axis_label
        m = (x > 380) & (x < 1050)
        with np.errstate(divide="ignore", invalid="ignore"):
            if mode == 3:
                Z = R; lab = "counts per scan, no dark subtraction or scaling"
            elif mode == 0:
                Z = R / np.nanmean(R[:, (x >= 780) & (x <= 820)], 1, keepdims=True); lab = "relative to 780–820 nm"
            elif mode == 1:
                Z = np.log10(np.clip(R, 1, None)); lab = "log10 counts/s"
            else:
                Z = R / R[0]; lab = "ratio to first slot"
        Z = Z[:, m]
        lo, hi = np.nanpercentile(Z, [1, 99])
        tn = mdates.date2num(t)
        im = a.imshow(Z, aspect="auto", origin="lower", extent=[x[m][0], x[m][-1], tn[0], tn[-1]], cmap="magma", vmin=lo, vmax=hi, interpolation="nearest")
        a.yaxis_date(tz=LOCAL_TZ); a.yaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
        a.set_xlabel(f"wavelength (nm, {axis_label})"); a.set_title(f"Recorded spectra over time ({len(shown)} shown, every {self.spec_stride} slot(s)) · {rtxt}", loc="left")
        if self.wf_cbar is not None:
            try:
                self.wf_cbar.remove()
            except Exception:
                pass
        self.wf_cbar = self.c_wf.fig.colorbar(im, ax=a, label=lab); self.wf_cbar.ax.tick_params(colors=MUTED, labelsize=8)
        self._wf_shown = (x[m], np.asarray(tn), Z, lab.split(",")[0]); self.wf_cursor.sync(); self._wf_cursor_text()
        self.c_wf.draw_idle()

    def _update_schedule(self, *_):
        if self._applying_setup:       # filling the form field by field: drawn once at the end
            return
        # While a run is armed or recording, show the run the engine is executing: its plan snapshot and the start/stop
        # times fixed when Start was pressed. Re-resolving a sun-event plan from the form after today's event would
        # return tomorrow's times (bug in 0.1.0: display only, the engine always used the fixed times).
        running = self.run_plan is not None and self.run_times is not None
        try:
            npx = len(self.wl_dev) if self.wl_dev is not None else 2068
            lim = (int(self.info.get("min_int_us_used", 8000)), int(self.info.get("max_int_us", 6e7)))
            if running:
                plan = RunPlan.from_dict(self.run_plan)
                e = estimates(plan, npx, *lim, t0=self.run_times[0], t1=self.run_times[1])
            else:
                plan = self._plan()
                e = estimates(plan, npx, *lim)
        except Exception as ex:
            self.sch_text.setPlainText(f"The plan cannot be evaluated: {ex}"); return
        a = self.c_sch.ax[0]; a.cla(); style_ax(a)
        t0, t1 = e["start"], e["stop"]
        pre = dt.timedelta(seconds=e["dark_session_s"] if plan.pre_darks else 0)
        days = sorted({(t0 - pre).date(), t1.date()})
        tt, el = [], []
        for d in days:
            tl, elv, rise, noon, sset = solar_day(d, plan.site_lat, plan.site_lon); tt += tl; el += list(elv)
            for tm, lab in [(rise, "sunrise"), (noon, "solar noon"), (sset, "sunset")]:
                if tm:
                    a.axvline(tm, color=MUTED, lw=0.6, ls=":"); a.text(tm, 1.0, f" {lab} {tm:%H:%M}", rotation=90, va="bottom", ha="right", color=MUTED, fontsize=7,
                                                                      transform=a.get_xaxis_transform())
        for lo, hi, al in [(-90, -18, 0.0), (-18, -12, 0.10), (-12, -6, 0.17), (-6, -0.833, 0.25)]:
            a.axhspan(lo, hi, color=BLUE, alpha=al, lw=0)
        for y, lab in [(-18, "Astronomical"), (-12, "Nautical"), (-6, "Civil")]:
            a.text(0.002, y + 0.5, lab, transform=a.get_yaxis_transform(), color=MUTED, fontsize=7)
        a.plot(tt, el, color=GOLD, lw=1.2, label="Solar elevation")
        a.axhline(0, color="#31475e", lw=0.8)
        if plan.pre_darks:
            a.axvspan(t0 - pre, t0, color=PURPLE, alpha=0.55, lw=0, label="Dark set before")
        a.axvspan(t0, t1, color=LAMP, alpha=0.07, lw=0, label="Recording")
        if plan.post_darks:
            a.axvspan(t1, t1 + dt.timedelta(seconds=e["dark_session_s"]), color=PURPLE, alpha=0.55, lw=0, label="Dark set after")
        for j, b in enumerate(e["bursts"]):
            a.axvspan(b, b + dt.timedelta(seconds=plan.burst_s), color=ORANGE, alpha=0.45, lw=0, label="Fast burst" if j == 0 else None)
        now = dt.datetime.now(LOCAL_TZ); a.axvline(now, color=TEAL, lw=1.0, label="Now")
        a.set_xlim(min(t0 - pre, now) - dt.timedelta(minutes=45), t1 + dt.timedelta(minutes=45)); a.set_ylim(-25, 90)
        a.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ)); a.set_ylabel("Solar elevation (°)")
        a.set_title(f"Schedule for {plan.label}" + (", running (times fixed at Start)" if running else ""), loc="left", pad=40)
        a.legend(loc="upper left", fontsize=7.5, frameon=True, facecolor=PANEL, edgecolor=LINE, labelcolor=TEXT2)
        self.c_sch.draw_idle()
        ev = sun_events(t0.date(), plan.site_lat, plan.site_lon)
        if running:
            self.when_lbl.setText(f"Running: {t0:%a %H:%M:%S} to {t1:%a %H:%M:%S}. These times were fixed when the run started; "
                                  "changes here apply to the next run.")
        else:
            self.when_lbl.setText(f"Records {t0:%a %H:%M:%S} to {t1:%a %H:%M:%S}, {e['duration_h']:.2f} h.")
        start_txt = "Now, after the pre-run darks" if plan.start_now and not running else f"{t0:%a %d %b, %H:%M:%S %Z}"
        if plan.start_event and not plan.start_now:
            start_txt += f"<br><span style='color:{TEXT2}'>{plan.start_event}, {plan.start_offset_min:+g} min</span>"
        stop_txt = f"{t1:%a %d %b, %H:%M:%S %Z}"
        if plan.stop_event:
            stop_txt += f"<br><span style='color:{TEXT2}'>{plan.stop_event}, {plan.stop_offset_min:+g} min</span>"
        bursts = ", ".join(f"{b:%H:%M}" for b in e["bursts"])
        rows = [("Start", start_txt), ("Stop", stop_txt), ("Duration", f"{e['duration_h']:.2f} h, {e['slots']:,} slots of {plan.slot_s:g} s"),
                ("Exposure", f"Automatic, {', '.join(f'{x:g}' for x in e['ladder_ms'])} ms, starting at {plan.int_ms:g} ms"
                             if plan.auto_exposure else f"Fixed at {plan.int_ms:g} ms" + _backoff_txt(plan)),
                ("Dark times", f"{', '.join(f'{x:g}' for x in e['dark_ladder_ms'])} ms"),
                ("Dark sets", f"{' and '.join(x for x, on in (('before', plan.pre_darks), ('after', plan.post_darks)) if on) or 'none'}, "
                              f"about {e['dark_session_s']:.0f} s each ({e['dark_scans_total']:,} scans)"),
                ("Bursts", f"{len(e['bursts'])} of {plan.burst_s:g} s at {e['burst_int_ms']:g} ms" + (f": {bursts}" if bursts else "")),
                ("Data", f"About {e['data_gb_expected']:.2f} GB; {e['disk_free_gb']:.0f} GB free"),
                ("Site", site_text(plan.site_lat, plan.site_lon) + (
                    f"<br><span style='color:{ORANGE}'>{site_problem(plan.site_lat, plan.site_lon)} "
                    f"Set it under Show advanced settings.</span>" if site_problem(plan.site_lat, plan.site_lon) else ""))]
        sun = [(k[0].upper() + k[1:], f"{v:%H:%M:%S}" if v else "–") for k, v in sorted(ev.items(), key=lambda kv: kv[1] or t0)]
        td = f"style='padding:3px 14px 3px 0; color:{TEXT2}; vertical-align:top; white-space:nowrap'"
        html = (f"<div style='color:{TEXT}'>"
                + (f"<p style='color:{LAMP}'><b>Run in progress.</b> These times were fixed when the run started.</p>" if running else "")
                + "<table cellspacing=0>" + "".join(f"<tr><td {td}>{k}</td><td style='padding:3px 0'>{v}</td></tr>" for k, v in rows) + "</table>"
                + f"<p style='margin-top:14px'><b>Sun events, {t0:%a %d %b %Y}</b></p><table cellspacing=0>"
                + "".join(f"<tr><td {td}>{k}</td><td style='padding:3px 0'>{v}</td></tr>" for k, v in sun) + "</table>"
                + f"<p style='color:{TEXT2}; margin-top:14px'>Darks cannot be taken during a run, so the sets before and after cover "
                  "every integration time the run may use.</p></div>")
        self.sch_text.setHtml(html)

    # =================================================================== calibration
    def _cal_acquire(self, kind):
        if self.state not in ("IDLE", "PREVIEW"):
            Q.QMessageBox.information(self, APP_NAME, "Connect a spectrometer and stop any run first."); return
        if kind == "dark":
            try:
                ladder = [int(round(float(x) * 1000)) for x in self.ladder_edit.text().replace(";", ",").split(",") if x.strip()]
            except ValueError:
                ladder = []
            times = sorted(set(ladder) | {int(round(self.cal_int.value() * 1000))})
            est = sum((max(5, min(30, round(self.dark_s.value() * 1e6 / u))) + 2) * u / 1e6 for u in times)
            if self._ask("Calibration: dark", f"Turn the lamp OFF (or cover the input) and keep it covered.\n\nDarks at "
                         f"{len(times)} integration times ({', '.join(f'{u / 1000:g}' for u in times)} ms), about {est:.0f} s. "
                         "The longest also finds the hot and cold pixels, which are removed before the fit.", "Acquire", "Cancel"):
                self.engine.send("live_dark", max(times), None, 5, times, self.dark_s.value(), "calibration")
            return
        msg = f"Turn the {self.lamp_combo.currentText()} lamp ON and aim it at the input. Avoid saturation (peak < {0.92 * self._sat():,.0f} counts)."
        if self._ask("Calibration", msg, "Acquire", "Cancel"):
            self.engine.send("lamp", kind, self.cal_int.value(), self.cal_n.value(), self.lamp_combo.currentText())

    def _cal_fit(self):
        if "lamp" not in self.cal:
            Q.QMessageBox.information(self, APP_NAME, "Acquire a lamp spectrum first."); return
        lamp = self.cal["lamp"]; dark = self._cal_matched_dark()
        if dark is None:
            self._log("calibration: no dark at the same integration time -> UV-floor pedestal used instead")
            base = np.median(lamp["mean"][(self.wl_dev > 205) & (self.wl_dev < 290)]); y = lamp["mean"] - base; dmean = np.full(len(y), base)
        else:
            y = lamp["mean"] - dark; dmean = dark
        if self.valid is not None and not self.valid.all():
            y = hotpix.fill(y, self.valid)          # hot and cold pixels from the dark set must not look like lines
            self._log(f"calibration: {int((~self.valid).sum())} hot/cold pixels bridged before peak finding")
        r = fit_calibration(self.wl_dev, y, self.lamp_combo.currentText(), deg=self.cal_deg.value(), min_rel=self.cal_minrel.value(),
                            isolation_nm=self.cal_iso.value(), snr=self.cal_snr.value(), raw_max=lamp["raw_max"], sat=self._sat())
        self.cal_result = r; self._cal_y = y; self._cal_dark = dmean
        if not r["ok"]:
            self.cal_text.setPlainText("FIT FAILED\n" + r["message"]); self._draw_cal(); return
        ov = r["oceanview"]
        self.cal_text.setPlainText(
            f"{r['lamp']}  ({', '.join(r['species'])})\nlines used {r['n_used']} of {r['n_matched']} matched; {r['n_saturated_rejected']} saturated peaks rejected\n"
            f"rms {r['rms_nm']:.3f} nm · max {r['max_abs_nm']:.3f} nm · leave-one-out rms {r['loo_rms_nm']:.3f} nm\n"
            f"coverage {r['coverage_nm'][0]:.1f}–{r['coverage_nm'][1]:.1f} nm (extrapolated outside)\n\n"
            f"OceanView coefficients (pixel 0..N-1)\n  Intercept  {ov['intercept']}\n  1st        {ov['c1']}\n  2nd        {ov['c2']}\n  3rd        {ov['c3']}\n\n"
            f"device axis − new axis (nm):\n  " + "  ".join(f"{k}: {v:+.3f}" for k, v in r["device_minus_fit_at"].items()) +
            "\n\nThe device EEPROM is NOT changed. Recorded data always keep the device axis + pixel index;\napply the saved calibration in analysis.")
        self._draw_cal(); self._cal_steps()

    def _cal_matched_dark(self):
        """Mean dark at the lamp's integration time: from the dark set (step 1), else a single-time dark."""
        us = self.cal["lamp"]["int_us"]
        for k, v in self.cal_darks.items():
            if abs(k - us) <= max(1, us * 0.01):
                return v
        d = self.cal.get("dark")
        return d["mean"] if d is not None and d["int_us"] == us else None

    def _draw_cal(self):
        a0, a1 = self.c_cal.ax; a0.cla(); a1.cla(); style_ax(a0); style_ax(a1)
        a0.set_title("Lamp spectrum (dark-subtracted) with NIST reference lines", loc="left"); a1.set_title("Fit residual (NIST − fit)", loc="left")
        x = self.wl_dev
        if x is None:
            self.c_cal.draw_idle(); return
        if "lamp" in self.cal:
            y = getattr(self, "_cal_y", None)
            if y is None:
                d = self._cal_matched_dark()
                y = self.cal["lamp"]["mean"] - (d if d is not None else 0)
                if self.valid is not None:
                    y = hotpix.fill(y, self.valid)
            a0.plot(x, y, color=TEAL, lw=0.7)
            a0.axhline(self._sat(), color=RED, ls="--", lw=0.6, gid="sat")
            refs = reference_lines(self.lamp_combo.currentText(), float(x[5]), float(x[-5]), self.cal_minrel.value(), self.cal_iso.value())
            top = float(np.nanmax(y)) if np.isfinite(y).any() else 1
            for r in refs:
                a0.plot([r["wl"]] * 2, [top * 1.02, top * 1.08], color=MUTED, lw=0.6)
        r = self.cal_result
        if r and r.get("ok"):
            for ln in r["lines"]:
                c = GOLD if ln["used"] else RED
                a0.plot([ln["device_nm"]] * 2, [0, ln["height"]], color=c, lw=0.5, alpha=0.6)
                a0.text(ln["device_nm"], ln["height"], f" {ln['species']} {ln['ref_nm']:.2f}", rotation=90, fontsize=6, color=c, va="bottom")
                a1.plot(ln["ref_nm"], ln["resid_nm"], "o" if ln["used"] else "x", color=c, ms=4)
            a1.axhline(0, color=MUTED, lw=0.6); a1.set_ylabel("nm"); a1.set_xlabel("wavelength (nm)")
            a1.text(0.01, 0.92, f"rms {r['rms_nm']:.3f} nm · {r['n_used']} lines", transform=a1.transAxes, color=TEXT, fontsize=8)
        self.c_cal.draw_idle()

    def _cal_save(self):
        r = self.cal_result
        if not r or not r.get("ok"):
            Q.QMessageBox.information(self, APP_NAME, "Fit a calibration first."); return
        root = self._output_dir()
        if not root:
            Q.QMessageBox.warning(self, APP_NAME, "Reconnect the saved data drive or choose a folder in Save in.")
            return
        folder = Path(root).parent / "calibrations"
        lamp = self.cal["lamp"]
        meta = dict(app=f"{APP_NAME} {VERSION}", device=self.info, lamp_int_us=lamp["int_us"], lamp_scans=lamp["n"],
                    lamp_time_local=local_dt(lamp["t_ns"]).isoformat(), dark=self._cal_matched_dark() is not None,
                    hot_cold_pixels_bridged=[int(i) for i in np.flatnonzero(~self.valid)] if self.valid is not None else [],
                    notes=self.notes_edit.text())
        out = save_calibration(folder, r, self.wl_dev, self._cal_y, self._cal_dark, lamp["raw_max"], meta)
        self._log(f"calibration saved: {out}"); self.statusBar().showMessage(f"calibration saved to {out}", 10000)
        self._cal_saved = True; self._cal_steps()

    def _cal_use(self, r=None):
        r = r or self.cal_result
        if not r or not r.get("ok") or self.wl_dev is None:
            return
        self.axis = np.polyval(np.array(r["coefficients_low_to_high"])[::-1], np.arange(len(self.wl_dev)))
        self.axis_label = f"calibrated: {r['lamp']}, rms {r['rms_nm']:.3f} nm"; self.cb_cal.setEnabled(True); self.cb_cal.setChecked(True)
        self._log(f"display axis -> {self.axis_label}"); self._draw_live(force=True)

    def _cal_load(self):
        root = self._output_dir()
        folder = str(Path(root).parent / "calibrations") if root else ""
        fn, _ = Q.QFileDialog.getOpenFileName(self, "Load calibration.json", folder, "calibration.json (*.json)")
        if fn:
            r = json.loads(Path(fn).read_text())["result"]; self.cal_result = r; self._cal_use(r)

    # =================================================================== close
    def closeEvent(self, ev):
        if self.link is not None or self._expect_attach is not None:
            if not self.auto_confirm:
                Q.QMessageBox.information(self, APP_NAME, "Closing this window does not stop the run.\n\nThe background recorder keeps "
                                          "recording; its LightKeeper icon in the notification area shows the state (double-click it to "
                                          "reopen this window). Stop or interrupt the run from that icon's menu or from this window.")
            if self.link is not None:
                self.link.close(); self.link = None
        self.engine.send("quit"); self.engine.join(15); shutdown_sdk(); ev.accept()


def main(argv=None):
    from .splash import main as launch
    return launch(argv)
