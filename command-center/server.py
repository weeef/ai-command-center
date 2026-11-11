"""Local "command center" for the llama-server models in D:\\oLLama server.

Standard library only. Binds 127.0.0.1 only. Talks to the model server on
127.0.0.1:8090 (started by start-standard.bat / start-uncensored.bat) and adds
the API key itself, so the key never has to live in the browser for chat.
Exits about 3 minutes after the last browser request unless a chat is streaming.
Cline keeps talking to 8090 directly; nothing here sits in front of it.
"""
import ctypes
import ctypes.wintypes as wt
import http.client
import http.server
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workspaces  # noqa: E402  (2:code and 3:web)
import tokens  # noqa: E402  (tokens burned per machine)
import chats  # noqa: E402  (saved chat history)
import memory  # noqa: E402  (long-term memory shared by every model)
import urllib.parse  # noqa: E402

PORT = 8099
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LLAMA = ("127.0.0.1", 8090)
KEYFILE = os.path.join(ROOT, "api-key.txt")
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
PAIR = ("127.0.0.1", 8080)
SETTINGS = os.path.join(HERE, "settings.json")
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


def save_settings():
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=1)
    except OSError:
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
    """PID, exe, parent of whatever listens on 8090, via one PowerShell call (only used for 'off')."""
    ps = ("$c=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8090 -State Listen -EA SilentlyContinue|Select -First 1;"
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
        return False, "port 8090 is held by something else: " + str(info.get("exe"))
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


PAIR_MEMBERS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Nvidia Corporation", "Personal AI Router",
                            "cluster", "members.json")
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
_BUILD_FILES = ("index.html", "server.py", "workspaces.py", "tokens.py", "chats.py", "memory.py", "importers.py")


