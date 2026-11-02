"""Window side of the window <-> background-recorder connection (multiprocessing.connection over 127.0.0.1 + authkey).

The recorder answers ('hello', version) with a snapshot dict (type 'snapshot'), then streams the engine's events.
The window sends ('cmd', name, args) for the run commands the recorder accepts (continue, stop, post_dark, finish,
interrupt). Losing the connection never affects the recording; the window just re-attaches later.
"""
from __future__ import annotations

import queue
import threading
from multiprocessing.connection import Client

from . import VERSION

RUN_COMMANDS = ("continue", "stop", "post_dark", "finish", "interrupt")


class RecorderLink:
    def __init__(self, session: dict, timeout: float = 8.0):
        self.session = session
        self.serial = session.get("serial", "")
        self.pid = session.get("pid")
        self.evt: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self.closed = False
        self.conn = Client(("127.0.0.1", int(session["port"])), authkey=bytes.fromhex(session["authkey"]))
        self.conn.send(("hello", VERSION))
        if not self.conn.poll(timeout):
            self.conn.close(); raise TimeoutError("recorder did not answer")
        snap = self.conn.recv()
        if not isinstance(snap, dict) or snap.get("type") != "snapshot":
            self.conn.close(); raise RuntimeError("unexpected reply from recorder")
        self.evt.put(snap)
        threading.Thread(target=self._rx, name="recorder-link", daemon=True).start()

    def _rx(self):
        try:
            while not self.closed:
                self.evt.put(self.conn.recv())
        except (EOFError, OSError, ConnectionError):
            pass
        except Exception as e:  # pragma: no cover
            self.evt.put(dict(type="error", text=f"recorder link: {e!r}"))
        finally:
            if not self.closed:
                self.evt.put(dict(type="detached"))

    def send(self, name: str, *args):
        if name not in RUN_COMMANDS:
            raise ValueError(f"not a recorder command: {name}")
        with self._lock:
            self.conn.send(("cmd", name, args))

    def close(self):
        self.closed = True
        try:
            self.conn.close()
        except Exception:
            pass
