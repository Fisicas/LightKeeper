"""One UI per settings home; background recorders do not acquire this lock.

The lock elects the owner atomically (QLocalServer alone does not do that on
Windows). The local socket carries only an activation request, never run commands.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtNetwork, QtWidgets as Q


def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsIconic.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    return u


def _foreground(hwnd):
    if sys.platform == "win32":
        u = _user32()
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE; do not change a maximized window
        u.SetForegroundWindow(hwnd)


def activate_legacy_window() -> bool:
    """Allow the first updated launch to focus an already-running pre-guard UI.

    Only the default profile can be identified by its old window title. Isolated
    LIGHTKEEPER_HOME profiles must never interfere with another profile's UI.
    """
    if sys.platform != "win32" or os.environ.get("LIGHTKEEPER_HOME"):
        return False
    import ctypes
    from ctypes import wintypes
    u = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    @callback_type
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, title, len(title))
        if re.fullmatch(r"LightKeeper \d+\.\d+\.\d+(?:[+\w.-]*)", title.value):
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid():
                found.append(hwnd)
        return True

    u.EnumWindows(visit, 0)
    if found:
        _foreground(found[0])
    return bool(found)


class WindowInstance(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        base = os.environ.get("LIGHTKEEPER_HOME")
        home = Path(base) if base else Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LightKeeper"
        home.mkdir(parents=True, exist_ok=True)
        identity = os.path.normcase(str(home.resolve()))
        self.name = "LightKeeper-UI-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.lock = QtCore.QLockFile(str(home / "window.lock"))
        self.lock.setStaleLockTime(0)  # a long recording must never age out a live UI lock
        self.server = QtNetwork.QLocalServer(self)
        self.server.setSocketOptions(QtNetwork.QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.window = None
        self.pending = False
        self.clients = set()

    def acquire(self) -> bool:
        """True for the owner; otherwise request activation and exit before startup."""
        # Retry election if the old UI exits while the activation request is sent.
        deadline = time.monotonic() + 1.5
        while True:
            if self.lock.tryLock(0):
                QtNetwork.QLocalServer.removeServer(self.name)  # safe: we own the lock
                if not self.server.listen(self.name):
                    self.lock.unlock()
                    raise RuntimeError(f"Could not listen for LightKeeper launches: {self.server.errorString()}")
                return True
            if self.lock.error() != QtCore.QLockFile.LockError.LockFailedError:
                raise RuntimeError(f"Could not lock LightKeeper window: {self.lock.error()}")
            if sys.platform == "win32":
                pid, _, _ = self.lock.getLockInfo()
                if pid > 0:
                    _user32().AllowSetForegroundWindow(pid)
            sock = QtNetwork.QLocalSocket()
            sock.connectToServer(self.name)
            if sock.waitForConnected(100):
                sock.write(b"activate\n")
                sent = sock.bytesToWrite() == 0 or sock.waitForBytesWritten(200)
                sock.disconnectFromServer()
                if sent:
                    return False
            sock.abort()
            if time.monotonic() >= deadline:
                # The existing UI may be busy. Never start a competing UI or put a
                # modal notification over an acquisition just because focus failed.
                print("LightKeeper is already starting or running; no second window was opened.", file=sys.stderr)
                return False
            time.sleep(0.05)

    def _accept(self):
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self.clients.add(sock)
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))
            QtCore.QTimer.singleShot(2000, sock, sock.abort)
            self._read(sock)

    def _drop(self, sock):
        self._read(sock)
        self.clients.discard(sock)
        sock.deleteLater()

    def _read(self, sock):
        if sock.canReadLine():
            if bytes(sock.readLine(64)) == b"activate\n":
                self.pending = True
                self._activate()

    def ready(self, window):
        """Called after the splash closes, before any recorder questions open."""
        self.window = window
        if self.pending:
            self._activate()

    def _activate(self):
        if self.window is None:
            return  # activation during startup is remembered until ready()
        self.pending = False
        w = self.window
        w.setWindowState(w.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
        w.show()
        target = Q.QApplication.activeModalWidget() or w
        target.raise_()
        target.activateWindow()
        _foreground(int(target.winId()))

    def close(self):
        for sock in list(self.clients):
            sock.abort()
        self.server.close()
        self.window = None
        self.lock.unlock()
