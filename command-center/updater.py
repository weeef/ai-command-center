"""Updates: finds out whether GitHub has a newer version of the command center, and installs it when asked.

Two kinds of install:
- git checkout (git clone): `git fetch`, then a fast-forward merge. Refuses when you have uncommitted changes to
  tracked files or local commits that aren't on GitHub, so it can never throw work away.
- ZIP download (no .git folder): compares each file's git blob hash with the repo's file list from the GitHub API,
  then downloads the repo ZIP and writes only the files that changed. Old copies are kept as *.bak.
  Files that aren't in the repo (settings, api key, models, chats, memory) are never touched.

Nothing installs by itself: the app checks in the background and shows "update"; you choose when.
After an update the server restarts itself (Python code only loads at start); the page reloads when it's back.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

REPO = "weeef/ai-command-center"
BRANCH = "main"
API = "https://api.github.com/repos/" + REPO
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
UA = {"User-Agent": "command-center-updater", "Accept": "application/vnd.github+json"}
NOWIN = 0x08000000
CHECK_EVERY = 6 * 3600


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _blob_shas(path):
    """Git's hash of the file as it is, and with Windows line endings turned back into LF (an unzip tool or git's
    autocrlf may have converted them; that alone isn't a change)."""
    with open(path, "rb") as f:
        data = f.read()
    out = {hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}
    if b"\r\n" in data:
        lf = data.replace(b"\r\n", b"\n")
        out.add(hashlib.sha1(b"blob %d\0" % len(lf) + lf).hexdigest())
    return out


