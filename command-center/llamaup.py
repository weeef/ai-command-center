"""llama.cpp engine updates: keeps llama.cpp\\ on a recent official build from github.com/ggml-org/llama.cpp.

How it works:
1. check   - asks GitHub for recent releases and picks the newest "bNNNNN" build that has a Windows zip of the same
             kind as the one installed (cuda-13 / cuda-12 / vulkan / cpu).
2. stage   - downloads that zip into llama.cpp.dl\\, checks it against the sha256 GitHub publishes, unpacks it to
             llama.cpp.next\\ and runs `llama-server --version` from there to prove it starts. The CUDA runtime zip
             (~400 MB) is only downloaded when its checksum differs from the one already installed; otherwise the
             runtime DLLs are copied over from the current install.
3. swap    - with every llama-server from llama.cpp\\ stopped: llama.cpp\\ -> llama.cpp-prev\\, llama.cpp.next\\ ->
             llama.cpp\\. Windows refuses to rename a folder whose DLLs are in use, so a half-swap can't happen; if
             the second rename fails the first is undone.
4. rollback - swaps llama.cpp\\ and llama.cpp-prev\\ back.

Automatic mode (settings "llama_update": auto_check, auto_install): checks every 12 h, stages the newest build that
has been out for a day (brand-new builds sometimes get a quick fix), and swaps it in the next time no model is
running - right before the command center starts a model, or when it finds the model off. A running model is never
stopped by auto mode; "install now" in 6:set does that (it restarts the same model afterwards).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
UA = {"User-Agent": "command-center-llama-updater", "Accept": "application/vnd.github+json"}
NOWIN = 0x08000000
CHECK_EVERY = 12 * 3600
SETTLE = 24 * 3600  # auto mode waits this long after a release before installing it
MARKER = ".cc-build.json"  # written into each install we make: tag, flavor, runtime checksum
RUNTIME_DLLS = re.compile(r"^(cudart|cublas|cublasLt|nvrtc|nvJitLink)[\w.-]*\.dll$", re.I)


def _get_json(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read())


def _ts(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0


def _build_no(tag):
    m = re.fullmatch(r"b(\d+)", tag or "")
    return int(m.group(1)) if m else None


class LlamaUpdater:
    def __init__(self, root, settings, save_settings):
        self.root = root
        self.dir = os.path.join(root, "llama.cpp")
        self.next = os.path.join(root, "llama.cpp.next")
        self.prev = os.path.join(root, "llama.cpp-prev")
        self.dl = os.path.join(root, "llama.cpp.dl")
        self.settings, self.save_settings = settings, save_settings
        self.lock = threading.Lock()
        # set by server.py: stop_all() -> (ok, msg, running_key); restart(key); model_off() -> bool
        self.stop_all = self.restart = self.model_off = None
        self.state = {"busy": False, "phase": "", "progress": None, "error": None, "checked": 0,
                      "installed": None, "latest": None, "staged": None, "prev": None, "available": False,
                      "last_msg": ""}
        self.refresh_local()

    # ------------------------------------------------------------ settings / status
    def prefs(self):
        p = self.settings.get("llama_update")
        return p if isinstance(p, dict) else {}

    def set_prefs(self, **kw):
        p = dict(self.prefs())
        p.update(kw)
        self.settings["llama_update"] = p
        self.save_settings()

    def public(self):
        s = dict(self.state)
        s["auto_check"] = self.prefs().get("auto_check", True)
        s["auto_install"] = self.prefs().get("auto_install", True)
        return s

    def version_of(self, folder):
        """{"build": 12290, "commit": "eeb2104"} from `llama-server --version`, or None if it won't run."""
        exe = os.path.join(folder, "llama-server.exe")
        if not os.path.exists(exe):
            return None
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, cwd=folder,
                               creationflags=NOWIN, encoding="utf-8", errors="replace",
                               env=dict(os.environ, CUDA_VISIBLE_DEVICES="-1"))
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"build (\d+), commit (\w+)", (r.stdout or "") + (r.stderr or ""))
        return {"build": int(m.group(1)), "commit": m.group(2)} if m else None

    def marker(self, folder):
        try:
            with open(os.path.join(folder, MARKER), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def flavor(self, folder=None):
        folder = folder or self.dir
        mk = self.marker(folder).get("flavor")
        if mk:
            return mk
        names = {n.lower() for n in os.listdir(folder)} if os.path.isdir(folder) else set()
        if any(n.startswith("cudart64_13") for n in names):
            return "cuda-13"
        if any(n.startswith("cudart64_12") for n in names):
            return "cuda-12"
        if "ggml-cuda.dll" in names:
            return "cuda-13"
        if "ggml-vulkan.dll" in names:
            return "vulkan"
        return "cpu"

    def refresh_local(self):
        v = self.version_of(self.dir)
        if v:
            v["flavor"] = self.flavor()
        self.state["installed"] = v
        p = self.version_of(self.prev) if os.path.isdir(self.prev) else None
        self.state["prev"] = p
        st = self.marker(self.next) if os.path.isdir(self.next) else {}
        self.state["staged"] = st if st.get("build") else None
        self.state["available"] = self._newer(self.state["latest"])

    def _newer(self, latest):
        inst = self.state["installed"]
        return bool(latest and (not inst or latest["build"] > inst["build"]))

    # ------------------------------------------------------------ check
    def pick(self, releases, flavor, settled=False):
        main_re = re.compile(r"^llama-b\d+-bin-win-%s(?:[.-][\d.]+)?-x64\.zip$" % re.escape(flavor))
        rt_re = re.compile(r"^cudart-llama-bin-win-%s(?:[.-][\d.]+)?-x64\.zip$" % re.escape(flavor))
        now = time.time()
        for r in releases:
            b = _build_no(r.get("tag_name"))
            if b is None or r.get("draft"):
                continue
            if settled and now - _ts(r.get("published_at")) < SETTLE:
                continue
            main = next((a for a in r.get("assets", []) if main_re.match(a["name"])), None)
            if not main:
                continue
            rt = next((a for a in r.get("assets", []) if rt_re.match(a["name"])), None)
            slim = lambda a: a and {"name": a["name"], "url": a["browser_download_url"], "size": a["size"],
                                    "sha256": (a.get("digest") or "").replace("sha256:", "").lower()}
            return {"tag": r["tag_name"], "build": b, "published": r.get("published_at"),
                    "url": r.get("html_url"), "main": slim(main), "runtime": slim(rt)}
        return None

    def check(self):
        with self.lock:
            if self.state["busy"]:
                return self.public()
            self.state.update(busy=True, phase="checking")
        try:
            rels = _get_json(RELEASES)
            fl = self.flavor()
            self.state["latest"] = self.pick(rels, fl)
            self.state["settled"] = self.pick(rels, fl, settled=True)
            self.refresh_local()
            self.state["error"] = None
        except Exception as e:
            self.state["error"] = "check failed: " + str(e)[:200]
        finally:
            self.state.update(checked=time.time(), busy=False, phase="")
        return self.public()

    # ------------------------------------------------------------ stage (download + verify + unpack)
    def _download(self, asset, dest):
        h = hashlib.sha256()
        req = urllib.request.Request(asset["url"], headers={"User-Agent": UA["User-Agent"]})
        done = 0
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                self.state["progress"] = {"file": asset["name"], "done": done, "total": asset["size"]}
        if asset["sha256"] and h.hexdigest() != asset["sha256"]:
            os.remove(dest)
            raise RuntimeError("checksum mismatch for %s; download deleted" % asset["name"])
        return bool(asset["sha256"])

    def _unzip(self, path, dest):
        root = os.path.normpath(dest) + os.sep
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                out = os.path.normpath(os.path.join(dest, n))
                if not out.startswith(root):
                    raise RuntimeError("zip has an unsafe path: " + n)
            z.extractall(dest)

    def stage(self, rel):
        """Gets `rel` ready in llama.cpp.next\\ without touching the running install. Returns (ok, msg)."""
        st = self.marker(self.next) if os.path.isdir(self.next) else {}
        if st.get("tag") == rel["tag"] and self.version_of(self.next):
            return True, "%s already downloaded" % rel["tag"]
        for d in (self.next, self.dl):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(self.dl)
        os.makedirs(self.next)
        try:
            self.state["phase"] = "downloading " + rel["tag"]
            zp = os.path.join(self.dl, rel["main"]["name"])
            verified = self._download(rel["main"], zp)
            self.state["phase"] = "unpacking " + rel["tag"]
            self._unzip(zp, self.next)
            # some releases put everything in a subfolder
            for dirpath, _, files in os.walk(self.next):
                if "llama-server.exe" in files and os.path.normpath(dirpath) != os.path.normpath(self.next):
                    for n in os.listdir(dirpath):
                        shutil.move(os.path.join(dirpath, n), os.path.join(self.next, n))
                    break
            rt = rel.get("runtime")
            have = self.marker(self.dir).get("runtime_sha256")
            if rt:
                if have and have == rt["sha256"]:
                    for n in os.listdir(self.dir):
                        if RUNTIME_DLLS.match(n):
                            shutil.copy2(os.path.join(self.dir, n), os.path.join(self.next, n))
                else:
                    self.state["phase"] = "downloading CUDA runtime"
                    rp = os.path.join(self.dl, rt["name"])
                    self._download(rt, rp)
                    self._unzip(rp, self.next)
            self.state["phase"] = "testing " + rel["tag"]
            v = self.version_of(self.next)
            if not v:
                raise RuntimeError("the new llama-server.exe didn't start (`--version` failed)")
            if v["build"] != rel["build"]:
                raise RuntimeError("downloaded build reports %s, expected %s" % (v["build"], rel["build"]))
            with open(os.path.join(self.next, MARKER), "w", encoding="utf-8") as f:
                json.dump({"tag": rel["tag"], "build": rel["build"], "commit": v["commit"],
                           "flavor": self.flavor(), "published": rel["published"], "verified": verified,
                           "runtime_sha256": (rt or {}).get("sha256") or have, "staged": time.time()}, f, indent=1)
            return True, "%s downloaded and verified" % rel["tag"]
        except Exception:
            shutil.rmtree(self.next, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(self.dl, ignore_errors=True)
            self.state["progress"] = None

    # ------------------------------------------------------------ swap
    def _swap(self, new_dir, old_to):
        """llama.cpp\\ -> old_to, new_dir -> llama.cpp\\. Fails cleanly while any llama-server.exe is running."""
        tmp = old_to + ".swap"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.rename(self.dir, tmp)
        except OSError:
            raise RuntimeError("llama.cpp\\ is in use (a llama-server or llama-cli from it is still running); "
                               "nothing was changed")
        try:
            os.rename(new_dir, self.dir)
        except OSError:
            os.rename(tmp, self.dir)
            raise RuntimeError("couldn't move the new build into place; the old one is back")
        shutil.rmtree(old_to, ignore_errors=True)
        os.rename(tmp, old_to)

    def _with_stopped(self, fn, restart_model):
        running = None
        if self.stop_all:
            ok, msg, running = self.stop_all()
            if not ok:
                raise RuntimeError(msg)
        try:
            return fn()
        finally:
            if restart_model and running and self.restart:
                try:
                    self.restart(running)
                except Exception as e:
                    self.state["error"] = "updated, but restarting the model failed: " + str(e)[:200]

    def install(self, rel=None, restart_model=True):
        """Download (if needed) and switch to `rel` (default: newest). Stops the model, restarts it after."""
        with self.lock:
            if self.state["busy"]:
                return False, "already busy: " + (self.state["phase"] or "working")
            self.state.update(busy=True, error=None)
        try:
            rel = rel or self.state["latest"]
            if not rel:
                rel = self._fresh_latest()
            if not rel:
                return False, "couldn't find a build to install"
            if not self._newer(rel) and not (self.state["installed"] is None):
                return False, "already on %s" % rel["tag"]
            self.stage(rel)
            self.state["phase"] = "switching to " + rel["tag"]
            old = (self.state["installed"] or {}).get("build")
            self._with_stopped(lambda: self._swap(self.next, self.prev), restart_model)
            self.refresh_local()
            msg = "llama.cpp b%s -> %s" % (old, rel["tag"]) if old else "installed " + rel["tag"]
            self.state["last_msg"] = msg
            return True, msg
        except Exception as e:
            self.state["error"] = str(e)[:300]
            return False, str(e)[:300]
        finally:
            self.state.update(busy=False, phase="")

    def rollback(self):
        with self.lock:
            if self.state["busy"]:
                return False, "already busy"
            self.state.update(busy=True, error=None)
        try:
            if not self.version_of(self.prev):
                return False, "there's no previous build to go back to"
            self.state["phase"] = "rolling back"
            back = os.path.join(self.root, "llama.cpp.back")
            shutil.rmtree(back, ignore_errors=True)
            os.rename(self.prev, back)
            try:
                self._with_stopped(lambda: self._swap(back, self.prev), True)
            except Exception:
                if os.path.isdir(back) and not os.path.isdir(self.prev):
                    os.rename(back, self.prev)
                raise
            self.refresh_local()
            msg = "rolled back to b%s" % (self.state["installed"] or {}).get("build")
            self.state["last_msg"] = msg
            return True, msg
        except Exception as e:
            self.state["error"] = str(e)[:300]
            return False, str(e)[:300]
        finally:
            self.state.update(busy=False, phase="")

    def _fresh_latest(self):
        self.state["busy"] = False
        self.check()
        self.state["busy"] = True
        return self.state["latest"]

    # ------------------------------------------------------------ automatic mode
    def swap_if_staged(self):
        """Called right before the command center launches a model (nothing from llama.cpp\\ is running then).
        Swaps in a downloaded build when auto-install is on. Never raises."""
        if not self.prefs().get("auto_install", True) or self.state["busy"]:
            return None
        st = self.marker(self.next) if os.path.isdir(self.next) else {}
        inst = self.state["installed"] or {}
        if not st.get("build") or st["build"] <= inst.get("build", 0):
            return None
        with self.lock:
            if self.state["busy"]:
                return None
            self.state.update(busy=True, phase="switching to " + st["tag"])
        try:
            if self.stop_all:
                ok, msg, _ = self.stop_all(embedder_only=True)
                if not ok:
                    return None
            self._swap(self.next, self.prev)
            self.refresh_local()
            self.state["last_msg"] = "auto-updated llama.cpp to " + st["tag"]
            return self.state["last_msg"]
        except Exception as e:
            self.state["error"] = "auto-update: " + str(e)[:200]
            return None
        finally:
            self.state.update(busy=False, phase="")

    def auto_once(self):
        self.check()
        if not self.prefs().get("auto_install", True):
            return
        rel = self.state.get("settled")
        if not rel or not self._newer(rel):
            return
        with self.lock:
            if self.state["busy"]:
                return
            self.state["busy"] = True
        try:
            self.stage(rel)
        except Exception as e:
            self.state["error"] = "auto-update download: " + str(e)[:200]
        finally:
            self.state.update(busy=False, phase="")
            self.refresh_local()
        if self.model_off and self.model_off():
            self.swap_if_staged()

    def watch(self):
        time.sleep(60)
        while True:
            if self.prefs().get("auto_check", True):
                try:
                    self.auto_once()
                except Exception as e:
                    self.state["error"] = str(e)[:200]
            time.sleep(CHECK_EVERY)
