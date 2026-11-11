"""Local "command center" for the llama-server models in D:\\oLLama server.

Standard library only. Binds 127.0.0.1 only. Talks to the model server on
127.0.0.1:8090 (started by start-standard.bat / start-uncensored.bat) and adds
the API key itself, so the key never has to live in the browser.
Exits about 3 minutes after the last browser request unless a chat is streaming
or an import is running. Cline can talk to 8090 directly, or through
/cline/<pc|laptop>/v1 here (5:cline), which adds long-term memory.

Layout of this file: config + settings, system stats, model control (this PC), the
laptop (NVIDIA PAIR), then the HTTP handler: one guard for every request, a route
table, and the streaming proxies.

Environment overrides (used by the test suite, and for running a second copy):
  CC_PORT, CC_LLAMA_PORT, CC_PAIR_PORT, CC_EMBED_PORT   ports
  CC_STATE_DIR   folder for settings.json, tokens*.json, secrets.json and the default memory folder
  CC_KEYFILE     the API key file
  CC_PAIR_MEMBERS  PAIR's member list (point it at a missing file to ignore the laptop)
"""
import ctypes
import ctypes.wintypes as wt
import http.client
import http.server
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fsutil  # noqa: E402  (atomic file writes)
import workspaces  # noqa: E402  (2:code and 3:web)
import tokens  # noqa: E402  (tokens burned per machine)
import chats  # noqa: E402  (saved chat history)
import memory  # noqa: E402  (long-term memory shared by every model)
import cloud  # noqa: E402  (optional web search / stronger cloud model as tools)
import urllib.parse  # noqa: E402
import collections  # noqa: E402
import re  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("CC_PORT") or 8099)
STATE = os.environ.get("CC_STATE_DIR") or HERE          # settings.json and friends
DATA_ROOT = os.environ.get("CC_STATE_DIR") or ROOT      # the default memory folder lives here
LLAMA = ("127.0.0.1", int(os.environ.get("CC_LLAMA_PORT") or 8090))
KEYFILE = os.environ.get("CC_KEYFILE") or os.path.join(ROOT, "api-key.txt")
LLAMA_EXE = os.path.normcase(os.path.join(ROOT, "llama.cpp", "llama-server.exe"))
MAX_BODY = 64 * 1024 * 1024
IDLE_EXIT = 180
NOWIN = 0x08000000  # CREATE_NO_WINDOW, so pythonw never flashes consoles

# The browser only ever sends one of these keys. Paths come from here, never from the request.
MODELS = {
    "standard": {"bat": "start-standard.bat", "name": "Qwen3.6-35B standard Q4_K_M"},
    "uncensored": {"bat": "start-uncensored.bat", "name": "Qwen3.6-35B uncensored Q5_K_M"},
}

# The laptop's model, reached through NVIDIA PAIR's proxy on this PC (no API key there).
PAIR = ("127.0.0.1", int(os.environ.get("CC_PAIR_PORT") or 8080))
SETTINGS = os.path.join(STATE, "settings.json")
TARGETS = ("local", "laptop")


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


settings = load_settings()
target = {"v": settings.get("target") if settings.get("target") in TARGETS else "local"}


_settings_lock = threading.Lock()


def save_settings():
    """Atomic (temp file + rename) and serialised: settings.json holds the memory folder's location, so a crash
    or two threads saving at once must never leave it half-written."""
    with _settings_lock:
        try:
            fsutil.write_json(SETTINGS, settings, indent=1)
        except (OSError, RuntimeError):  # RuntimeError: another thread changed the dict mid-dump; the next save wins
            pass


def save_target(t):
    target["v"] = t
    settings["target"] = t
    save_settings()


lock = threading.Lock()
last_request = time.time()
active_streams = 0
launch = {"key": None, "t": 0.0}  # last model we launched, for LOADING before the port opens
switching = {"on": False, "msg": ""}


def touch():
    global last_request
    last_request = time.time()


def api_key():
    try:
        with open(KEYFILE, "r", encoding="utf-8-sig") as f:
            return f.readline().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- system stats

class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram():
    m = MEMSTAT()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"total": m.ullTotalPhys, "free": m.ullAvailPhys}


_cpu_prev = None


def cpu_percent():
    global _cpu_prev
    idle, kern, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    v = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    cur = (v(idle), v(kern), v(user))
    prev, _cpu_prev = _cpu_prev, cur
    if not prev:
        return 0.0
    di, dk, du = (c - p for c, p in zip(cur, prev))
    total = dk + du  # kernel time includes idle time
    return round(max(0.0, min(100.0, 100.0 * (1 - di / total))), 1) if total else 0.0


def gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,memory.free,"
             "utilization.gpu,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=NOWIN).stdout
        f = [x.strip() for x in out.splitlines()[0].split(",")]
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
        mib = 1024 * 1024
        return {"name": f[0].replace("NVIDIA GeForce ", ""), "used": int(f[1]) * mib,
                "total": int(f[2]) * mib, "free": int(f[3]) * mib, "util": num(f[4]),
                "temp": num(f[5]), "watts": num(f[6])}
    except Exception:
        return None


# ---------------------------------------------------------------- model server

def llama_get(path, timeout=1.5):
    c = http.client.HTTPConnection(*LLAMA, timeout=timeout)
    try:
        c.request("GET", path, headers={"Authorization": "Bearer " + api_key()})
        r = c.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body)
        except ValueError:
            return r.status, None
    finally:
        c.close()


def which_model(path):
    p = (path or "").lower()
    if not p:
        return None
    return "uncensored" if "uncensored" in p else "standard"


def model_info(gpu_util):
    info = {"state": "OFF", "key": None, "name": None, "file": None, "ctx": None,
            "vision": None, "engine": None, "id": None, "processing": False,
            "note": switching["msg"] if switching["on"] else ""}
    try:
        code, health = llama_get("/health")
    except OSError:
        if switching["on"] or time.time() - launch["t"] < 90:
            info.update(state="LOADING", key=launch["key"])
            info["name"] = MODELS.get(launch["key"] or "", {}).get("name")
        return info
    if code == 503:
        info.update(state="LOADING", key=launch["key"])
        info["name"] = MODELS.get(launch["key"] or "", {}).get("name")
        return info
    if code != 200:
        info["state"] = "LOADING"
        return info
    info["state"] = "ON"
    try:
        _, props = llama_get("/props")
        props = props or {}
        path = props.get("model_path", "")
        info["key"] = which_model(path)
        info["name"] = MODELS.get(info["key"] or "", {}).get("name")
        info["file"] = os.path.basename(path)
        info["ctx"] = (props.get("default_generation_settings") or {}).get("n_ctx")
        info["vision"] = bool((props.get("modalities") or {}).get("vision"))
        info["engine"] = "llama.cpp " + str(props.get("build_info", "")).split("-")[0]
        if props.get("is_sleeping"):
            info["state"] = "ASLEEP"
    except Exception:
        pass
    try:
        _, models = llama_get("/v1/models")
        info["id"] = ((models or {}).get("data") or [{}])[0].get("id")
    except Exception:
        pass
    try:
        _, slots = llama_get("/slots")
        info["processing"] = any(s.get("is_processing") for s in (slots or []) if isinstance(s, dict))
    except Exception:
        pass
    if switching["on"]:
        info["state"] = "LOADING"
    return info


