"""Remember local Windows paths across changes in volume mount points."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path, PureWindowsPath


@lru_cache(maxsize=1)
def _volume_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for fn in (api.GetVolumePathNameW, api.GetVolumeNameForVolumeMountPointW):
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.BOOL
    api.GetVolumePathNamesForVolumeNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                                    ctypes.POINTER(wintypes.DWORD)]
    api.GetVolumePathNamesForVolumeNameW.restype = wintypes.BOOL
    api.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                          ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    api.GetVolumeInformationW.restype = wintypes.BOOL
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    api.GetLogicalDrives.argtypes = []
    api.GetLogicalDrives.restype = wintypes.DWORD
    api.GetThreadErrorMode.argtypes = []
    api.GetThreadErrorMode.restype = wintypes.DWORD
    api.SetThreadErrorMode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.SetThreadErrorMode.restype = wintypes.BOOL
    return api


def _volume_serial(root: str) -> str:
    api = _volume_api()
    if api.GetDriveTypeW(root) not in (2, 3, 6):  # Removable, fixed and RAM disks.
        return ""
    serial = wintypes.DWORD()
    previous = wintypes.DWORD()
    # An empty removable drive should return an error instead of opening a Windows dialog.
    changed = api.SetThreadErrorMode(api.GetThreadErrorMode() | 0x0001, ctypes.byref(previous))
    try:
        found = api.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0)
        return f"{serial.value:08x}" if found and serial.value else ""
    finally:
        if changed:
            api.SetThreadErrorMode(previous.value, None)


def _volume_location(path: Path) -> tuple[str, str, PureWindowsPath] | None:
    """(Volume GUID, filesystem serial, relative path), following junctions and mounted folders."""
    if os.name != "nt":
        return None
    try:
        canonical = path.resolve()
    except (OSError, RuntimeError):
        return None
    api = _volume_api()
    root = ctypes.create_unicode_buffer(len(str(canonical)) + 6)
    guid = ctypes.create_unicode_buffer(50)
    if not api.GetVolumePathNameW(str(canonical), root, len(root)):
        return None
    api.GetVolumeNameForVolumeMountPointW(root.value, guid, len(guid))
    serial = _volume_serial(root.value)
    if not (guid.value or serial):
        return None  # Network shares and some virtual filesystems have no local volume ID.
    try:
        relative = canonical.relative_to(root.value)
    except ValueError:
        return None
    return guid.value.casefold(), serial, PureWindowsPath(relative)


def _volume_mounts(guid: str) -> list[str]:
    if os.name != "nt" or not guid:
        return []
    paths = _volume_api().GetVolumePathNamesForVolumeNameW
    size = wintypes.DWORD()
    paths(guid, None, 0, ctypes.byref(size))
    if not size.value:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if not paths(guid, buf, len(buf), ctypes.byref(size)):
        return []
    return [s for s in buf[:].split("\0") if s]


def _serial_mounts(serial: str) -> list[str]:
    """Fallback for systems that deny volume GUID lookup, using lettered Windows drives only."""
    if os.name != "nt" or not serial:
        return []
    mask = _volume_api().GetLogicalDrives()
    roots = (f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i))
    return [root for root in roots if _volume_serial(root) == serial]


def remember_path(path: str | Path) -> dict[str, str]:
    """Store the last known path and, for local Windows storage, its volume-relative identity."""
    if not path:
        return {"path": ""}
    absolute = Path(os.path.abspath(path))
    record = {"path": str(absolute)}
    location = _volume_location(absolute)
    if location:
        guid, serial, relative = location
        record.update(volume_guid=guid, volume_serial=serial, relative_path=str(relative))
    return record


def resolve_path(record: dict | str | Path | None) -> Path | None:
    """Find the saved location. A known volume that is unavailable never falls back to its old letter."""
    if not record:
        return None
    if not isinstance(record, dict):
        return Path(record)  # Pre-identity settings and registry records.
    if record.get("unverified_legacy_path"):
        return None
    original = record.get("path")
    guid = record.get("volume_guid") or ""
    serial = record.get("volume_serial") or ""
    if not (guid or serial):
        return Path(original) if original else None
    if not isinstance(record.get("relative_path"), str):
        return None
    relative = PureWindowsPath(record["relative_path"])
    if relative.anchor or ".." in relative.parts:
        return None

    def match_kind(path: Path) -> str:
        location = _volume_location(path)
        if location is None or location[2] != relative:
            return ""
        current_guid, current_serial, _ = location
        if guid and current_guid:
            return "guid" if current_guid == guid and (not serial or not current_serial or current_serial == serial) else ""
        return "serial" if serial and current_serial == serial else ""

    candidates = [Path(original)] if original else []
    candidates.extend(Path(root).joinpath(*relative.parts) for root in _volume_mounts(guid))
    serial_matches = {}
    for path in candidates:
        match = match_kind(path)
        if match == "guid":
            return path
        if match == "serial":
            serial_matches[os.path.normcase(str(path))] = path
    for root in _serial_mounts(serial):
        path = Path(root).joinpath(*relative.parts)
        match = match_kind(path)
        if match == "guid":
            return path
        if match == "serial":
            serial_matches[os.path.normcase(str(path))] = path
    return next(iter(serial_matches.values())) if len(serial_matches) == 1 else None


def location_key(record: dict | str | Path) -> str:
    """A comparison key for saved locations, independent of the current mount path."""
    if isinstance(record, dict):
        identity = record.get("volume_guid") or record.get("volume_serial")
        if identity:
            return identity + "|" + record.get("relative_path", "").casefold()
    return os.path.normcase(os.path.abspath(record.get("path", "") if isinstance(record, dict) else record))