def build_stamp():
    """Newest modification time of the app's own files. The page reloads itself when this changes, so a window
    left open (the taskbar pin only refocuses it) never keeps showing an old version."""
    t = 0
    for f in _BUILD_FILES:
        try:
            t = max(t, os.path.getmtime(os.path.join(HERE, f)))
        except OSError:
            pass
    return int(t)
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

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "command-center"

    def log_message(self, *a):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def guard(self):
        """Local window: Host is 127.0.0.1/localhost. Phone: Host is this PC's tailnet name, which only reaches us
        through `tailscale serve` (it binds nothing public); serve stamps Tailscale-User-Login on requests from
        the owner's signed-in devices, so anything without it is refused."""
        host = self.headers.get("Host", "").lower()
        self.remote = False
        if host not in ALLOWED_HOSTS:
            dns = workspaces.tailnet_dns()
            if not dns or host.split(":")[0] != dns or not self.headers.get("Tailscale-User-Login"):
                self.send_json(403, {"error": "bad host"})
                return False
            self.remote = True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS and not (self.remote and origin.lower() == "https://" + host.split(":")[0]):
            self.send_json(403, {"error": "bad origin"})
            return False
        if not self.remote:
            touch()  # only the PC window keeps the app alive; the phone works while it's open
        return True

    # From the phone: chat, saved chats, model on/off, run-on target and stats. Not VS Code, not the API key,
    # and not phone access itself (turning that off from the phone would lock the phone out).
    REMOTE_BLOCKED = ("/api/code", "/api/cline", "/api/web/serve", "/api/memory/location", "/api/memory/import",
                      "/api/memory/import-file")

    def remote_blocked(self, path):
        if self.remote and any(path == p or path.startswith(p + "/") for p in self.REMOTE_BLOCKED):
            self.send_json(403, {"error": "only available on the PC"})
            return True
        return False

    def do_GET(self):
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        if self.remote_blocked(path):
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/ping":
            self.send_json(200, {"ok": True, "app": "command-center"})
        elif path == "/api/stats":
            self.send_json(200, dict(stats(), remote=self.remote, memory=memory.M.brief()))
        elif path == "/api/memory":
            self.send_json(200, memory.M.status())
        elif path == "/api/memory/list":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            one = lambda k, d="": (qs.get(k) or [d])[0]
            if not memory.M.ok():
                return self.send_json(200, {"memories": [], "error": memory.M.status().get("error")})
            try:
                lim, off = max(1, min(500, int(one("limit", "200")))), max(0, int(one("offset", "0")))
            except ValueError:
                lim, off = 200, 0
            if one("ids"):
                ids = [int(x) for x in one("ids").split(",")[:50] if x.strip().isdigit()]
                return self.send_json(200, {"memories": [m for m in map(memory.M.store.get, ids) if m]})
            self.send_json(200, {"memories": memory.M.store.list(one("q"), one("topic"), lim, off)})
        elif path == "/api/memory/graph":
            self.send_json(200, {"nodes": memory.M.graph()})
        elif path == "/api/memory/export":
            body = json.dumps(memory.M.export(), ensure_ascii=False, indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="memories-%s.json"' % time.strftime("%Y-%m-%d"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/tokens/log":
            return self.send_json(200, {"log": token_watch.recent_log(30) if token_watch else []})
        elif path == "/api/chats":
            self.send_json(200, {"chats": chats.list_chats()})
        elif path.startswith("/api/chats/"):
            c = chats.get_chat(path[len("/api/chats/"):])
            self.send_json(200 if c else 404, c or {"error": "no such chat"})
        elif path == "/api/code":
            self.send_json(200, workspaces.code_info(settings, model_info(None), laptop_info(), api_key()))
        elif path == "/api/web":
            w = dict(workspaces.web_info(api_key(), model_info(None)))
            if self.remote:
                w["api_key"] = ""
            self.send_json(200, dict(w, remote=self.remote))
        elif path == "/api/cline":
            if target["v"] == "laptop":
                self.send_json(200, {"provider": "OpenAI Compatible", "target": "laptop",
                                     "base_url": f"http://{PAIR[0]}:{PAIR[1]}/v1", "api_key": "any"})
            else:
                self.send_json(200, {"provider": "OpenAI Compatible", "target": "local",
                                     "base_url": f"http://{LLAMA[0]}:{LLAMA[1]}/v1", "api_key": api_key()})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.guard():
            return
        if self.path.split("?", 1)[0] == "/api/memory/import-file":
            return self.import_upload()
        if not (self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"):
            return self.send_json(415, {"error": "JSON only"})
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send_json(411, {"error": "Content-Length required"})
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            return self.send_json(413, {"error": "body too large (64 MB max)"})
        body = self.rfile.read(n)
        try:
            data = json.loads(body)
        except ValueError:
            return self.send_json(400, {"error": "invalid JSON"})
        path = self.path.split("?", 1)[0]
        if self.remote_blocked(path):
            return
        if not isinstance(data, dict):
            return self.send_json(400, {"error": "expected a JSON object"})
        if path.startswith("/api/memory/"):
            return self.memory_post(path[len("/api/memory/"):], data)
        if path == "/api/chats/save":
            ok, msg = chats.save_chat(data)
            return self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})
        if path == "/api/chats/delete":
            ok, msg = chats.delete_chat(data.get("id"))
            return self.send_json(200 if ok else 404, {"ok": ok, "msg": msg})
        if path == "/api/chats/prune":
            return self.send_json(200, {"ok": True, "deleted": chats.prune(data.get("days", 30), data.get("keep") or "")})
        if path == "/api/chats/rename":
            ok, msg = chats.rename_chat(data.get("id"), data.get("title"))
            return self.send_json(200 if ok else 404, {"ok": ok, "msg": msg})
        if path == "/api/code/open":
            lid = laptop_info()["id"] if data.get("which") == "laptop" else None
            ok, msg = workspaces.open_code(data.get("which"), data.get("path") or "", settings, save_settings, lid)
            return self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})
        if path == "/api/code/both":
            ok, msg = workspaces.open_both(data.get("path") or "", settings, save_settings)
            return self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})
        if path == "/api/code/forget":
            ok, msg = workspaces.forget_recent(str(data.get("path", "")), settings, save_settings)
            return self.send_json(200, {"ok": ok, "msg": msg})
        if path == "/api/web/serve":
            if data.get("action") not in ("on", "off"):
                return self.send_json(400, {"error": "action must be on or off"})
            ok, msg = workspaces.set_serve(data["action"] == "on")
            return self.send_json(200 if ok else 500, {"ok": ok, "msg": msg})
        if path == "/api/target":
            t = data.get("target")
            if t not in TARGETS:
                return self.send_json(400, {"error": "target must be local or laptop"})
            save_target(t)
            _stats["t"] = 0
            return self.send_json(200, {"ok": True, "target": t})
        if path == "/api/model":
            action = data.get("action")
            key = data.get("model", "standard")
            if action not in ("on", "off") or key not in MODELS:
                return self.send_json(400, {"error": "action must be on/off, model standard/uncensored"})
            if switching["on"]:
                return self.send_json(409, {"error": "busy: " + switching["msg"]})
            ok, msg = laptop_action(action) if target["v"] == "laptop" else model_action(action, key)
            _stats["t"] = 0
            return self.send_json(200 if ok else 500, {"ok": ok, "msg": msg})
        if path == "/v1/chat/completions":
            # long-term memory: the same recalled memories go to whichever model answers
            chat_id = data.pop("chat_id", None)
            use_mem = not data.pop("no_memory", False)
            mem_info = {}
            if use_mem and isinstance(data.get("messages"), list) and data["messages"]:
                try:
                    sys_txt, mem_info = memory.M.recall(data["messages"], chat_id if isinstance(chat_id, str) else None)
                except Exception:
                    sys_txt = None
                if sys_txt:
                    first = data["messages"][0]
                    if isinstance(first, dict) and first.get("role") == "system" and isinstance(first.get("content"), str):
                        data["messages"] = [dict(first, content=sys_txt + "\n\n" + first["content"])] + data["messages"][1:]
                    else:
                        data["messages"] = [{"role": "system", "content": sys_txt}] + data["messages"]
            if target["v"] == "laptop":
                mid = laptop_info()["id"]
                if not mid:
                    return self.send_json(502, {"error": "no laptop model listed by PAIR. Is the laptop on?"})
                data["model"] = mid  # the model id always comes from PAIR's listing, never the browser
                return self.proxy_chat(json.dumps(data).encode(), PAIR, {}, mem_info)
            return self.proxy_chat(json.dumps(data).encode(), LLAMA, {"Authorization": "Bearer " + api_key()}, mem_info)
        self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        self.guard() and self.send_json(405, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_PUT

    def import_upload(self):
        """A ChatGPT/Claude export (.zip or .json, can be GBs) streamed to a temp file in the memory folder,
        then imported in the background."""
        global active_streams
        if self.remote_blocked("/api/memory/import-file"):
            return
        M = memory.M
        if not M.ok():
            self.close_connection = True
            return self.send_json(409, {"ok": False, "msg": "memory isn't available"})
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self.send_json(411, {"ok": False, "msg": "Content-Length required"})
        if n <= 0 or n > 16 * 1024 ** 3:
            self.close_connection = True
            return self.send_json(413, {"ok": False, "msg": "file too large"})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        learn = (qs.get("learn") or ["1"])[0] != "0"
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
        except OSError as e:
            self.close_connection = True
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self.send_json(500, {"ok": False, "msg": "upload failed: %s" % e})
        finally:
            with lock:
                active_streams -= 1
        ok, msg = M.import_file(tmp, learn, cleanup=True)
        return self.send_json(200 if ok else 409, {"ok": ok, "msg": msg})

    def memory_post(self, what, data):
        M = memory.M
        if what == "location":
            ok, msg = M.move(data.get("path"), data.get("mode"))
            return self.send_json(200 if ok else 400, {"ok": ok, "msg": msg})
        if what == "settings":
            M.set_flags(on=data.get("on"), learn=data.get("learn"))
            return self.send_json(200, {"ok": True})
        if not M.ok():
            return self.send_json(409, {"ok": False, "msg": M.status().get("error") or "memory isn't available"})
        mid = data.get("id")
        if what == "add":
            new = M.add_checked(data.get("text"), data.get("topic") or "other", "manual", bool(data.get("pinned")))
            if new:
                M.seq += 1
                M.last_learned = [{"op": "add", "id": new, "text": " ".join(str(data.get("text")).split()),
                                   "topic": data.get("topic") or "other", "seq": M.seq, "manual": True}]
            return self.send_json(200 if new else 400, {"ok": bool(new), "id": new})
        if what == "update":
            ok = isinstance(mid, int) and M.store.update(mid, data.get("text"), data.get("topic"), data.get("pinned"))
            return self.send_json(200 if ok else 404, {"ok": bool(ok)})
        if what == "delete":
            ok = isinstance(mid, int) and M.store.delete(mid)
            return self.send_json(200 if ok else 404, {"ok": bool(ok)})
        if what == "import":
            n = M.import_(data)
            return self.send_json(200, {"ok": True, "msg": "imported %d memories" % n})
        if what == "paste":
            n = M.paste_memories(data.get("text"))
            M.seq += 1 if n else 0
            return self.send_json(200, {"ok": bool(n), "msg": "added %d memories%s" % (n, ", tidying them up in the background" if n and M.learn_on else "")})
        return self.send_json(404, {"error": "not found"})

    def proxy_chat(self, body, upstream, auth, mem_info=None):
        global active_streams
        up = http.client.HTTPConnection(*upstream, timeout=900)
        try:
            up.request("POST", "/v1/chat/completions", body, {
                "Content-Type": "application/json", "Accept": "text/event-stream", **auth})
            resp = up.getresponse()
        except OSError as e:
            up.close()
            return self.send_json(502, {"error": f"model server not reachable on {upstream[1]} ({e.__class__.__name__}). Is it on?"})
        with lock:
            active_streams += 1
        done = threading.Event()

        def watch_browser():
            # Nothing else arrives on this socket while we stream, so "readable" means it closed.
            while not done.is_set():
                try:
                    r, _, _ = select.select([self.connection], [], [], 0.5)
                    if r and not self.connection.recv(1, socket.MSG_PEEK):
                        raise OSError("browser closed")
                except (OSError, ValueError):
                    try:
                        up.sock and up.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    up.close()
                    return
                if r:
                    time.sleep(0.5)

        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            if mem_info:
                self.send_header("X-Memory", json.dumps(mem_info))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            threading.Thread(target=watch_browser, daemon=True).start()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            self.close_connection = True
        finally:
            done.set()
            up.close()
            with lock:
                active_streams -= 1
            touch()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy share the port

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        super().server_bind()


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
        except (ValueError, KeyError, IndexError):
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
    while True:
        time.sleep(5)
        if active_streams == 0 and time.time() - last_request > IDLE_EXIT:
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
    chats.ON_SAVE, chats.ON_DELETE = (lambda c: memory.M.note_chat(c)), (lambda cid: memory.M.forget_chat(cid))
    memory.M.start(settings, save_settings, ROOT, os.path.join(ROOT, "llama.cpp", "llama-server.exe"),
                   llm_json, lambda: active_streams > 0 or switching["on"], chats)
    token_watch = tokens.TokenWatch(LLAMA, PAIR, api_key)
    threading.Thread(target=token_watch.run, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
