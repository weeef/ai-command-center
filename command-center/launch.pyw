"""Opens the command center with no console window (this is what the taskbar shortcut runs).

Starts server.py hidden if nothing answers on 127.0.0.1:8099, then opens Edge in app mode with its
own profile in this folder. Running it twice never starts a second server.
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8099/"
NOWIN = 0x08000000           # CREATE_NO_WINDOW
DETACHED = 0x00000008        # DETACHED_PROCESS, so the server outlives this launcher


def up():
    try:
        urllib.request.urlopen(URL + "api/ping", timeout=1.5).read()
        return True
    except Exception:
        return False


sys.path.insert(0, HERE)
import taskbar  # noqa: E402

# Already open? Bring it to the front instead of opening a second window.
existing = taskbar.find_windows()
if existing and up():
    taskbar.set_window_identity(existing[0])
    taskbar.focus(existing[0])
    sys.exit(0)

if not up():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, os.path.join(HERE, "server.py")],
                     cwd=HERE, creationflags=NOWIN | DETACHED, close_fds=True)
    for _ in range(40):
        if up():
            break
        time.sleep(0.25)

edge = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), "msedge.exe")
subprocess.Popen([edge, "--app=" + URL, "--user-data-dir=" + os.path.join(HERE, "edge-profile"),
                  "--disable-gpu", "--no-first-run", "--disable-sync",
                  "--disable-background-timer-throttling", "--window-size=1400,920"], creationflags=NOWIN)
taskbar.tag_when_ready()
