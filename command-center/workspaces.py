"""Data and actions for the 2:code and 3:web workspaces of the command center.

code: the two VS Code setups (main VS Code -> this PC's model on 8090; the separate "laptop" VS Code from
      start-vscode-laptop.bat -> the laptop via PAIR on 8080), whether each is running, and opening folders in them.
web:  phone access through Tailscale (`tailscale serve` -> the command center on 127.0.0.1:8099, which forwards
      chat to whichever model "run on" picks), the tailnet's devices, and serve on/off.
Everything that launches a program uses a fixed command; the browser only picks "pc" / "laptop" / "on" / "off",
plus a folder path that must be an existing directory.
"""
import json
import os
import shutil
import subprocess
import time

NOWIN = 0x08000000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAPTOP_BAT = os.path.join(ROOT, "start-vscode-laptop.bat")
LAPTOP_DATA = os.path.join(ROOT, "vscode-laptop").lower()
LAPTOP_CLINE = os.path.join(ROOT, "cline-laptop", "data")      # CLINE_DIR set by start-vscode-laptop.bat
MAIN_CLINE = os.path.join(os.path.expanduser("~"), ".cline", "data")
LAPTOP_URL = "http://127.0.0.1:8080/v1"
PC_URL = "http://127.0.0.1:8090/v1"
CODE_CMD = shutil.which("code") or os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd")
GIT = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
LAPTOP_BRANCH = "cline-laptop"
TAILSCALE = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
SERVE_TARGET = "http://127.0.0.1:8099"
MAX_RECENT = 8
CC_URL = "http://127.0.0.1:8099/cline/%s/v1"   # through the command center: memory + the request log
ROUTE = {"pc": False, "laptop": False}          # set from settings.json by the server


def run(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


# ---------------------------------------------------------------- code

_code_cache = {"t": 0, "v": None}


def vscode_running():
    """Which VS Code setups have a window open: {"pc": bool, "laptop": bool}."""
    if time.time() - _code_cache["t"] < 3 and _code_cache["v"]:
        return _code_cache["v"]
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='Code.exe'\" | "
          "Where-Object { $_.CommandLine -notmatch '--type=' } | ForEach-Object { $_.CommandLine }")
    _, out, _ = run(["powershell", "-NoProfile", "-Command", ps], timeout=15)
    lines = [l.lower() for l in out.splitlines() if l.strip()]
    v = {"laptop": any(LAPTOP_DATA in l for l in lines),
         "pc": any(LAPTOP_DATA not in l for l in lines)}
    _code_cache.update(t=time.time(), v=v)
    return v


