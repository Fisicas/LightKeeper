"""A recorder's POST_RUN/AWAIT_UNCOVER snapshot cannot ask under the splash.

Uses the real window and timers, fake device discovery, isolated settings, and
captured commands. No spectrometer, recorder process, or real run is touched.
"""
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HOME = tempfile.mkdtemp(prefix="lk_prompt_")
os.environ.update(LIGHTKEEPER_HOME=HOME, LIGHTKEEPER_AUTOCONNECT="0", LIGHTKEEPER_NO_INSTALL="1",
                  LIGHTKEEPER_DATA_ROOT=str(Path(HOME) / "runs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as Q
from lightkeeper import splash, ui

app = Q.QApplication(sys.argv[:1])
ui.list_devices = lambda *args: ([], [])
ui.live_sessions = lambda: []
ui.open_runs = lambda: []
splash._load_libraries = lambda _: None


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


for state, event, command in (("POST_RUN", "need_post_dark", "post_dark"),
                               ("AWAIT_UNCOVER", "state", "continue")):
    _, w, sp = splash.start([sys.argv[0]])
    asked, sent = [], []

    def ask(*args):
        assert not sp.isVisible(), "Recorder question appeared under the splash"
        asked.append(args[0])
        return True

    w._ask = ask
    w._cmd = lambda *args: sent.append(args)
    w._apply_snapshot(dict(state=state))
    w._handle(dict(type=event, state=state))  # snapshot and live event may both request the same question
    pump(0.12)
    assert sp.isVisible() and not w._started
    assert not asked and not sent, (asked, sent)
    assert len(w._on_started) == 2, w._on_started  # resume callback plus one deduplicated recorder question
    pump(0.7)
    assert not sp.isVisible() and w._started
    assert len(asked) == 1 and sent == [(command,)], (asked, sent)
    print(f"PASS {state}: splash closed before one question and {command}", flush=True)
    # A stale deferred question must not send commands after the state changes.
    asked.clear(); sent.clear()
    w._started = False
    w._ask_post_dark() if state == "POST_RUN" else w._ask_uncover()
    w._set_state("DISCONNECTED")
    w.startup_finished()
    assert not asked and not sent
    w.close()
    app.processEvents()

print("PASS recorder startup prompts", flush=True)