def listener():
    """PID, exe, parent of whatever listens on the model port, via one PowerShell call (only used for 'off')."""
    ps = ("$c=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %d -State Listen -EA SilentlyContinue|Select -First 1;" % LLAMA[1] +
          "if(!$c){'{}';exit};$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\";"
          "$q=Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\";"
          "@{pid=$p.ProcessId;exe=$p.ExecutablePath;ppid=$p.ParentProcessId;pname=$q.Name;pcmd=$q.CommandLine}|ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                         text=True, timeout=20, creationflags=NOWIN).stdout.strip()
    try:
        return json.loads(out or "{}")
    except ValueError:
        return {}


def port_open():
    try:
        socket.create_connection(LLAMA, timeout=0.5).close()
        return True
    except OSError:
        return False


def stop_model():
    info = listener()
    if not info.get("pid"):
        return True, "already off"
    if os.path.normcase(info.get("exe") or "") != LLAMA_EXE:
        return False, "port %d is held by something else: %s" % (LLAMA[1], info.get("exe"))
    pcmd = (info.get("pcmd") or "").lower()
    # Close the model's own console window too when it came from one of our launchers.
    if (info.get("pname") or "").lower() == "cmd.exe" and ROOT.lower() in pcmd and ".bat" in pcmd:
        target = ["taskkill", "/PID", str(info["ppid"]), "/T", "/F"]
    else:
        target = ["taskkill", "/PID", str(info["pid"]), "/F"]
    subprocess.run(target, capture_output=True, timeout=15, creationflags=NOWIN)
    for _ in range(60):
        if not port_open():
            return True, "stopped"
        time.sleep(0.25)
    return False, "server did not stop"


def start_model(key):
    bat = os.path.join(ROOT, MODELS[key]["bat"])
    # Its own minimized console (blue or red), independent of this process.
    subprocess.Popen(["cmd", "/c", "start", "", "/min", bat], cwd=ROOT, creationflags=NOWIN)
    launch.update(key=key, t=time.time())


def model_action(action, key):
    try:
        if action == "off":
            switching.update(on=True, msg="stopping")
            ok, msg = stop_model()
            launch.update(key=None, t=0)
            return ok, msg
        if port_open():
            try:
                _, props = llama_get("/props")
                running = which_model((props or {}).get("model_path"))
            except Exception:
                running = None
            if running == key:
                return True, "already on"
            switching.update(on=True, msg="switching to " + key)
            ok, msg = stop_model()
            if not ok:
                return False, msg
        switching.update(on=True, msg="starting " + key)
        start_model(key)
        return True, "starting " + key
    finally:
        switching.update(on=False, msg="")


def pair_req(method, path, obj=None, timeout=2.0):
    c = http.client.HTTPConnection(*PAIR, timeout=timeout)
    try:
        body = json.dumps(obj).encode() if obj is not None else None
        c.request(method, path, body, {"Content-Type": "application/json"} if body else {})
        r = c.getresponse()
        d = r.read()
        try:
            return r.status, json.loads(d)
        except ValueError:
            return r.status, d.decode("utf-8", "replace")[:300]
    finally:
        c.close()


PAIR_MEMBERS = os.environ.get("CC_PAIR_MEMBERS") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Nvidia Corporation", "Personal AI Router", "cluster", "members.json")
NODE_INFO_PORT = 14318  # PAIR's nvpair-node-info, read-only telemetry on each paired machine


def laptop_address():
    """The paired machine that isn't this one, from PAIR's member list (address can change with DHCP)."""
    try:
        with open(PAIR_MEMBERS, encoding="utf-8") as f:
            members = json.load(f)
        me = socket.gethostname().lower()
        for m in members:
            ip = m.get("ipAddress") or ""
            if ip and not ip.startswith("127.") and (m.get("name") or "").lower() != me:
                return ip, m.get("name") or ip
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def laptop_machine():
    """GPU / RAM / CPU of the laptop in the same shape as this PC's, or None if it can't be read."""
    ip, name = laptop_address()
    if not ip:
        return None
    c = http.client.HTTPConnection(ip, NODE_INFO_PORT, timeout=1.5)
    try:
        c.request("GET", "/v1/node-info")
        r = c.getresponse()
        j = json.loads(r.read()) if r.status == 200 else None
    except (OSError, ValueError):
        return None
    finally:
        c.close()
    if not j:
        return None
    gpus = j.get("GPUs") or []
    g = next((x for x in gpus if "nvidia" in (x.get("name") or "").lower()), gpus[0] if gpus else None)
    gpu_out = None
    if g and g.get("vram_bytes"):
        total, used = g.get("vram_bytes", 0), g.get("vram_used_bytes", 0)
        gpu_out = {"name": (g.get("name") or "").replace("NVIDIA GeForce ", ""), "used": used, "total": total,
                   "free": max(0, total - used), "util": g.get("utilization_percent"), "temp": None, "watts": None}
    mem = j.get("memory") or {}
    return {"name": name, "host": name, "gpu": gpu_out,
            "ram": {"total": mem.get("total_bytes", 0), "free": max(0, mem.get("total_bytes", 0) - mem.get("used_bytes", 0))},
            "cpu": (j.get("cpu") or {}).get("utilization_percent", 0) or 0, "cpu_name": (j.get("cpu") or {}).get("name")}


laptop_cache = {"machine": None, "t": 0.0, "resolved": None}


def resolve_laptop(name):
    """What the laptop's name resolves to right now (mDNS/DNS), IPv4 only; None if it doesn't."""
    if not name:
        return None
    for host in (name, name + ".local"):
        try:
            return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        except (OSError, IndexError):
            continue
    return None


def laptop_reach_note():
    ip, name = laptop_address()
    if not ip:
        return "PAIR has no paired laptop on record"
    if laptop_cache["machine"] is not None:
        return f"{name} answers at {ip} but its model server doesn't"
    now = laptop_cache.get("resolved")
    if now and now != ip:
        return (f"{name} isn't answering at {ip}, where PAIR expects it; its name now points to {now}, "
                f"so its address changed: re-pair it in the PAIR app or reserve its IP in the router")
    return (f"{name} isn't answering at {ip}. If PAIR is open on the laptop, its firewall is likely blocking: "
            f"set the laptop's Wi-Fi to a Private network (Settings > Network > Wi-Fi > network profile)")


def laptop_watch():
    """Check the laptop every few seconds in the background, so stats never wait on a sleeping laptop."""
    while True:
        m = laptop_machine()
        laptop_cache.update(machine=m, t=time.time())
        if m is None:   # only look the name up when something is wrong (it can take a moment)
            laptop_cache["resolved"] = resolve_laptop(laptop_address()[1] or "")
        time.sleep(3)


PAIR_STATES = {"loaded": "ON", "loading": "LOADING", "sleeping": "ASLEEP", "unloaded": "OFF"}


def laptop_info(timeout=2.0):
    info = {"state": "OFF", "key": "laptop", "name": None, "file": None, "ctx": None, "vision": None,
            "engine": "PAIR → laptop", "id": None, "processing": False, "note": ""}
    try:
        _, j = pair_req("GET", "/models", timeout=timeout)
    except ConnectionRefusedError:
        info["note"] = "PAIR isn't running on this PC (nothing on 127.0.0.1:8080)"
        return info
    except OSError:
        # PAIR's proxy is up but slow: it is waiting on a laptop that doesn't answer
        info["note"] = "PAIR is running, but " + laptop_reach_note()
        return info
    data = j.get("data") if isinstance(j, dict) else None
    if not data:
        info["note"] = "PAIR lists no laptop model: " + laptop_reach_note()
        return info
    # Prefer a model that's already up; otherwise the first one PAIR lists.
    m = next((x for x in data if (x.get("status") or {}).get("value") in ("loaded", "sleeping", "loading")), data[0])
    st = (m.get("status") or {}).get("value", "")
    args = (m.get("status") or {}).get("args") or []
    ctx = (m.get("meta") or {}).get("n_ctx")
    if not ctx and "--ctx-size" in args:
        try:
            ctx = int(args[args.index("--ctx-size") + 1])
        except (ValueError, IndexError):
            pass
    info.update(state=PAIR_STATES.get(st, "OFF"), id=m.get("id"), name=(m.get("id") or "").split("/")[-1],
                file=m.get("id"), ctx=ctx,
                vision="image" in ((m.get("architecture") or {}).get("input_modalities") or []))
    if info["state"] == "ON":  # only when loaded: asking a sleeping model could wake it
        try:
            from urllib.parse import quote
            code, slots = pair_req("GET", "/slots?model=" + quote(info["id"], safe=""))
            if code == 400:
                # PAIR quirk (seen 2026-11-05): while the laptop is generating, /slots for a model it lists
                # as loaded answers 400 "model not found". Idle, it answers 200 with the slot list.
                info["processing"] = True
            else:
                info["processing"] = any(s.get("is_processing") for s in (slots if isinstance(slots, list) else []) if isinstance(s, dict))
        except OSError:
            pass
    if switching["on"]:
        info["state"], info["note"] = "LOADING", switching["msg"]
    return info


def laptop_action(action):
    info = laptop_info(timeout=12)   # a click can wait; the 1 s stats poll can't
    if not info["id"]:
        return False, info["note"] or "laptop model not found"
    if action == "on" and info["state"] in ("ON", "ASLEEP"):
        return True, "already on" + (" (asleep, wakes on the first message)" if info["state"] == "ASLEEP" else "")
    if action == "off" and info["state"] == "OFF":
        return True, "already off"
    switching.update(on=True, msg=("waking " if action == "on" else "unloading ") + "laptop model")
    try:
        code, j = pair_req("POST", "/models/load" if action == "on" else "/models/unload", {"model": info["id"]}, timeout=30)
    except OSError as e:
        return False, f"PAIR did not answer ({e.__class__.__name__})"
    finally:
        switching.update(on=False, msg="")
    if code >= 400:
        msg = j.get("error") if isinstance(j, dict) else j
        if isinstance(msg, dict):
            msg = msg.get("message")
        return False, f"PAIR refused ({code}): {msg}"
    return True, ("loading" if action == "on" else "unloading") + " laptop model"


_stats = {"t": 0, "v": None}
_BUILD_FILES = ("index.html", "server.py", "workspaces.py", "tokens.py", "chats.py", "memory.py", "importers.py", "cloud.py")


def build_stamp():
    """Newest modification time of the app's own files. The page reloads itself when this changes, so a window
    left open (the taskbar pin only refocuses it) never keeps showing an old version."""
    files = [os.path.join(HERE, f) for f in _BUILD_FILES]
    try:
        files += [e.path for e in os.scandir(STATIC)]
    except OSError:
        pass
    t = 0
    for f in files:
        try:
            t = max(t, os.path.getmtime(f))
        except OSError:
            pass
    return int(t)


STATIC = os.path.join(HERE, "static")   # the UI's stylesheet and scripts
token_watch = None


def stats():
    with lock:
        if time.time() - _stats["t"] < 0.7 and _stats["v"]:
            return _stats["v"]
    # Both machines every time, so the usage pane can show either one whatever the target is.
    g = gpu()
    pc = {"label": "this pc", "name": socket.gethostname(), "ok": True, "gpu": g, "ram": ram(), "cpu": cpu_percent()}
    lm = laptop_cache["machine"]
    lap = {"label": "laptop", "name": lm["name"] if lm else (laptop_address()[1] or "laptop"), "ok": lm is not None,
           "gpu": lm and lm["gpu"], "ram": lm["ram"] if lm else {"total": 0, "free": 0}, "cpu": lm["cpu"] if lm else 0}
    if target["v"] == "laptop":
        m = laptop_info()
        # Busy comes from our own chats and the laptop's slot status; this PC's GPU says nothing about the laptop.
        busy = active_streams > 0 or m["processing"]
        machine = lap
    else:
        m = model_info(g and g.get("util"))
        busy = active_streams > 0 or m["processing"] or (
            m["state"] == "ON" and g is not None and (g.get("util") or 0) >= 25)
        machine = pc
    v = {"host": socket.gethostname(), "user": os.environ.get("USERNAME", "user"), "target": target["v"],
         "machine": machine["label"], "machine_name": machine["name"], "machine_ok": machine["ok"],
         "gpu": machine["gpu"], "ram": machine["ram"], "cpu": machine["cpu"], "model": m, "busy": busy,
         "machines": {"pc": pc, "laptop": lap},
         "laptop_online": laptop_cache["machine"] is not None,
         "laptop_note": None if laptop_cache["machine"] is not None else laptop_reach_note(),
         "laptop_name": (laptop_cache["machine"] or {}).get("name") or laptop_address()[1],
         "tokens": token_watch.view() if token_watch else None,
         "ui_streams": active_streams, "models": {k: v["name"] for k, v in MODELS.items()},
         "build": build_stamp(), "time": time.time()}
    with lock:
        _stats.update(t=time.time(), v=v)
    return v


# ---------------------------------------------------------------- http
#
# Security model, enforced in Handler.guard() and Handler.dispatch() for every request:
#   - the server only listens on 127.0.0.1;
#   - Host must be 127.0.0.1/localhost (the PC window) or this PC's tailnet name with Tailscale's
#     user header (the phone, through `tailscale serve`); anything else is refused, which stops DNS rebinding;
#   - a browser request from any other site (Origin / Sec-Fetch-Site) is refused, which stops CSRF;
#   - a response is never sent while request bytes are still unread on a kept-alive connection (see dispatch);
#   - routes are PC-only unless they are marked remote=True;
#   - the API key and the cloud keys never go to the browser.

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
ERROR_LOG = os.path.join(STATE, "server-errors.log")

ROUTES = {}     # (method, path) -> (function, remote_ok, raw_body)
PATTERNS = []   # (method, compiled regex, function, remote_ok, raw_body), for paths with a variable part


def route(method, path, remote=False, raw=False):
    """Register a handler. remote=True also allows it from the phone (default: PC only, so a new route is never
    exposed by accident). raw=True hands the handler the unread request body (uploads)."""
    def deco(fn):
        if isinstance(path, str):
            ROUTES[(method, path)] = (fn, remote, raw)
        else:
            PATTERNS.append((method, path, fn, remote, raw))
        return fn
    return deco


def mask_key(k):
    return ("•" * 8 + k[-4:]) if k else ""


def copy_to_clipboard(text):
    """Put text on the Windows clipboard from here, so a secret can be copied without the page ever holding it."""
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError:
        data = b"\xff\xfe" + text.encode("utf-16-le")
    try:
        return subprocess.run(["clip"], input=data, timeout=5, creationflags=NOWIN).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "command-center"
    sys_version = ""
    timeout = 120   # an idle or half-open connection gives its thread back after two minutes

    def log_message(self, *a):
        pass

    # ---- responses
    def send_body(self, code, body, ctype, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        if not any(k.lower() == "cache-control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.responded = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_body(code, json.dumps(obj).encode(), "application/json")

    def stream_headers(self, status, ctype, mem_info):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        if mem_info:
            self.send_header("X-Memory", json.dumps(mem_info))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.responded = True

    # ---- the gate every request passes
    def guard(self):
        """Local window: Host is 127.0.0.1/localhost. Phone: Host is this PC's tailnet name, which only reaches us
        through `tailscale serve` (it binds nothing public); serve stamps Tailscale-User-Login on requests from
        signed-in devices, and that login must be this PC's own Tailscale user (or one listed in settings.json ->
        "tailscale_allow"), so a device someone else shared into the tailnet gets nothing."""
        host = self.headers.get("Host", "").lower()
        self.remote = False
        if host not in ALLOWED_HOSTS:
            dns, owner = workspaces.tailnet_identity()
            login = (self.headers.get("Tailscale-User-Login") or "").strip().lower()
            allowed = {owner} | {str(x).lower() for x in settings.get("tailscale_allow") or []} if owner else None
            if not dns or host.split(":")[0] != dns or not login or (allowed is not None and login not in allowed):
                self.send_json(403, {"error": "bad host"})
                return False
            self.remote = True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS and not (self.remote and origin.lower() == "https://" + host.split(":")[0]):
            self.send_json(403, {"error": "bad origin"})
            return False
        # Browsers label where a request comes from. Another site (or another local port) may only navigate to
        # the page itself; it may not call the API or load anything else, even with a "simple" request that
        # carries no Origin header.
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            page = self.command == "GET" and self.path.split("?", 1)[0] in ("/", "/index.html")
            if not (page and self.headers.get("Sec-Fetch-Mode") == "navigate"):
                self.send_json(403, {"error": "cross-site request"})
                return False
        if not self.remote:
            touch()  # only the PC window keeps the app alive; the phone works while it's open
        return True

    def dispatch(self):
        """Every request comes through here: guard, route lookup, PC-only check, body, handler."""
        self.responded = False
        keep = not self.close_connection
        has_body = (self.headers.get("Content-Length") or "0").strip() != "0" or "Transfer-Encoding" in self.headers
        # Until the body has been read in full this connection must not be reused: whatever is left unread would
        # be parsed as the next request, with headers of the sender's choosing (no Origin, a local Host). That
        # would let any web page smuggle an API call past the checks above inside the body of a refused one.
        if has_body:
            self.close_connection = True
        try:
            if not self.guard():
                return
            if "Transfer-Encoding" in self.headers:
                return self.send_json(400, {"error": "Transfer-Encoding is not supported; send Content-Length"})
            path = urllib.parse.urlsplit(self.path).path
            hit, args = ROUTES.get((self.command, path)), ()
            if not hit:
                for method, rx, fn, remote_ok, raw in PATTERNS:
                    m = method == self.command and rx.fullmatch(path)
                    if m:
                        hit, args = (fn, remote_ok, raw), m.groups()
                        break
            if not hit:
                known = self.command in ("GET", "POST")
                return self.send_json(404 if known else 405, {"error": "not found" if known else "method not allowed"})
            fn, remote_ok, raw = hit
            if self.remote and not remote_ok:
                return self.send_json(403, {"error": "only available on the PC"})
            if self.command == "GET":
                if has_body:
                    return self.send_json(400, {"error": "GET with a body"})
                return fn(self, *args)
            if raw:
                return fn(self, keep, *args)
            data = self.read_json(keep)
            if data is not None:
                return fn(self, data, *args)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            import traceback
            fsutil.append_log(ERROR_LOG, "%s %s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.command,
                                                             self.path.split("?", 1)[0], traceback.format_exc()))
            if not self.responded:
                try:
                    self.send_json(500, {"error": "internal error (details in command-center\\server-errors.log)"})
                except OSError:
                    pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = dispatch

    def read_json(self, keep):
        """The request body as a JSON object, or None after answering with the error."""
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send_json(415, {"error": "JSON only"})
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send_json(411, {"error": "Content-Length required"})
        if n < 0 or n > MAX_BODY:
            return self.send_json(413, {"error": "body too large (64 MB max)"})
        body = self.rfile.read(n)
        if len(body) < n:
            raise ConnectionError("request body cut short")
        self.close_connection = not keep   # fully read: the connection is safe to reuse
        try:
            data = json.loads(body)
        except ValueError:
            return self.send_json(400, {"error": "invalid JSON"})
        if not isinstance(data, dict):
            return self.send_json(400, {"error": "expected a JSON object"})
        return data

    def query(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return lambda k, d="": (qs.get(k) or [d])[0]

    def client_gone(self):
        """True once the browser has closed this connection (nothing else arrives on it while we stream)."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True

    # ---- the page and its files
    @route("GET", "/", remote=True)
    @route("GET", "/index.html", remote=True)
    def page(self):
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            body = f.read()
        self.send_body(200, body, "text/html; charset=utf-8", [
            ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", CSP)])

    @route("GET", re.compile(r"/static/([a-z0-9][a-z0-9.-]*\.(?:js|css))"), remote=True)
    def static_file(self, name):
        """The UI's own scripts and stylesheet. The name can only be letters, digits, dots and dashes, so it
        can't leave the folder. no-cache + ETag: always current after an update, one small 304 otherwise."""
        try:
            p = os.path.join(STATIC, name)
            st = os.stat(p)
            etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.responded = True
                return
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json(404, {"error": "not found"})
        self.send_body(200, body, STATIC_TYPES[os.path.splitext(name)[1]], [("ETag", etag), ("Cache-Control", "no-cache")])

    @route("GET", re.compile(r"/fonts/([a-z0-9-]+\.woff2)"), remote=True)
    def font(self, name):
        """The bundled Inter / IBM Plex Mono files (SIL OFL), so the app looks the same offline and on the phone."""
        try:
            with open(os.path.join(HERE, "fonts", name), "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json(404, {"error": "not found"})
        self.send_body(200, body, "font/woff2", [("Cache-Control", "max-age=604800")])

    # ---- status
    @route("GET", "/api/ping", remote=True)
    def ping(self):
        self.send_json(200, {"ok": True, "app": "command-center"})

    @route("GET", "/api/stats", remote=True)
    def get_stats(self):
        self.send_json(200, dict(stats(), remote=self.remote, memory=memory.M.brief()))

    @route("GET", "/api/tokens/log", remote=True)
    def tokens_log(self):
        self.send_json(200, {"log": token_watch.recent_log(30) if token_watch else []})

    # ---- model control
    @route("POST", "/api/target", remote=True)
    def set_target(self, data):
        t = data.get("target")
        if t not in TARGETS:
            return self.send_json(400, {"error": "target must be local or laptop"})
        save_target(t)
        _stats["t"] = 0
        self.send_json(200, {"ok": True, "target": t})

    @route("POST", "/api/model", remote=True)
    def set_model(self, data):
        action = data.get("action")
        key = data.get("model", "standard")
        if action not in ("on", "off") or key not in MODELS:
            return self.send_json(400, {"error": "action must be on/off, model standard/uncensored"})
        if switching["on"]:
            return self.send_json(409, {"error": "busy: " + switching["msg"]})
        ok, msg = laptop_action(action) if target["v"] == "laptop" else model_action(action, key)
        _stats["t"] = 0
        self.send_json(200 if ok else 500, {"ok": ok, "msg": msg})

    @route("POST", "/api/key/copy")
    def key_copy(self, data):
        """Copy the model server's API key to the clipboard on the PC. The page only ever sees a masked key."""
        k = api_key()
        if not k:
            return self.send_json(404, {"ok": False, "msg": "no api-key.txt (run setup.bat)"})
        ok = copy_to_clipboard(k)
        self.send_json(200 if ok else 500, {"ok": ok, "msg": "copied" if ok else "couldn't reach the clipboard"})

    # ---- saved chats
    @route("GET", "/api/chats", remote=True)
    def chats_list(self):
        self.send_json(200, {"chats": chats.list_chats()})

    @route("GET", re.compile(r"/api/chats/([^/]+)"), remote=True)
    def chats_get(self, cid):
        c = chats.get_chat(cid)   # the id must match chats.ID_RE, so it can't name any other file
        self.send_json(200 if c else 404, c or {"error": "no such chat"})

    @route("POST", "/api/chats/save", remote=True)
    def chats_save(self, data):
        ok, msg = chats.save_chat(data)
        self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})

    @route("POST", "/api/chats/delete", remote=True)
    def chats_delete(self, data):
        ok, msg = chats.delete_chat(data.get("id"))
        self.send_json(200 if ok else 404, {"ok": ok, "msg": msg})

    @route("POST", "/api/chats/prune", remote=True)
    def chats_prune(self, data):
        self.send_json(200, {"ok": True, "deleted": chats.prune(data.get("days", 30), data.get("keep") or "")})

    @route("POST", "/api/chats/rename", remote=True)
    def chats_rename(self, data):
        ok, msg = chats.rename_chat(data.get("id"), data.get("title"))
        self.send_json(200 if ok else 404, {"ok": ok, "msg": msg})

    # ---- 2:code, 3:web, the cline pane
    @route("GET", "/api/code")
    def code_get(self):
        self.send_json(200, workspaces.code_info(settings, model_info(None), laptop_info(), mask_key(api_key())))

    @route("POST", "/api/code/open")
    def code_open(self, data):
        lid = laptop_info()["id"] if data.get("which") == "laptop" else None
        ok, msg = workspaces.open_code(data.get("which"), data.get("path") or "", settings, save_settings, lid)
        self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})

    @route("POST", "/api/code/both")
    def code_both(self, data):
        ok, msg = workspaces.open_both(data.get("path") or "", settings, save_settings)
        self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})

    @route("POST", "/api/code/forget")
    def code_forget(self, data):
        ok, msg = workspaces.forget_recent(str(data.get("path", "")), settings, save_settings)
        self.send_json(200, {"ok": ok, "msg": msg})

    @route("GET", "/api/web", remote=True)
    def web_get(self):
        w = dict(workspaces.web_info(mask_key(api_key()), model_info(None)))
        if self.remote:
            w["api_key"] = ""
        self.send_json(200, dict(w, remote=self.remote))

    @route("POST", "/api/web/serve")
    def web_serve(self, data):
        if data.get("action") not in ("on", "off"):
            return self.send_json(400, {"error": "action must be on or off"})
        ok, msg = workspaces.set_serve(data["action"] == "on")
        self.send_json(200 if ok else 500, {"ok": ok, "msg": msg})

    @route("GET", "/api/cline")
    def cline_get(self):
        if target["v"] == "laptop":
            self.send_json(200, {"provider": "OpenAI Compatible", "target": "laptop",
                                 "base_url": workspaces.cline_url("laptop", workspaces.ROUTE["laptop"]), "api_key": "any"})
        else:
            self.send_json(200, {"provider": "OpenAI Compatible", "target": "local",
                                 "base_url": workspaces.cline_url("pc", workspaces.ROUTE["pc"]), "api_key": mask_key(api_key())})

    # ---- 5:cline: Cline routed through here
    @route("GET", "/api/clinecc")
    def clinecc_get(self):
        self.send_json(200, {"route": workspaces.cline_state(), "memory": settings.get("cline_memory", True),
                             "learn": settings.get("cline_learn", True), "log": list(cline_log)[::-1],
                             "memory_on": memory.M.on and memory.M.ok()})

    @route("POST", "/api/clinecc/route")
    def clinecc_route(self, data):
        ok, msg = workspaces.cline_route(data.get("which"), bool(data.get("through")))
        if ok:
            settings.setdefault("cline_route", {})[data["which"]] = bool(data.get("through"))
            save_settings()
        self.send_json(200 if ok else 409, {"ok": ok, "msg": msg})

    @route("POST", "/api/clinecc/settings")
    def clinecc_settings(self, data):
        for k in ("memory", "learn"):
            if k in data:
                settings["cline_" + k] = bool(data[k])
        save_settings()
        self.send_json(200, {"ok": True})

    @route("GET", re.compile(r"/cline/(pc|laptop)/v1/models"))
    def cline_models(self, which):
        host, auth = (PAIR, {}) if which == "laptop" else (LLAMA, {"Authorization": "Bearer " + api_key()})
        c = http.client.HTTPConnection(*host, timeout=5)
        try:
            c.request("GET", "/v1/models", headers=auth)
            r = c.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException):
            return self.send_json(502, {"error": {"message": "model server not reachable"}})
        finally:
            c.close()
        self.send_body(r.status, body, "application/json")

    @route("POST", re.compile(r"/cline/(pc|laptop)/v1/chat/completions"))
    def cline_chat(self, data, which):
        """Cline -> (memory) -> the model that VS Code is set up for: "pc" = this PC's llama-server, "laptop" = PAIR.
        Memory is recalled once per task and put in the task message, so every step of the task sends the same
        prompt prefix and llama-server's prompt cache keeps working."""
        info, task, text = {}, "", None
        if settings.get("cline_memory", True) and isinstance(data.get("messages"), list):
            try:
                text, info, task = memory.M.recall_task(data["messages"])
            except Exception:
                text = None
            if text:
                data["messages"] = memory.inject(data["messages"], text, "first")
        else:
            task = memory.cline_task(data.get("messages") or [])
        entry = {"t": time.time(), "which": which, "task": task[:200], "mem": len(info.get("ids", [])),
                 "chunks": info.get("chunks", 0), "status": "…", "steps": 1}
        # one row per task: later steps of the same task update it instead of adding rows
        with lock:
            prev = next((e for e in reversed(cline_log) if e["task"] == entry["task"] and e["which"] == which
                         and time.time() - e["t"] < 3600), None)
            if prev:
                prev.update(t=time.time(), status="…", steps=prev.get("steps", 1) + 1)
                entry = prev
            else:
                cline_log.append(entry)
        if which == "laptop":
            mid = laptop_info()["id"]
            if not mid:
                entry["status"] = "laptop offline"
                return self.send_json(502, {"error": {"message": "no laptop model listed by PAIR. Is the laptop on?"}})
            data["model"] = mid
            return self.proxy_chat(json.dumps(data).encode(), PAIR, {}, info, entry)
        return self.proxy_chat(json.dumps(data).encode(), LLAMA, {"Authorization": "Bearer " + api_key()}, info, entry)

    # ---- memory
    @route("GET", "/api/memory", remote=True)
    def memory_status(self):
        self.send_json(200, memory.M.status())

    @route("GET", "/api/memory/list", remote=True)
    def memory_list(self):
        one = self.query()
        if not memory.M.ok():
            return self.send_json(200, {"memories": [], "error": memory.M.status().get("error")})
        try:
            lim, off = max(1, min(500, int(one("limit", "200")))), max(0, int(one("offset", "0")))
        except ValueError:
            lim, off = 200, 0
        if one("ids"):
            ids = [int(x) for x in one("ids").split(",")[:50] if x.strip().isdigit()]
            return self.send_json(200, {"memories": [m for m in map(memory.M.store.get, ids) if m]})
        self.send_json(200, {"memories": memory.M.store.list(one("q"), one("topic"), lim, off, one("archived") == "1")})

    @route("GET", "/api/memory/tree", remote=True)
    def memory_tree(self):
        self.send_json(200, memory.M.tree())

    @route("GET", "/api/memory/synapses", remote=True)
    def memory_synapses(self):
        self.send_json(200, {"synapses": memory.M.synapses()})

    @route("GET", "/api/memory/related", remote=True)
    def memory_related(self):
        try:
            mid = int(self.query()("id", "0"))
        except ValueError:
            mid = 0
        self.send_json(200, {"related": memory.M.related(mid)})

    @route("GET", "/api/memory/graph", remote=True)
    def memory_graph(self):
        self.send_json(200, {"nodes": memory.M.graph()})

    @route("GET", "/api/memory/export")
    def memory_export(self):
        body = json.dumps(memory.M.export(), ensure_ascii=False, indent=1).encode()
        self.send_body(200, body, "application/json", [
            ("Content-Disposition", 'attachment; filename="memories-%s.json"' % time.strftime("%Y-%m-%d"))])

    @route("POST", "/api/memory/location")
    def memory_location(self, data):
        ok, msg = memory.M.move(data.get("path"), data.get("mode"))
        self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})

    @route("POST", "/api/memory/settings", remote=True)
    def memory_settings(self, data):
        memory.M.set_flags(on=data.get("on"), learn=data.get("learn"))
        self.send_json(200, {"ok": True})

    def memory_ready(self):
        if memory.M.ok():
            return True
        self.send_json(409, {"ok": False, "msg": memory.M.status().get("error") or "memory isn't available"})
        return False

    @route("POST", "/api/memory/add", remote=True)
    def memory_add(self, data):
        if not self.memory_ready():
            return
        new = memory.M.add_manual(data.get("text"), data.get("topic") or "other", bool(data.get("pinned")))
        self.send_json(200 if new else 400, {"ok": bool(new), "id": new})

    @route("POST", "/api/memory/update", remote=True)
    def memory_update(self, data):
        if not self.memory_ready():
            return
        mid = data.get("id")
        ok = isinstance(mid, int) and memory.M.store.update(mid, data.get("text"), data.get("topic"), data.get("pinned"),
                                                            data.get("archived"))
        self.send_json(200 if ok else 404, {"ok": bool(ok)})

    @route("POST", "/api/memory/delete", remote=True)
    def memory_delete(self, data):
        if not self.memory_ready():
            return
        mid = data.get("id")
        ok = isinstance(mid, int) and memory.M.store.delete(mid)
        self.send_json(200 if ok else 404, {"ok": bool(ok)})

    @route("POST", "/api/memory/import")
    def memory_import(self, data):
        if not self.memory_ready():
            return
        self.send_json(200, {"ok": True, "msg": "imported %d memories" % memory.M.import_(data)})

    @route("POST", "/api/memory/paste", remote=True)
    def memory_paste(self, data):
        if not self.memory_ready():
            return
        n = memory.M.paste_memories(data.get("text"))
        self.send_json(200, {"ok": bool(n), "msg": "added %d memories%s" % (
            n, ", tidying them up in the background" if n and memory.M.learn_on else "")})

    @route("POST", "/api/memory/import-file", raw=True)
    def memory_import_file(self, keep):
        """A ChatGPT/Claude export (.zip or .json, can be GBs) streamed to a temp file in the memory folder,
        then imported in the background. Everything that can refuse the upload is checked before reading it."""
        global active_streams
        M = memory.M
        if not M.ok():
            return self.send_json(409, {"ok": False, "msg": "memory isn't available"})
        if M.job.get("running"):
            return self.send_json(409, {"ok": False, "msg": "an import is already running"})
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send_json(411, {"ok": False, "msg": "Content-Length required"})
        if n <= 0 or n > 16 * 1024 ** 3:
            return self.send_json(413, {"ok": False, "msg": "file too large"})
        try:
            if shutil.disk_usage(M.home).free < n + 200e6:
                return self.send_json(507, {"ok": False, "msg": "not enough free space in the memory folder for this file"})
        except OSError:
            pass
        learn = self.query()("learn", "1") != "0"
        tmp = os.path.join(M.home, "import-%d.tmp" % int(time.time() * 1000))
        with lock:
            active_streams += 1
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    b = self.rfile.read(min(left, 1 << 20))
                    if not b:
                        raise OSError("upload cut short")
                    f.write(b)
                    left -= len(b)
            self.close_connection = not keep
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self.send_json(500, {"ok": False, "msg": "upload failed: %s" % e})
        finally:
            with lock:
                active_streams -= 1
        ok, msg = M.import_file(tmp, learn, cleanup=True)
        if not ok:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.send_json(200 if ok else 409, {"ok": ok, "msg": msg})

    # ---- cloud tools (web search / ask a stronger model): settings
    @route("GET", "/api/cloud")
    def cloud_get(self):
        self.send_json(200, cloud.status(settings))

    @route("POST", "/api/cloud/key")
    def cloud_key(self, data):
        ok = cloud.set_key(data.get("provider"), data.get("key"))
        self.send_json(200 if ok else 400, {"ok": ok, "msg": "saved" if ok else "unknown provider"})

    @route("POST", "/api/cloud/test")
    def cloud_test(self, data):
        if data.get("provider") not in cloud.PROVIDERS:
            return self.send_json(400, {"ok": False, "msg": "unknown provider"})
        ok, msg, ids = cloud.list_models(data["provider"])
        self.send_json(200, {"ok": ok, "msg": msg, "models": ids})

    @route("POST", "/api/cloud/settings")
    def cloud_settings(self, data):
        c = settings.setdefault("cloud", {})
        for k in ("enabled", "web_search", "ask_cloud"):
            if k in data:
                c[k] = bool(data[k])
        if data.get("provider") in cloud.PROVIDERS:
            c["provider"] = data["provider"]
        m = data.get("model")
        if isinstance(m, dict) and m.get("provider") in cloud.PROVIDERS and isinstance(m.get("id"), str):
            mid = m["id"].strip()[:100]
            c.setdefault("models", {})[m["provider"]] = mid or cloud.DEFAULT_MODELS[m["provider"]]
        save_settings()
        self.send_json(200, {"ok": True})

    # ---- chat
    @route("POST", "/v1/chat/completions", remote=True)
    def chat(self, data):
        # long-term memory: the same recalled memories go to whichever model answers
        chat_id = data.pop("chat_id", None)
        use_mem = not data.pop("no_memory", False)
        mem_info = {}
        if use_mem and isinstance(data.get("messages"), list) and data["messages"]:
            cid = chat_id if isinstance(chat_id, str) else None
            try:
                sys_txt, mem_info = memory.M.recall(data["messages"], cid)
            except Exception:
                sys_txt = None
            try:
                data["messages"] = memory.M.chat_inject(data["messages"], cid, sys_txt)
            except Exception:
                pass   # memory must never be the reason a chat fails
        if target["v"] == "laptop":
            mid = laptop_info()["id"]
            if not mid:
                return self.send_json(502, {"error": "no laptop model listed by PAIR. Is the laptop on?"})
            data["model"] = mid  # the model id always comes from PAIR's listing, never the browser
            host, auth = PAIR, {}
        else:
            host, auth = LLAMA, {"Authorization": "Bearer " + api_key()}
        tools = cloud.tools_for(settings) if data.get("stream") and not data.get("tools") else None
        if tools:
            return self.tool_chat(data, host, auth, mem_info, tools)
        return self.proxy_chat(json.dumps(data).encode(), host, auth, mem_info)

    def _raw(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))

    def _sse(self, obj):
        self._raw(b"data: " + json.dumps(obj).encode() + b"\n\n")

    def tool_chat(self, data, upstream, auth, mem_info, tools):
        """Chat where the local model may call web_search / ask_cloud. Its reply streams to the browser as usual;
        tool-call deltas are held back, the tools run here (cloud.py), the browser gets {"cc_tool": ...} progress
        events, and the model continues with the results.

        Limits, because every call costs the owner money: at most cloud.MAX_CALLS tool calls per reply (the model
        is told how many are left and loses the tools when they are used up), a repeated identical call is
        answered from the first one, and nothing is run once the browser has gone away."""
        global active_streams
        msgs = list(data["messages"])
        # The model doesn't know today's date and distrusts "future" dates in fresh results without this.
        # Same text all day, so the prompt cache still covers the conversation.
        note = ("Today is %s. You have tools: web_search looks things up on the live web (more current than your "
                "training data, so trust it for recent events, prices and releases) and ask_cloud asks a stronger AI. "
                "Use them only when they help: answer from your own knowledge when you can, and one search is "
                "usually enough. When you use search results, mention the source briefly."
                % time.strftime("%A %Y-%m-%d"))
        if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
            msgs[0] = dict(msgs[0], content=note + "\n\n" + msgs[0]["content"])
        else:
            msgs.insert(0, {"role": "system", "content": note})
        started, up = False, None
        left, done = cloud.MAX_CALLS, {}   # calls left for this reply; (tool, input) -> result text already given
        with lock:
            active_streams += 1
        try:
            for rnd in range(cloud.MAX_CALLS + 2):
                body = dict(data, messages=msgs, stream=True)
                if left > 0:
                    body["tools"] = tools
                up = http.client.HTTPConnection(*upstream, timeout=900)
                try:
                    up.request("POST", "/v1/chat/completions", json.dumps(body),
                               {"Content-Type": "application/json", "Accept": "text/event-stream", **auth})
                    resp = up.getresponse()
                except (OSError, http.client.HTTPException) as e:
                    if not started:
                        return self.send_json(502, {"error": f"model server not reachable on {upstream[1]} ({e.__class__.__name__}). Is it on?"})
                    self._sse({"error": {"message": "the model server went away"}})
                    break
                if resp.status != 200:
                    err = resp.read()
                    if not started:
                        return self.send_body(resp.status, err, "application/json")
                    self._sse({"error": {"message": err.decode("utf-8", "replace")[:300]}})
                    break
                if not started:
                    self.stream_headers(200, "text/event-stream", mem_info)
                    started = True
                calls, content, buf = {}, "", b""
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == b"[DONE]":
                            continue
                        try:
                            j = json.loads(payload)
                        except ValueError:
                            continue
                        ch = (j.get("choices") or [{}])[0] if isinstance(j, dict) else {}
                        d = ch.get("delta") or {}
                        if d.get("tool_calls"):
                            for tc in d["tool_calls"]:
                                if not isinstance(tc, dict):
                                    continue
                                c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                c["id"] = tc.get("id") or c["id"]
                                f = tc.get("function") or {}
                                c["name"] += f.get("name") or ""
                                c["args"] += f.get("arguments") or ""
                            continue
                        content += d.get("content") or ""
                        self._raw(b"data: " + payload + b"\n\n")
                up.close()
                if not calls:
                    break
                tcs = [{"id": c["id"] or "call_%d_%d" % (rnd, i), "type": "function",
                        "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for i, c in sorted(calls.items())]
                msgs.append({"role": "assistant", "content": content, "tool_calls": tcs})
                provider = cloud.NAMES[cloud.config(settings)["provider"]]
                for t in tcs:
                    name, args = t["function"]["name"], cloud.parse_args(t["function"]["name"], t["function"]["arguments"])
                    key = (name, cloud.norm_input(name, args))
                    if self.client_gone():
                        raise ConnectionError("browser closed")   # stop before spending on a reply nobody reads
                    if key in done:
                        out = "You already ran this exact call in this reply. Its result:\n" + done[key]
                    elif left <= 0:
                        out = "Not run: the limit of %d tool calls per reply is used up. Answer now with what you have." % cloud.MAX_CALLS
                        self._sse({"cc_tool": {"id": t["id"], "name": name, "args": args, "status": "error",
                                               "error": "not run (limit of %d per reply)" % cloud.MAX_CALLS, "sources": []}})
                    else:
                        left -= 1
                        self._sse({"cc_tool": {"id": t["id"], "name": name, "args": args, "status": "running", "provider": provider}})
                        try:
                            res = cloud.run(settings, name, args)
                        except Exception as e:
                            res = {"ok": False, "error": "tool crashed: %s" % e.__class__.__name__, "sources": []}
                        self._sse({"cc_tool": {"id": t["id"], "name": name, "args": args, "status": "done" if res["ok"] else "error",
                                               "error": res.get("error"), "sources": res.get("sources", []),
                                               "provider": res.get("provider"), "cached": bool(res.get("cached"))}})
                        if res["ok"]:
                            out = res["text"] or "(no answer)"
                            if res.get("sources"):
                                out += "\n\nSources:\n" + "\n".join("- %s: %s" % (s["title"], s["url"]) for s in res["sources"])
                            done[key] = out
                            out += ("\n\n(%d tool call%s left for this reply. Answer now unless something essential is still missing.)"
                                    % (left, "" if left == 1 else "s")) if left else "\n\n(No tool calls left for this reply: answer now.)"
                        else:
                            out = "The tool failed: %s. Answer without it and say so briefly." % res.get("error")
                    msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
            self._raw(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True
        finally:
            if up:
                up.close()
            with lock:
                active_streams -= 1
            touch()

    def proxy_chat(self, body, upstream, auth, mem_info=None, entry=None):
        """Forward a chat request to a model server and stream the answer back unchanged. Only our own headers go
        upstream (plus the API key), never the browser's."""
        global active_streams
        up = http.client.HTTPConnection(*upstream, timeout=900)
        try:
            up.request("POST", "/v1/chat/completions", body, {
                "Content-Type": "application/json", "Accept": "text/event-stream", **auth})
            resp = up.getresponse()
        except (OSError, http.client.HTTPException) as e:
            up.close()
            if entry is not None:
                entry["status"] = "model off"
            return self.send_json(502, {"error": f"model server not reachable on {upstream[1]} ({e.__class__.__name__}). Is it on?"})
        t0, tail = time.time(), b""
        with lock:
            active_streams += 1
        done = threading.Event()

        def watch_browser():
            # When the browser goes away (stop button, closed tab), drop the upstream connection too, so the model
            # stops generating instead of finishing a reply nobody reads.
            while not done.wait(0.5):
                if self.client_gone():
                    try:
                        up.sock and up.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        try:
            self.stream_headers(resp.status, resp.getheader("Content-Type", "text/event-stream"), mem_info)
            threading.Thread(target=watch_browser, daemon=True).start()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if entry is not None:
                    tail = (tail + chunk)[-16384:]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            self.close_connection = True   # the browser sees the cut-off stream as an error, which it is
        finally:
            done.set()
            up.close()
            if entry is not None:
                entry.update(status=resp.status, secs=round(time.time() - t0, 1), **stream_stats(tail))
            with lock:
                active_streams -= 1
            touch()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy share the port
    request_queue_size = 64

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass  # a dropped connection is routine; real handler errors are logged in Handler.dispatch


cline_log = collections.deque(maxlen=80)


def stream_stats(tail):
    """Token counts from the last events of a streamed reply (llama.cpp sends timings; others send usage)."""
    out = {}
    for line in tail.split(b"\n")[::-1]:
        line = line.strip()
        if not line.startswith(b"data:") or line.endswith(b"[DONE]"):
            continue
        try:
            j = json.loads(line[5:])
        except ValueError:
            continue
        if not isinstance(j, dict):
            continue
        t, u = j.get("timings") or {}, j.get("usage") or {}
        if t:
            out.update(prompt=t.get("prompt_n", 0) + t.get("cache_n", 0), cached=t.get("cache_n", 0), out=t.get("predicted_n", 0))
            break
        if u and "prompt" not in out:
            out.update(prompt=u.get("prompt_tokens", 0), out=u.get("completion_tokens", 0))
    return out


def llm_json(messages, schema, max_tokens):
    """One short, non-streaming request to whichever model "run on" points at, for memory housekeeping.
    Returns the parsed JSON object, or None if no model is available (memory retries later)."""
    req = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.2, "stream": False,
           "chat_template_kwargs": {"enable_thinking": False},
           "response_format": {"type": "json_schema", "json_schema": {"name": "memories", "schema": schema}}}
    if target["v"] == "laptop":
        mid = laptop_info()["id"]
        if not mid:
            return None
        req["model"], host, auth = mid, PAIR, {}
    else:
        if model_info(None)["state"] not in ("ON", "ASLEEP"):
            return None
        host, auth = LLAMA, {"Authorization": "Bearer " + api_key()}
    for attempt in (0, 1):
        c = http.client.HTTPConnection(*host, timeout=240)
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(req), {"Content-Type": "application/json", **auth})
            r = c.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException):
            return None
        finally:
            c.close()
        if r.status == 400 and attempt == 0:
            req.pop("response_format", None)  # an engine without structured output: ask plainly and parse
            continue
        if r.status != 200:
            return None
        try:
            text = json.loads(body)["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None
        if "</think>" in text:
            text = text.split("</think>", 1)[1]
        a, b = text.find("{"), text.rfind("}")
        try:
            out = json.loads(text[a:b + 1]) if a >= 0 else None
        except ValueError:
            out = None
        return out if isinstance(out, dict) else {"memories": []}
    return None


def idle_watch():
    """Exit a few minutes after the window closes. Not while something is streaming, and not in the middle of an
    import or a move of the memory folder, which would be cut off half-done."""
    while True:
        time.sleep(5)
        busy = active_streams > 0 or memory.M.job.get("running") or memory.M.moving
        if busy:
            touch()
        elif time.time() - last_request > IDLE_EXIT:
            memory.M.shutdown()  # stops the embedding server too
            os._exit(0)


def main():
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(0)  # already running
    cpu_percent()
    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=laptop_watch, daemon=True).start()
    global token_watch
    for w in ("pc", "laptop"):
        workspaces.ROUTE[w] = bool((settings.get("cline_route") or {}).get(w))
    chats.ON_SAVE, chats.ON_DELETE = (lambda c: memory.M.note_chat(c)), (lambda cid: memory.M.forget_chat(cid))
    memory.M.start(settings, save_settings, DATA_ROOT, os.environ.get("CC_LLAMA_EXE") or os.path.join(ROOT, "llama.cpp", "llama-server.exe"),
                   llm_json, lambda: active_streams > 0 or switching["on"], chats,
                   models_root=ROOT, embed_port=int(os.environ.get("CC_EMBED_PORT") or memory.EMBED_PORT))
    token_watch = tokens.TokenWatch(LLAMA, PAIR, api_key)
    threading.Thread(target=token_watch.run, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        memory.M.shutdown()


if __name__ == "__main__":
    main()
