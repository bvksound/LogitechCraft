"""Which application is in the foreground?

* Windows: executable name of the foreground window's process ("chrome.exe")
* macOS:   bundle identifier of the frontmost app ("com.google.Chrome")
* Linux:   best effort via xdotool (process name)

Profiles match on either the identifier or the display name, so one profile
can cover the same app on both platforms.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AppInfo:
    ident: str  # exe name or bundle id
    name: str  # human readable
    pid: int = 0

    @property
    def is_self(self) -> bool:
        return self.pid == os.getpid()

    def keys(self) -> set[str]:
        out = {self.ident.lower(), self.name.lower()}
        if self.ident.lower().endswith(".exe"):
            out.add(self.ident.lower()[:-4])
        return {k for k in out if k}


# --------------------------------------------------------------------------- Windows

def _win_process_path(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _win_app_for_window(hwnd) -> Optional[AppInfo]:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    path = _win_process_path(pid.value)
    if not path:
        return None
    exe = os.path.basename(path)
    return AppInfo(exe, os.path.splitext(exe)[0], pid.value)


def _win_foreground() -> Optional[AppInfo]:
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return _win_app_for_window(hwnd) if hwnd else None


def _win_running() -> list[AppInfo]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    apps: dict[str, AppInfo] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            info = _win_app_for_window(hwnd)
            if info is not None:
                apps.setdefault(info.ident.lower(), info)
        return True

    user32.EnumWindows(callback, 0)
    return list(apps.values())


# --------------------------------------------------------------------------- macOS

def _mac_foreground() -> Optional[AppInfo]:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:
        return _mac_foreground_osascript()
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    return AppInfo(str(app.bundleIdentifier() or app.localizedName() or ""),
                   str(app.localizedName() or ""), int(app.processIdentifier()))


def _mac_foreground_osascript() -> Optional[AppInfo]:
    script = ('tell application "System Events" to set p to first application process '
              'whose frontmost is true\n'
              'return (bundle identifier of p) & "|" & (name of p) & "|" & (unix id of p)')
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        ident, name, pid = out.split("|")
        return AppInfo(ident, name, int(pid))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _mac_running() -> list[AppInfo]:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:
        return []
    out = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() == 0 and app.bundleIdentifier():  # regular apps only
            out.append(AppInfo(str(app.bundleIdentifier()), str(app.localizedName() or ""),
                               int(app.processIdentifier())))
    return out


# --------------------------------------------------------------------------- Linux

def _linux_foreground() -> Optional[AppInfo]:
    try:
        pid = int(subprocess.run(["xdotool", "getactivewindow", "getwindowpid"],
                                 capture_output=True, text=True, timeout=1).stdout.strip())
        with open(f"/proc/{pid}/comm") as fh:
            name = fh.read().strip()
        return AppInfo(name, name, pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- public

def get_foreground_app() -> Optional[AppInfo]:
    try:
        if sys.platform.startswith("win"):
            return _win_foreground()
        if sys.platform == "darwin":
            return _mac_foreground()
        return _linux_foreground()
    except Exception:  # noqa: BLE001 - never let app detection crash the UI
        return None


def list_running_apps() -> list[AppInfo]:
    try:
        if sys.platform.startswith("win"):
            apps = _win_running()
        elif sys.platform == "darwin":
            apps = _mac_running()
        else:
            apps = []
    except Exception:  # noqa: BLE001
        apps = []
    return sorted((a for a in apps if not a.is_self), key=lambda a: a.name.lower())