def _jload(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _jsave(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def cline_points_to(data_dir, expect=None):
    """Where a Cline data folder sends requests. The VS Code extension reads globalState.json (your main Cline works
    on 8090 while its settings/providers.json still says lmstudio), so that file decides; providers.json is only
    used when globalState.json is missing."""
    g = _jload(os.path.join(data_dir, "globalState.json"))
    if g:
        prov = g.get("actModeApiProvider")
        return (g.get("openAiBaseUrl") or "").rstrip("/") if prov == "openai" else "provider: " + str(prov)
    p = _jload(os.path.join(data_dir, "settings", "providers.json")) or {}
    last = p.get("lastUsedProvider")
    if not last:
        return None
    url = (((p.get("providers") or {}).get(last) or {}).get("settings") or {}).get("baseUrl")
    return (url or "").rstrip("/") if last == "openai-compatible" else "provider: " + str(last)


def pin_laptop_cline(model_id=None):
    """Point the laptop VS Code's Cline at the laptop for both plan and act mode: through the command center if
    that route is on (5:cline), else straight to PAIR on :8080.
    Only while that VS Code is closed: a running Cline keeps its settings in memory and would write them back."""
    return _point_cline(LAPTOP_CLINE, CC_URL % "laptop" if ROUTE["laptop"] else LAPTOP_URL, model_id)


def cline_url(which, through):
    return CC_URL % which if through else (PC_URL if which == "pc" else LAPTOP_URL)


def cline_route(which, through):
    """Switch one VS Code's Cline between the command center (memory) and its model server directly."""
    if which not in ("pc", "laptop"):
        return False, "which must be pc or laptop"
    _code_cache["t"] = 0
    if vscode_running()[which]:
        return False, ("close the %s VS Code first: Cline keeps its settings in memory and would write the old "
                       "address back when it closes" % ("main" if which == "pc" else "laptop"))
    d = MAIN_CLINE if which == "pc" else LAPTOP_CLINE
    gs = os.path.join(d, "globalState.json")
    if not os.path.exists(gs):
        return False, "no Cline settings found in %s (open Cline once first)" % d
    bak = gs + ".bak-command-center"
    if not os.path.exists(bak):
        shutil.copy2(gs, bak)  # the very first time, keep the original settings
    if not _point_cline(d, cline_url(which, through)):
        return False, "couldn't update Cline's settings"
    ROUTE[which] = bool(through)
    return True, ("%s Cline now goes through the command center" if through else "%s Cline now talks to its model directly") % (
        "main" if which == "pc" else "laptop")


def cline_state():
    run_ = vscode_running()
    out = {}
    for w, d in (("pc", MAIN_CLINE), ("laptop", LAPTOP_CLINE)):
        pts = cline_points_to(d)
        out[w] = {"points": pts, "routed": (pts or "").rstrip("/") == CC_URL % w, "running": run_[w],
                  "found": os.path.exists(os.path.join(d, "globalState.json")),
                  "url_cc": CC_URL % w, "url_direct": cline_url(w, False)}
    return out


def _point_cline(d, url, model_id=None):
    g = _jload(os.path.join(d, "globalState.json"))
    if g is None:
        return False
    g.update(planModeApiProvider="openai", actModeApiProvider="openai", openAiBaseUrl=url)
    if model_id:
        g.update(planModeOpenAiModelId=model_id, actModeOpenAiModelId=model_id)
    _jsave(os.path.join(d, "globalState.json"), g)
    pp = os.path.join(d, "settings", "providers.json")
    p = _jload(pp)
    if p is not None:
        p["lastUsedProvider"] = "openai-compatible"
        oc = p.setdefault("providers", {}).setdefault("openai-compatible", {}).setdefault("settings", {})
        oc.update(provider="openai-compatible", baseUrl=url)
        oc.setdefault("apiKey", "none")
        if model_id:
            oc["model"] = model_id
        _jsave(pp, p)
    mp = os.path.join(d, "settings", "models.json")
    m = _jload(mp)
    if m is not None and "openai-compatible" in (m.get("providers") or {}):
        m["providers"]["openai-compatible"].setdefault("provider", {})["baseUrl"] = url
        _jsave(mp, m)
    return True


def code_info(settings, pc_model, laptop_model, key):
    running = vscode_running()
    return {
        "installed": os.path.exists(CODE_CMD),
        "laptop_setup": os.path.exists(LAPTOP_BAT),
        "recent": [p for p in settings.get("recent_projects", []) if os.path.isdir(p)],
        "setups": {
            "pc": {"running": running["pc"], "label": "VS Code", "model": pc_model,
                   "points_to": cline_points_to(MAIN_CLINE, PC_URL), "expect": PC_URL,
                   "cline": {"provider": "OpenAI Compatible", "base_url": cline_url("pc", ROUTE["pc"]),
                             "api_key": key, "model_id": pc_model.get("id") or "(any, the server has one model)"}},
            "laptop": {"running": running["laptop"], "label": "VS Code (laptop)", "model": laptop_model,
                       "points_to": cline_points_to(LAPTOP_CLINE, LAPTOP_URL), "expect": LAPTOP_URL,
                       "cline": {"provider": "OpenAI Compatible", "base_url": "http://127.0.0.1:8080/v1",
                                 "api_key": "any (PAIR has no key)", "model_id": laptop_model.get("id") or "–"}},
        },
    }


def open_code(which, path, settings, save, laptop_model_id=None):
    """Start VS Code (main or the laptop setup), optionally on a folder. Returns (ok, message)."""
    if which not in ("pc", "laptop"):
        return False, "which must be pc or laptop"
    args = []
    if path:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"'))))
        if not os.path.isdir(path):
            return False, "that folder doesn't exist: " + path
        args = [path]
    if which == "pc":
        if not os.path.exists(CODE_CMD):
            return False, "VS Code's `code` command wasn't found"
        cmd = ["cmd", "/c", "call", CODE_CMD, *args]
    else:
        if not os.path.exists(LAPTOP_BAT):
            return False, "start-vscode-laptop.bat is missing"
        if not vscode_running()["laptop"]:
            pin_laptop_cline(laptop_model_id)   # every fresh launch starts on the laptop's model
        cmd = ["cmd", "/c", "call", LAPTOP_BAT, *args]
    subprocess.Popen(cmd, cwd=ROOT, creationflags=NOWIN)
    _code_cache["t"] = 0
    if path:
        rec = [p for p in settings.get("recent_projects", []) if p.lower() != path.lower()]
        settings["recent_projects"] = [path] + rec[:MAX_RECENT - 1]
        save()
    return True, ("opening " + (os.path.basename(path) or path) if path else "opening VS Code") + \
        (" (laptop setup)" if which == "laptop" else "")


def laptop_worktree(repo):
    """Make sure the repo has a sibling worktree "<repo>-laptop" on branch cline-laptop. Returns (ok, path_or_error, created)."""
    wt = os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-laptop")
    _, out, _ = run([GIT, "-C", repo, "worktree", "list", "--porcelain"])
    known = [l[9:].strip().replace("/", "\\").lower() for l in out.splitlines() if l.startswith("worktree ")]
    if wt.lower() in known and os.path.isdir(wt):
        return True, wt, False
    if os.path.exists(wt):
        return False, wt + " already exists and isn't a worktree of this repo - move it or open the two separately", False
    run([GIT, "-C", repo, "worktree", "prune"])
    code, _, _ = run([GIT, "-C", repo, "rev-parse", "--verify", "--quiet", "HEAD"])
    if code != 0:
        return False, "this repo has no commits yet - make a first commit, then try again", False
    code, _, _ = run([GIT, "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + LAPTOP_BRANCH])
    args = [GIT, "-C", repo, "worktree", "add", wt] + ([LAPTOP_BRANCH] if code == 0 else ["-b", LAPTOP_BRANCH])
    code, out, err = run(args, timeout=60)
    if code != 0:
        return False, "git worktree failed: " + (err or out).strip()[:250], False
    return True, wt, True


def open_both(path, settings, save):
    """PC VS Code on the repo, laptop VS Code on its own worktree, so the two agents never edit the same files."""
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))
    if not os.path.isdir(path):
        return False, "that folder doesn't exist: " + path
    if not os.path.exists(GIT):
        return False, "git wasn't found"
    code, out, _ = run([GIT, "-C", path, "rev-parse", "--show-toplevel"])
    if code != 0:
        return False, "not a git repo - run `git init` and commit once, so each agent can get its own copy"
    repo = os.path.normpath(out.strip())
    ok, wt, created = laptop_worktree(repo)
    if not ok:
        return False, wt
    ok, msg = open_code("pc", repo, settings, save)
    if not ok:
        return False, msg
    ok, msg = open_code("laptop", wt, settings, lambda: None)
    if not ok:
        return False, "pc opened, laptop failed: " + msg
    _, dirty, _ = run([GIT, "-C", repo, "status", "--porcelain"])
    note = " (uncommitted changes stay in the pc copy only)" if created and dirty.strip() else ""
    return True, ("made" if created else "reusing") + " worktree " + os.path.basename(wt) + " on branch " + LAPTOP_BRANCH + "; merge it back with: git merge " + LAPTOP_BRANCH + note


def forget_recent(path, settings, save):
    settings["recent_projects"] = [p for p in settings.get("recent_projects", []) if p != path]
    save()
    return True, "removed"


# ---------------------------------------------------------------- web

_web_cache = {"t": 0, "v": None}


def web_info(key, pc_model):
    if time.time() - _web_cache["t"] < 4 and _web_cache["v"]:
        v = dict(_web_cache["v"])
        v["model"] = pc_model
        return v
    info = {"installed": os.path.exists(TAILSCALE), "running": False, "self": None, "peers": [],
            "serve": {"on": False, "url": None, "target": None}, "api_key": key, "model": pc_model, "error": None}
    if not info["installed"]:
        info["error"] = "Tailscale isn't installed"
        return info
    code, out, err = run([TAILSCALE, "status", "--json"])
    try:
        st = json.loads(out)
    except ValueError:
        info["error"] = (err or out or "tailscale status failed").strip()[:200]
        return info
    info["running"] = st.get("BackendState") == "Running"
    me = st.get("Self") or {}
    dns = (me.get("DNSName") or "").rstrip(".")
    info["self"] = {"name": me.get("HostName"), "dns": dns, "ip": (me.get("TailscaleIPs") or [None])[0],
                    "online": bool(me.get("Online")) and info["running"], "os": me.get("OS")}
    for p in (st.get("Peer") or {}).values():
        name = p.get("HostName") or ""
        if not name or name.lower() == "localhost":  # iOS reports "localhost"; the tailnet name is more useful
            name = (p.get("DNSName") or "").split(".")[0] or name
        info["peers"].append({"name": name, "dns": (p.get("DNSName") or "").rstrip("."),
                              "ip": (p.get("TailscaleIPs") or [None])[0], "os": p.get("OS"),
                              "online": bool(p.get("Online")), "last_seen": p.get("LastSeen")})
    info["peers"].sort(key=lambda p: (not p["online"], (p["name"] or "").lower()))
    # what `tailscale serve` is exposing
    _, sout, _ = run([TAILSCALE, "serve", "status", "--json"])
    try:
        sv = json.loads(sout or "{}")
    except ValueError:
        sv = {}
    for host, cfg in (sv.get("Web") or {}).items():
        h = ((cfg or {}).get("Handlers") or {}).get("/") or {}
        if h.get("Proxy"):
            info["serve"] = {"on": True, "url": "https://" + host.split(":")[0] + ("" if host.endswith(":443") else ":" + host.rsplit(":", 1)[-1]),
                             "target": h["Proxy"]}
            break
    if not info["serve"]["on"] and dns:
        info["serve"]["url"] = "https://" + dns  # what it will be once turned on
    _web_cache.update(t=time.time(), v=info)
    return info


_dns_cache = {"t": 0, "v": ""}


def tailnet_dns():
    """This PC's tailnet name (e.g. mypc.tailXXXX.ts.net), cached for a minute. "" if Tailscale isn't up."""
    if time.time() - _dns_cache["t"] < 60:
        return _dns_cache["v"]
    dns = ""
    if os.path.exists(TAILSCALE):
        _, out, _ = run([TAILSCALE, "status", "--json"], timeout=5)
        try:
            dns = ((json.loads(out).get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        except ValueError:
            pass
    _dns_cache.update(t=time.time(), v=dns)
    return dns


def set_serve(on):
    if not os.path.exists(TAILSCALE):
        return False, "Tailscale isn't installed"
    if on:
        code, out, err = run([TAILSCALE, "serve", "--bg", "--https=443", SERVE_TARGET], timeout=30)
    else:
        code, out, err = run([TAILSCALE, "serve", "--https=443", "off"], timeout=30)
    _web_cache["t"] = 0
    if code != 0:
        return False, (err or out or "tailscale serve failed").strip()[:300]
    return True, "phone access on" if on else "phone access off"
