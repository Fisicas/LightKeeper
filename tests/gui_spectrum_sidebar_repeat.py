"""Offscreen checks for sidebar scrolling, detector display and fresh-scan saves."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LIGHTKEEPER_AUTOCONNECT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets as Q  # noqa: E402
from lightkeeper import ui  # noqa: E402


def check_window(folder):
    os.environ["LIGHTKEEPER_HOME"] = str(folder / "settings")
    window = ui.Window(auto_confirm=True)
    try:
        window.resize(1500, 720)
        window.show()
        app.processEvents()
        scroll = window.setup_scroll
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 100
        assert scroll.horizontalScrollBar().maximum() == 0
        original_width = scroll.viewport().width()
        original_range = bar.maximum()
        point = QtCore.QPointF(10, 10)
        positions = []
        for _ in range(4):
            wheel = QtGui.QWheelEvent(point, point, QtCore.QPoint(), QtCore.QPoint(0, -120),
                                      QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                                      QtCore.Qt.ScrollPhase.NoScrollPhase, False)
            app.sendEvent(scroll.viewport(), wheel)
            app.processEvents()
            positions.append(bar.value())
        assert all(a < b for a, b in zip([0] + positions, positions)), positions
        assert max(np.diff([0] + positions)) < 100, positions
        bar.setSliderDown(True)
        for position in range(17, 180, 17):
            bar.setSliderPosition(position)
            app.processEvents()
            assert bar.value() == position
            assert scroll.widget().y() == -position
            assert scroll.viewport().width() == original_width and bar.maximum() == original_range
            assert window._scrolling_setup()
        bar.setSliderDown(False)

        window.info = {"model": "MayaPro2000", "serial": "SIM", "pixels": 2068}
        window.axis = window.wl_dev = np.linspace(200, 1123, 2068)
        window.valid = np.ones(2068, bool)
        window.latest = np.full(2068, 300.0)
        window.latest[0] = 30_000
        window.latest[-1] = -20_000
        window.latest_int = 50_000
        window.latest_t = time.time_ns()
        window._set_state("PREVIEW")
        window._draw_live(force=True)
        original_x = window.axis[[10, 2057]]
        live = window.c_spec.ax[0]
        np.testing.assert_allclose(live.get_xlim(), original_x)
        assert live.get_ylim()[1] < 400
        live.set_xlim(400, 650)
        window.btn_fitxy.click()
        np.testing.assert_allclose(live.get_xlim(), original_x)
        window.cb_edges.setChecked(True)
        np.testing.assert_allclose(live.get_xlim(), window.axis[[0, -1]])
        assert live.get_ylim()[1] > 30_000
        window.cb_edges.setChecked(False)

        window._start_repeat_save(folder, 3, 1)
        window._save_next_spectrum()  # Same scan, must wait.
        assert len(list(folder.glob("*.dpt"))) == 1
        for _ in range(2):
            window.latest_t += 1_000_000_000
            window._save_next_spectrum()
        assert window._repeat_save is None
        records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
        assert len(records) == 3 and len({m["scan_time_local"] for m in records}) == 3
        assert [m["series"]["index"] for m in records] == [1, 2, 3]
        assert all(len(m["raw_counts"]) == 2068 and m["raw_counts"][0] == 30_000 for m in records)
    finally:
        window.close()


if __name__ == "__main__":
    app = Q.QApplication(sys.argv)
    ui.theme.apply(app)
    with tempfile.TemporaryDirectory(prefix="lk_view_") as temp:
        check_window(Path(temp))
    print("PASS: continuous sidebar positions, Fit X/Y, terminal-channel toggle, and fresh full-spectrum repeat saves")
