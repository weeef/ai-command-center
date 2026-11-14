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
import models  # noqa: E402  (the model library: scan models, size for this PC, launch, download)
import cloud  # noqa: E402  (optional web search / stronger cloud model as tools)
import updater  # noqa: E402  (new versions from GitHub, installed when you say so)
import llamaup  # noqa: E402  (llama.cpp engine updates)
import urllib.parse  # noqa: E402
import collections  # noqa: E402
import re  # noqa: E402

PORT = int(os.environ.get("CC_PORT") or 8099)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LLAMA = ("127.0.0.1", 8090)
KEYFILE = os.path.join(ROOT, "api-key.txt")
LLAMA_EXE = os.path.normcase(os.path.join(ROOT, "llama.cpp", "llama-server.exe"))
MAX_BODY = 64 * 1024 * 1024
IDLE_EXIT = 180
NOWIN = 0x08000000  # CREATE_NO_WINDOW, so pythonw never flashes consoles

# Models come from models.py, which scans models\ for .gguf files. The browser sends a library id; the file path is
# always looked up from the scan, never taken from the request.

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
launch = {"key": None, "t": 0.0, "proc": None, "fail": ""}  # last model we launched, for LOADING before the port opens
switching = {"on": False, "msg": ""}


LOG_FILE = os.path.join(HERE, "server.log")
_last_err = {}


def log_error(where):
    """Traceback into command-center\\server.log (kept under ~1 MB); the same error at most once a minute."""
    import traceback
    tb = traceback.format_exc()
    key = (where, tb.strip().splitlines()[-1] if tb.strip() else "")
    if time.time() - _last_err.get(key, 0) < 60:
        return
    _last_err[key] = time.time()
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + where + "\n" + tb + "\n")
    except OSError:
        pass


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
    return models.from_path(path)


def model_name(key):
    m = models.find(key) if key else None
    return m["name"] if m else (os.path.basename(key) if key else None)


def model_info(gpu_util):
    info = {"state": "OFF", "key": None, "name": None, "file": None, "ctx": None,
            "vision": None, "engine": None, "id": None, "processing": False,
            "note": switching["msg"] if switching["on"] else ""}
    p = launch.get("proc")
    if p is not None and p.poll() is not None:  # llama-server exited on its own: say why instead of "loading"
        launch.update(proc=None, t=0, fail=models.log_tail(6) or "llama-server exited (code %s)" % p.returncode)
    if launch.get("fail"):
        info["note"] = "failed to start: " + launch["fail"].splitlines()[-1][:160]
        info["error"] = launch["fail"]
    try:
        code, health = llama_get("/health")
    except OSError:
        if switching["on"] or (launch["proc"] is not None and time.time() - launch["t"] < 600) or time.time() - launch["t"] < 90:
            info.update(state="LOADING", key=launch["key"])
            info["name"] = model_name(launch["key"])
        return info
    if code == 503:
        info.update(state="LOADING", key=launch["key"])
        info["name"] = model_name(launch["key"])
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
        info["name"] = model_name(info["key"]) or os.path.basename(path)
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


def model_ctx(m):
    pref = (settings.get("model_prefs") or {}).get(m["id"]) or {}
    c = pref.get("ctx")
    return c if c in models.ctx_choices(m) else models.default_ctx(m)


def start_model(m):
    """llama-server in the background, sized for this PC (models.py); output in command-center\\llama.log."""
    launch.update(key=m["id"], t=time.time(), fail="", proc=None)
    if LU:  # nothing from the llama.cpp folder runs right now: the moment to swap in a downloaded engine update
        done = LU.swap_if_staged()
        if done:
            sys.stderr and print(time.ctime(), done, file=sys.stderr)
    launch["proc"] = models.launch(m, model_ctx(m))