class Updater:
    def __init__(self, root, settings, save_settings):
        self.root = root
        self.settings = settings
        self.save_settings = save_settings
        self.lock = threading.Lock()
        self.state = {"checked": 0, "available": False, "commits": [], "error": None, "busy": False,
                      "mode": self.mode(), "current": None, "latest": None, "behind": 0, "ahead": 0,
                      "dirty": False, "files": []}

    # ------------------------------------------------------------ helpers
    def mode(self):
        return "git" if os.path.isdir(os.path.join(self.root, ".git")) and shutil.which("git") else "zip"

    def git(self, *args, timeout=60):
        r = subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=NOWIN, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "git failed").strip().splitlines()[-1])
        return r.stdout.strip()

    def prefs(self):
        p = self.settings.get("update")
        return p if isinstance(p, dict) else {}

    def set_prefs(self, **kw):
        p = dict(self.prefs())
        p.update(kw)
        self.settings["update"] = p
        self.save_settings()

    def public(self):
        s = dict(self.state)
        skip = self.prefs().get("skip")
        s["skipped"] = bool(skip and skip == s.get("latest"))
        s["auto_check"] = self.prefs().get("auto_check", True)
        s["files"] = s["files"][:40]
        return s

    # ------------------------------------------------------------ check
    def check(self):
        with self.lock:
            if self.state["busy"]:
                return self.public()
            self.state["busy"] = True
        try:
            out = self.check_git() if self.state["mode"] == "git" else self.check_zip()
            self.state.update(out, error=None)
        except Exception as e:  # network down, rate limit, git missing: say so, try again next time
            self.state["error"] = str(e)[:300]
        finally:
            self.state["checked"] = time.time()
            self.state["busy"] = False
        return self.public()

    def check_git(self):
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            raise RuntimeError("the repo is on a detached HEAD; check out a branch to get updates")
        upstream = f"origin/{branch}"
        self.git("fetch", "--quiet", "origin", branch, timeout=90)
        behind = int(self.git("rev-list", "--count", f"HEAD..{upstream}") or 0)
        ahead = int(self.git("rev-list", "--count", f"{upstream}..HEAD") or 0)
        log = self.git("log", f"HEAD..{upstream}", "-n", "30", "--format=%h%x1f%cI%x1f%s") if behind else ""
        commits = [dict(zip(("sha", "date", "msg"), l.split("\x1f"))) for l in log.splitlines() if l]
        files = self.git("diff", "--name-only", f"HEAD...{upstream}").splitlines() if behind else []
        dirty = bool(self.git("status", "--porcelain", "--untracked-files=no"))
        return {"available": behind > 0, "behind": behind, "ahead": ahead, "dirty": dirty, "commits": commits,
                "files": files, "current": self.git("rev-parse", "--short", "HEAD"),
                "latest": self.git("rev-parse", "--short", upstream), "branch": branch}

    def remote_tree(self):
        head = json.loads(_get(f"{API}/commits/{BRANCH}"))
        tree = json.loads(_get(f"{API}/git/trees/{head['sha']}?recursive=1"))
        files = {t["path"]: t["sha"] for t in tree.get("tree", []) if t.get("type") == "blob"}
        return head, files

    def changed_files(self, files):
        out = []
        for path, sha in files.items():
            local = os.path.join(self.root, *path.split("/"))
            try:
                if sha not in _blob_shas(local):
                    out.append(path)
            except OSError:
                out.append(path)
        return out

    def check_zip(self):
        head, files = self.remote_tree()
        changed = self.changed_files(files)
        installed = self.prefs().get("installed") or ""
        head_c = {"sha": head["sha"][:7], "date": head["commit"]["committer"]["date"],
                  "msg": head["commit"]["message"].splitlines()[0]}
        commits = []
        if changed and not installed:
            commits = [head_c]  # unknown which version this copy came from: show the newest only
        elif changed:
            try:
                for c in json.loads(_get(f"{API}/commits?sha={BRANCH}&per_page=15")):
                    if c["sha"][:7] == installed[:7]:
                        break
                    commits.append({"sha": c["sha"][:7], "date": c["commit"]["committer"]["date"],
                                    "msg": c["commit"]["message"].splitlines()[0]})
            except Exception:
                commits = [head_c]
        return {"available": bool(changed), "behind": len(commits) or (1 if changed else 0), "ahead": 0,
                "dirty": False, "commits": commits, "files": changed,
                "current": installed[:7] or None, "latest": head["sha"][:7]}

    def watch(self):
        """Background check soon after start, then every few hours (only if auto-check is on)."""
        time.sleep(20)
        while True:
            if self.prefs().get("auto_check", True):
                self.check()
            time.sleep(CHECK_EVERY)

    # ------------------------------------------------------------ apply
    def apply(self):
        """Installs the update. Returns (ok, message). The caller restarts the server on success."""
        with self.lock:
            if self.state["busy"]:
                return False, "already checking or updating"
            self.state["busy"] = True
        try:
            if self.state["mode"] == "git":
                return self.apply_git()
            return self.apply_zip()
        except Exception as e:
            return False, str(e)[:300]
        finally:
            self.state["busy"] = False

    def apply_git(self):
        s = self.check_git()
        if not s["behind"]:
            return False, "already up to date"
        if s["dirty"]:
            return False, "you have uncommitted changes in the repo; commit or stash them first (nothing was changed)"
        if s["ahead"]:
            return False, (f"this copy has {s['ahead']} commit(s) that aren't on GitHub; push or rebase first "
                           "(nothing was changed)")
        self.git("merge", "--ff-only", "--quiet", f"origin/{s['branch']}", timeout=120)
        self.state.update(available=False, behind=0, commits=[], files=[], current=s["latest"])
        return True, f"updated to {s['latest']}"

    def apply_zip(self):
        head, files = self.remote_tree()
        changed = self.changed_files(files)
        if not changed:
            return False, "already up to date"
        z = zipfile.ZipFile(io.BytesIO(_get(ZIP_URL, timeout=120)))
        prefix = z.namelist()[0].split("/")[0] + "/"
        # write every file to a temp name first, then swap them all in, so a failed download changes nothing
        staged = []
        for path in changed:
            dest = os.path.normpath(os.path.join(self.root, *path.split("/")))
            if not dest.startswith(os.path.normpath(self.root) + os.sep):
                continue  # never write outside the install folder
            data = z.read(prefix + path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".new", "wb") as f:
                f.write(data)
            staged.append(dest)
        for dest in staged:
            if os.path.exists(dest):
                shutil.copy2(dest, dest + ".bak")
            os.replace(dest + ".new", dest)
        self.set_prefs(installed=head["sha"], skip=None)
        self.state.update(available=False, behind=0, commits=[], files=[], current=head["sha"][:7])
        return True, f"updated {len(staged)} file(s) to {head['sha'][:7]}"
