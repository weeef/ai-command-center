"""Gives the command center's Edge window its own taskbar identity.

Edge app windows normally group under Microsoft Edge, so pinning one pins plain Edge. Setting the window's
AppUserModel properties (the same ID the Start-menu shortcut carries) makes Windows treat it as its own app:
its own taskbar button, our icon when pinned, and a pinned button that runs launch.pyw.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

APP_ID = "OllamaServer.CommandCenter"
TITLE = "command center"
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "command-center.ico")

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


def guid(s):
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("val", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


AUMID_FMT = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PKEY_ID, PKEY_RELAUNCH_CMD, PKEY_RELAUNCH_ICON, PKEY_RELAUNCH_NAME = 5, 2, 3, 4
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
VT_LPWSTR = 31


def relaunch_command():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{os.path.join(HERE, "launch.pyw")}"'


def set_window_identity(hwnd):
    ps = ctypes.c_void_p()
    iid = guid(IID_IPropertyStore)
    if shell32.SHGetPropertyStoreForWindow(wt.HWND(hwnd), ctypes.byref(iid), ctypes.byref(ps)) != 0 or not ps:
        return False
    vtbl = ctypes.cast(ctypes.cast(ps, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    SetValue = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtbl[6])
    Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    keep = []

    def put(pid, text):
        buf = ctypes.create_unicode_buffer(text)
        keep.append(buf)
        pv = PROPVARIANT(vt=VT_LPWSTR, val=ctypes.cast(buf, ctypes.c_void_p))
        return SetValue(ps, ctypes.byref(PROPERTYKEY(guid(AUMID_FMT), pid)), ctypes.byref(pv))

    ok = True
    # Relaunch details first, then the ID: Windows reads them together when the button is pinned.
    ok &= put(PKEY_RELAUNCH_CMD, relaunch_command()) == 0
    ok &= put(PKEY_RELAUNCH_NAME, "Command Center") == 0
    ok &= put(PKEY_RELAUNCH_ICON, ICON + ",0") == 0
    ok &= put(PKEY_ID, APP_ID) == 0
    Commit(ps)
    Release(ps)
    return ok


EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def find_windows():
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, b, n + 1)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if b.value == TITLE and cls.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def focus(hwnd):
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def tag_when_ready(timeout=20):
    """Wait for the window to appear, then tag it. Returns how many windows were tagged."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        wins = find_windows()
        if wins:
            time.sleep(0.4)  # let Edge finish setting up its own window properties first
            return sum(1 for w in find_windows() if set_window_identity(w))
        time.sleep(0.25)
    return 0


if __name__ == "__main__":
    print("tagged", sum(1 for w in find_windows() if set_window_identity(w)), "window(s)")