def model_action(action, key):
    try:
        if action == "off":
            switching.update(on=True, msg="stopping")
            ok, msg = stop_model()
            launch.update(key=None, t=0, proc=None, fail="")
            return ok, msg
        m = models.find(key)
        if not m:
            return False, "no model called %s in the models folder" % key
        key = m["id"]
        if port_open():
            try:
                _, props = llama_get("/props")
                running = which_model((props or {}).get("model_path"))
            except Exception:
                running = None
            if running == key:
                return True, "already on"
            switching.update(on=True, msg="switching to " + m["name"])
            ok, msg = stop_model()
            if not ok:
                return False, msg
        switching.update(on=True, msg="starting " + m["name"])
        try:
            start_model(m)
        except Exception as e:
            launch.update(fail=str(e))
            return False, str(e)
        return True, "starting " + m["name"]
    finally:
        switching.update(on=False, msg="")


def models_state():
    hw = models.hardware()
    lib = [models.describe(m, model_ctx(m), hw) for m in models.scan()]
    return {"hardware": hw, "models": lib, "llama": os.path.exists(models.LLAMA_EXE), "folder": models.MODELS_DIR,
            "downloads": {k: v for k, v in models.downloads.items() if v["state"] != "cancelled"}}


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
_BUILD_FILES = ("index.html", "server.py", "models.py", "workspaces.py", "tokens.py", "chats.py", "memory.py", "importers.py", "cloud.py", "updater.py", "llamaup.py")
UP = None  # updater.Updater, made in main()
LU = None  # llamaup.LlamaUpdater, made in main()


def llama_stop_all(embedder_only=False):
    """Frees the llama.cpp folder for an engine swap: stops the memory embedder and (unless embedder_only) the model.
    Returns (ok, msg, key of the model that was running so it can be started again)."""
    emb = getattr(memory.M, "embedder", None)
    if emb:
        emb.last_start = time.time() + 60  # keep it from restarting itself mid-swap
        p = emb.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    if embedder_only:
        return True, "ok", None
    if active_streams > 0:
        return False, "a reply is still streaming; try again when it's done", None
    running = None
    if port_open():
        try:
            _, props = llama_get("/props")
            running = which_model((props or {}).get("model_path")) or launch.get("key")
        except Exception:
            running = launch.get("key")
    switching.update(on=True, msg="updating llama.cpp")
    try:
        ok, msg = stop_model()
        launch.update(key=None, t=0, proc=None, fail="")
    finally:
        switching.update(on=False, msg="")
    if not ok:
        return False, msg, None
    time.sleep(1.0)  # let Windows release the DLLs
    return True, "stopped", running


_vision = {"t": 0.0, "v": True}


def pc_has_vision():
    """Whether the model on :8090 takes images (cached a few seconds). Fast-text MTP models run without --mmproj."""
    if time.time() - _vision["t"] > 5:
        try:
            _, props = llama_get("/props")
            _vision["v"] = bool(((props or {}).get("modalities") or {}).get("vision"))
        except Exception:
            _vision["v"] = True  # unknown: leave the request alone
        _vision["t"] = time.time()
    return _vision["v"]


def strip_images(data):
    """A text-only model rejects the whole request (HTTP 500) if any message has an image, even an old one.
    Swap images for a short note instead. Only for this PC's model; returns how many were removed."""
    msgs = data.get("messages")
    if not isinstance(msgs, list) or pc_has_vision():
        return 0
    n = 0
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        k = sum(1 for p in c if isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image"))
        if k:
            n += k
            m["content"] = [p for p in c if not (isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image"))]
            m["content"].append({"type": "text", "text": "[%d image%s omitted: the current model has no image input]" % (k, "s" if k > 1 else "")})
    return n


def llama_model_off():
    p = launch.get("proc")
    return not switching["on"] and not port_open() and (p is None or p.poll() is not None)
STARTED = time.time()


def update_brief():
    if not UP:
        return None
    s = UP.state
    skip = UP.prefs().get("skip")
    return {"available": s["available"], "behind": s["behind"], "latest": s["latest"],
            "skipped": bool(skip and skip == s["latest"]), "busy": s["busy"]}


def restart_server():
    """Starts a fresh copy of server.py (it waits for this one to let go of the port) and exits this one."""
    time.sleep(0.8)  # let the reply reach the page
    try:
        memory.M.shutdown()
    except Exception:
        pass
    subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], cwd=HERE,
                     env=dict(os.environ, CC_RESTART="1"), creationflags=NOWIN | 0x00000008, close_fds=True)
    os._exit(0)


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


def part(fn, fallback, what):
    try:
        return fn()
    except Exception:
        log_error("stats: " + what)
        return fallback


def stats():
    with lock:
        if time.time() - _stats["t"] < 0.7 and _stats["v"]:
            return _stats["v"]
    # Both machines every time, so the usage pane can show either one whatever the target is.
    g = gpu()
    pc = {"label": "this pc", "name": socket.gethostname(), "ok": True, "gpu": g,
          "ram": part(ram, {"total": 0, "free": 0}, "ram"), "cpu": part(cpu_percent, 0.0, "cpu")}
    off = {"state": "OFF", "key": None, "name": None, "processing": False, "note": "status unavailable (see server.log)"}
    lm = laptop_cache["machine"]
    lap = {"label": "laptop", "name": lm["name"] if lm else (laptop_address()[1] or "laptop"), "ok": lm is not None,
           "gpu": lm and lm["gpu"], "ram": lm["ram"] if lm else {"total": 0, "free": 0}, "cpu": lm["cpu"] if lm else 0}
    if target["v"] == "laptop":
        m = part(laptop_info, dict(off), "laptop model")
        # Busy comes from our own chats and the laptop's slot status; this PC's GPU says nothing about the laptop.
        busy = active_streams > 0 or m["processing"]
        machine = lap
    else:
        m = part(lambda: model_info(g and g.get("util")), dict(off), "model")
        busy = active_streams > 0 or m["processing"] or (
            m["state"] == "ON" and g is not None and (g.get("util") or 0) >= 25)
        machine = pc
    v = {"host": socket.gethostname(), "user": os.environ.get("USERNAME", "user"), "target": target["v"],
         "machine": machine["label"], "machine_name": machine["name"], "machine_ok": machine["ok"],
         "gpu": machine["gpu"], "ram": machine["ram"], "cpu": machine["cpu"], "model": m, "busy": busy,
         "machines": {"pc": pc, "laptop": lap},
         "laptop_online": laptop_cache["machine"] is not None,
         "laptop_note": None if laptop_cache["machine"] is not None else part(laptop_reach_note, None, "laptop note"),
         "laptop_name": (laptop_cache["machine"] or {}).get("name") or part(lambda: laptop_address()[1], None, "laptop name"),
         "tokens": part(token_watch.view, None, "tokens") if token_watch else None,
         "ui_streams": active_streams,
         "build": build_stamp(), "update": part(update_brief, None, "update"), "time": time.time()}
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
                      "/api/memory/import-file", "/api/clinecc", "/cline", "/api/cloud", "/api/update", "/api/llama-update",
                      "/api/models/hf", "/api/models/download", "/api/models/cancel", "/api/models/open")

    def remote_blocked(self, path):
        if self.remote and any(path == p or path.startswith(p + "/") for p in self.REMOTE_BLOCKED):
            self.send_json(403, {"error": "only available on the PC"})
            return True
        return False

    def hold(self):
        """Call first in every do_*. Until a request's body has been read in full, the connection must not be
        reused: whatever is left unread would be parsed as the next request, with headers of the sender's choosing
        (no Origin, a local Host). That would let any web page smuggle an API call past guard() inside the body
        of a refused request. Returns whether the connection may be kept once the body has been read."""
        keep = not self.close_connection
        if (self.headers.get("Content-Length") or "0").strip() != "0" or "Transfer-Encoding" in self.headers:
            self.close_connection = True
        return keep

    def do_GET(self):
        self.safely(self._get)

    def do_POST(self):
        self.safely(self._post)

    def safely(self, fn):
        try:
            fn()
        except (ConnectionError, TimeoutError):
            pass  # the browser went away mid-reply
        except Exception as e:
            log_error("%s %s" % (self.command, self.path.split("?", 1)[0]))
            self.close_connection = True
            try:
                self.send_json(500, {"error": "server error: %s: %s (details in command-center\\server.log)" % (e.__class__.__name__, e)})
            except Exception:
                pass

    def _get(self):
        self.hold()
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        if self.remote_blocked(path):
            return
        if path.startswith("/fonts/"):
            return self.send_font(path[len("/fonts/"):])
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
            self.send_json(200, {"ok": True, "app": "command-center", "started": STARTED})
        elif path == "/api/stats":
            self.send_json(200, dict(stats(), remote=self.remote, memory=memory.M.brief()))
        elif path == "/api/memory":
            self.send_json(200, memory.M.status())
        elif path == "/api/update":
            self.send_json(200, UP.public())
        elif path == "/api/llama-update":
            self.send_json(200, LU.public())
        elif path == "/api/models":
            self.send_json(200, models_state())
        elif path == "/api/cloud":
            self.send_json(200, cloud.status(settings))
        elif path == "/api/clinecc":
            self.send_json(200, {"route": workspaces.cline_state(), "memory": settings.get("cline_memory", True),
                                 "learn": settings.get("cline_learn", True), "log": list(cline_log)[::-1],
                                 "memory_on": memory.M.on and memory.M.ok()})
        elif path.startswith("/cline/") and path.endswith("/v1/models"):
            return self.cline_models(path.split("/")[2])
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
        elif path == "/api/memory/tree":
            self.send_json(200, memory.M.tree())
        elif path == "/api/memory/synapses":
            self.send_json(200, {"synapses": memory.M.synapses()})
        elif path == "/api/memory/related":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                mid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                mid = 0
            self.send_json(200, {"related": memory.M.related(mid)})
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
                                     "base_url": workspaces.cline_url("laptop", workspaces.ROUTE["laptop"]), "api_key": "any"})
            else:
                self.send_json(200, {"provider": "OpenAI Compatible", "target": "local",
                                     "base_url": workspaces.cline_url("pc", workspaces.ROUTE["pc"]), "api_key": api_key()})
        else:
            self.send_json(404, {"error": "not found"})

    def _post(self):
        keep = self.hold()
        if not self.guard():
            return
        if "Transfer-Encoding" in self.headers:
            return self.send_json(400, {"error": "Transfer-Encoding is not supported; send Content-Length"})
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
        if len(body) == n:
            self.close_connection = not keep   # fully read: the connection is safe to reuse
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
        if path.startswith("/api/models/"):
            return self.models_post(path[len("/api/models/"):], data)
        if path.startswith("/cline/") and path.endswith("/v1/chat/completions"):
            return self.cline_chat(path.split("/")[2], data)
        if path.startswith("/api/llama-update/"):
            return self.llama_update_post(path.rsplit("/", 1)[1], data)
        if path == "/api/update/check":
            return self.send_json(200, UP.check())
        if path == "/api/update/settings":
            if "auto_check" in data:
                UP.set_prefs(auto_check=bool(data["auto_check"]))
            if "skip" in data:
                UP.set_prefs(skip=str(data["skip"])[:40] if data["skip"] else None)
            return self.send_json(200, UP.public())
        if path == "/api/update/apply":
            if active_streams > 0 or switching["on"]:
                return self.send_json(409, {"ok": False, "msg": "wait for the current reply or model switch to finish"})
            ok, msg = UP.apply()
            if ok:
                threading.Thread(target=restart_server, daemon=True).start()
            return self.send_json(200 if ok else 409, {"ok": ok, "msg": msg, "restarting": ok})
        if path == "/api/cloud/key":
            ok = cloud.set_key(data.get("provider"), data.get("key"))
            return self.send_json(200 if ok else 400, {"ok": ok, "msg": "saved" if ok else "unknown provider"})
        if path == "/api/cloud/test":
            if data.get("provider") not in cloud.PROVIDERS:
                return self.send_json(400, {"ok": False, "msg": "unknown provider"})
            ok, msg, ids = cloud.list_models(data["provider"])
            return self.send_json(200, {"ok": ok, "msg": msg, "models": ids})
        if path == "/api/cloud/settings":
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
            return self.send_json(200, {"ok": True})
        if path == "/api/clinecc/route":
            ok, msg = workspaces.cline_route(data.get("which"), bool(data.get("through")))
            if ok:
                settings.setdefault("cline_route", {})[data["which"]] = bool(data.get("through"))
                save_settings()
            return self.send_json(200 if ok else 409, {"ok": ok, "msg": msg})
        if path == "/api/clinecc/settings":
            for k in ("memory", "learn"):
                if k in data:
                    settings["cline_" + k] = bool(data[k])
            save_settings()
            return self.send_json(200, {"ok": True})
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
            key = data.get("model") or "standard"
            if action not in ("on", "off") or not isinstance(key, str):
                return self.send_json(400, {"error": "action must be on or off"})
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
                cid = chat_id if isinstance(chat_id, str) else None
                try:
                    sys_txt, mem_info = memory.M.recall(data["messages"], cid)
                except Exception:
                    sys_txt = None
                data["messages"] = memory.M.chat_inject(data["messages"], cid, sys_txt)
            if target["v"] == "laptop":
                mid = laptop_info()["id"]
                if not mid:
                    return self.send_json(502, {"error": "no laptop model listed by PAIR. Is the laptop on?"})
                data["model"] = mid  # the model id always comes from PAIR's listing, never the browser
                host, auth = PAIR, {}
            else:
                host, auth = LLAMA, {"Authorization": "Bearer " + api_key()}
                strip_images(data)
            tools = cloud.tools_for(settings) if data.get("stream") and not data.get("tools") else None
            if tools:
                return self.tool_chat(data, host, auth, mem_info, tools)
            return self.proxy_chat(json.dumps(data).encode(), host, auth, mem_info)
        self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        self.hold()
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

    def _raw(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))

    def _sse(self, obj):
        self._raw(b"data: " + json.dumps(obj).encode() + b"\n\n")

    def tool_chat(self, data, upstream, auth, mem_info, tools):
        """Chat where the local model may call web_search / ask_cloud. Its reply streams to the browser as usual;
        tool-call deltas are held back, the tools run here (cloud.py), the browser gets {"cc_tool": ...} progress
        events, and the model continues with the results. At most 4 tool rounds, then it must answer."""
        global active_streams
        msgs = list(data["messages"])
        # The model doesn't know today's date and distrusts "future" dates in fresh results without this.
        # Same text all day, so the prompt cache still covers the conversation.
        note = ("Today is %s. You have tools: web_search looks things up on the live web (more current than your "
                "training data, so trust it for recent events, prices and releases) and ask_cloud asks a stronger AI. "
                "Use them only when they help. When you use search results, mention the source briefly."
                % time.strftime("%A %Y-%m-%d"))
        if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
            msgs[0] = dict(msgs[0], content=note + "\n\n" + msgs[0]["content"])
        else:
            msgs.insert(0, {"role": "system", "content": note})
        started, up = False, None
        with lock:
            active_streams += 1
        try:
            for rnd in range(5):
                body = dict(data, messages=msgs, stream=True)
                if rnd < 4:
                    body["tools"] = tools
                up = http.client.HTTPConnection(*upstream, timeout=900)
                try:
                    up.request("POST", "/v1/chat/completions", json.dumps(body),
                               {"Content-Type": "application/json", "Accept": "text/event-stream", **auth})
                    resp = up.getresponse()
                except OSError as e:
                    if not started:
                        return self.send_json(502, {"error": f"model server not reachable on {upstream[1]} ({e.__class__.__name__}). Is it on?"})
                    self._sse({"error": {"message": "the model server went away"}})
                    break
                if resp.status != 200:
                    err = resp.read()
                    if not started:
                        self.send_response(resp.status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return
                    self._sse({"error": {"message": err.decode("utf-8", "replace")[:300]}})
                    break
                if not started:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    if mem_info:
                        self.send_header("X-Memory", json.dumps(mem_info))
                    self.end_headers()
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
                        ch = (j.get("choices") or [{}])[0]
                        d = ch.get("delta") or {}
                        if d.get("tool_calls"):
                            for tc in d["tool_calls"]:
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
                for t in tcs:
                    name = t["function"]["name"]
                    try:
                        args = json.loads(t["function"]["arguments"] or "{}")
                    except ValueError:
                        args = {}
                    self._sse({"cc_tool": {"id": t["id"], "name": name, "args": args, "status": "running",
                                           "provider": cloud.NAMES[cloud.config(settings)["provider"]]}})
                    try:
                        res = cloud.run(settings, name, args)
                    except Exception as e:
                        res = {"ok": False, "error": "tool crashed: %s" % e.__class__.__name__, "sources": []}
                    self._sse({"cc_tool": {"id": t["id"], "name": name, "args": args, "status": "done" if res["ok"] else "error",
                                           "error": res.get("error"), "sources": res.get("sources", []), "provider": res.get("provider")}})
                    if res["ok"]:
                        out = res["text"] or "(no answer)"
                        if res.get("sources"):
                            out += "\n\nSources:\n" + "\n".join("- %s: %s" % (s["title"], s["url"]) for s in res["sources"])
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

    def send_font(self, name):
        """The bundled Inter / IBM Plex Mono files (SIL OFL), so the app looks the same offline and on the phone."""
        if not re.fullmatch(r"[a-z0-9-]+\.woff2", name):
            return self.send_json(404, {"error": "not found"})
        try:
            with open(os.path.join(HERE, "fonts", name), "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        self.wfile.write(body)

    def cline_chat(self, which, data):
        """Cline -> (memory) -> the model that VS Code is set up for: "pc" = this PC's llama-server, "laptop" = PAIR.
        Memory is recalled once per task and put in the task message, so every step of the task sends the same
        prompt prefix and llama-server's prompt cache keeps working."""
        if which not in ("pc", "laptop"):
            return self.send_json(404, {"error": "not found"})
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
        strip_images(data)
        return self.proxy_chat(json.dumps(data).encode(), LLAMA, {"Authorization": "Bearer " + api_key()}, info, entry)

    def cline_models(self, which):
        if which not in ("pc", "laptop"):
            return self.send_json(404, {"error": "not found"})
        host, auth = (PAIR, {}) if which == "laptop" else (LLAMA, {"Authorization": "Bearer " + api_key()})
        c = http.client.HTTPConnection(*host, timeout=5)
        try:
            c.request("GET", "/v1/models", headers=auth)
            r = c.getresponse()
            body = r.read()
        except OSError:
            return self.send_json(502, {"error": {"message": "model server not reachable"}})
        finally:
            c.close()
        self.send_response(r.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def llama_update_post(self, what, data):
        """6:set -> llama.cpp engine. install/rollback run in the background; the page polls /api/llama-update."""
        if what == "check":
            return self.send_json(200, LU.check())
        if what == "settings":
            for k in ("auto_check", "auto_install"):
                if k in data:
                    LU.set_prefs(**{k: bool(data[k])})
            return self.send_json(200, LU.public())
        if what in ("install", "rollback"):
            if LU.state["busy"]:
                return self.send_json(409, {"ok": False, "msg": "already busy: " + (LU.state["phase"] or "working")})
            if active_streams > 0 or switching["on"]:
                return self.send_json(409, {"ok": False, "msg": "wait for the current reply or model switch to finish"})
            threading.Thread(target=LU.install if what == "install" else LU.rollback, daemon=True).start()
            return self.send_json(200, {"ok": True, "msg": "started"})
        return self.send_json(404, {"error": "unknown"})

    def models_post(self, op, data):
        if op == "prefs":
            m = models.find(data.get("id"))
            ctx = data.get("ctx")
            if not m or ctx not in models.ctx_choices(m):
                return self.send_json(400, {"error": "unknown model or context size"})
            settings.setdefault("model_prefs", {})[m["id"]] = {"ctx": ctx}
            save_settings()
            return self.send_json(200, {"ok": True, "model": models.describe(m, ctx)})
        if op == "hf":
            try:
                return self.send_json(200, models.hf_files(data.get("url")))
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            except Exception as e:
                return self.send_json(502, {"error": "couldn't read that from Hugging Face: " + str(e)[:200]})
        if op == "download":
            files = data.get("files")
            if not isinstance(files, list) or not files or len(files) > 20:
                return self.send_json(400, {"error": "files must be a list"})
            try:
                return self.send_json(200, {"ok": True, "id": models.download(data.get("repo"), files)})
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
        if op == "cancel":
            d = models.downloads.get(str(data.get("id")))
            if d and d["state"] in ("starting", "downloading"):
                d["state"] = "cancelled"
            return self.send_json(200, {"ok": True})
        if op == "open":
            models.open_folder()
            return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": "not found"})

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

    def proxy_chat(self, body, upstream, auth, mem_info=None, entry=None):
        global active_streams
        up = http.client.HTTPConnection(*upstream, timeout=900)
        try:
            up.request("POST", "/v1/chat/completions", body, {
                "Content-Type": "application/json", "Accept": "text/event-stream", **auth})
            resp = up.getresponse()
        except OSError as e:
            up.close()
            if entry is not None:
                entry["status"] = "model off"
            return self.send_json(502, {"error": f"model server not reachable on {upstream[1]} ({e.__class__.__name__}). Is it on?"})
        t0, tail = time.time(), b""
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
                if entry is not None:
                    tail = (tail + chunk)[-16384:]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            self.close_connection = True
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

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        super().server_bind()


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
    # after an update the old copy is still exiting: wait for the port instead of assuming another copy is running
    tries = 60 if os.environ.pop("CC_RESTART", None) else 1
    for i in range(tries):
        try:
            srv = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            if i == tries - 1:
                sys.exit(0)  # already running
            time.sleep(0.25)
    global UP
    UP = updater.Updater(ROOT, settings, save_settings)
    threading.Thread(target=UP.watch, daemon=True).start()
    global LU
    LU = llamaup.LlamaUpdater(ROOT, settings, save_settings)
    LU.stop_all, LU.model_off = llama_stop_all, llama_model_off
    LU.restart = lambda key: model_action("on", key)
    threading.Thread(target=LU.watch, daemon=True).start()
    cpu_percent()
    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=laptop_watch, daemon=True).start()
    global token_watch
    for w in ("pc", "laptop"):
        workspaces.ROUTE[w] = bool((settings.get("cline_route") or {}).get(w))
    chats.ON_SAVE, chats.ON_DELETE = (lambda c: memory.M.note_chat(c)), (lambda cid: memory.M.forget_chat(cid))
    memory.M.start(settings, save_settings, ROOT, os.path.join(ROOT, "llama.cpp", "llama-server.exe"),
                   llm_json, lambda: active_streams > 0 or switching["on"], chats)
    token_watch = tokens.TokenWatch(LLAMA, PAIR, api_key)
    threading.Thread(target=token_watch.run, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    if sys.stderr is None:  # pythonw: keep background-thread crashes instead of losing them
        try:
            sys.stderr = open(os.path.join(HERE, "server.log"), "a", encoding="utf-8", buffering=1)
        except OSError:
            pass
    main()
