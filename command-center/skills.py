"""Skills: instruction packs imported from GitHub that every local model can use.

A skill is a folder with a SKILL.md (the Agent Skills format Claude uses: YAML front matter with `name` and
`description`, then the instructions), plus any files it refers to (references, templates, examples).
Repos like anthropics/skills, Claude Code plugin marketplaces, or any repo with SKILL.md files work.

Import: paste a GitHub link (repo, a folder in it, or one SKILL.md). The repo is downloaded once as a zip from
codeload.github.com (no API, so no rate limit or token), every SKILL.md in it is listed, and you pick which to
install. They land in <command center root>\\skills\\<id>\\ with a skill.json recording where they came from, so
they can be updated later.

Use: the same way Claude does it, "progressive disclosure". The model sees only a short catalog (name + one-line
description of each enabled skill). When a task matches, it calls the `use_skill` tool and gets the full SKILL.md,
then can ask for any file the skill mentions. Cline gets the catalog with file paths and reads them itself.

Skills are text for the model to follow. Nothing in a skill is ever run here: scripts a skill ships are only
shown to the model as text, like any other file.
"""
import io
import json
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import fsutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "skills")
UA = {"User-Agent": "command-center-skills"}
MAX_ZIP = 150 * 1024 * 1024      # biggest repo archive downloaded
MAX_SKILL = 25 * 1024 * 1024     # biggest single skill folder installed (some bundle fonts)
MAX_FILES = 300                  # files per skill
FILE_CHARS = 30000               # characters of one file handed to the model (about 7,500 tokens)
DESC_CHARS = 300                 # catalog description length per skill
MAX_CALLS = 6                    # use_skill calls per reply
CACHE_SECONDS = 600

_ctx = ssl.create_default_context()
_lock = threading.Lock()
_zips = {}                       # (owner, repo, ref) -> (time, bytes, resolved_ref)

TOOL = {"type": "function", "function": {
    "name": "use_skill",
    "description": ("Load one of your skills: expert instructions for a kind of task. Call it BEFORE starting a task "
                    "that matches a skill in your list, then follow what it says. Without `file` you get the skill's "
                    "main instructions and a list of its files; with `file` you get that file (only when the "
                    "instructions point you to it)."),
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "the skill's name, exactly as in your list"},
        "file": {"type": "string", "description": "optional: a file inside the skill, e.g. reference/forms.md"}},
        "required": ["name"]}}}
ARG = {"use_skill": "name"}


# ---------------------------------------------------------------------------------------------- settings
def config(settings):
    c = settings.get("skills") or {}
    return {"enabled": bool(c.get("enabled", True)), "cline": bool(c.get("cline", True))}


# ---------------------------------------------------------------------------------------------- SKILL.md
def front_matter(text):
    """(meta dict, body) from a SKILL.md. Handles the small YAML subset skills use: key: value, quoted values,
    and folded/literal blocks (key: > / key: |). Unknown or nested keys are kept as raw strings or skipped."""
    text = text.lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    meta, lines, i = {}, m.group(1).splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", ln)
        i += 1
        if not km:
            continue
        key, val = km.group(1), km.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+") or (val == "" and i < len(lines) and lines[i][:1] in (" ", "\t")):
            block = []
            while i < len(lines) and (lines[i][:1] in (" ", "\t") or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            if val.startswith("|"):
                val = "\n".join(block).strip()
            else:
                val = " ".join(b for b in block if b).strip()
            if val.startswith("- ") or not val:   # a list or a nested map: not something the catalog needs
                meta.setdefault(key, val)
                continue
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace("\\\"", "\"") if val[0] == "\"" else val[1:-1].replace("''", "'")
        meta[key] = val
    return meta, text[m.end():]


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:64] or "skill"


# ---------------------------------------------------------------------------------------------- GitHub
def parse_source(text):
    """GitHub link -> (owner, repo, [path segments after /tree|blob/], kind). Accepts owner/repo, the repo URL,
    .../tree/<ref>/<folder>, .../blob/<ref>/<file>, raw.githubusercontent.com links, and a trailing .git."""
    t = (text or "").strip()
    if not t:
        raise ValueError("paste a GitHub link")
    if re.match(r"^[\w.-]+/[\w.-]+$", t):
        t = "https://github.com/" + t
    if not re.match(r"^https?://", t):
        t = "https://" + t
    u = urllib.parse.urlsplit(t)
    host = u.netloc.lower()
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if host == "raw.githubusercontent.com" and len(parts) >= 3:
        return parts[0], parts[1], parts[2:], "blob"
    if host not in ("github.com", "www.github.com") or len(parts) < 2:
        raise ValueError("that isn't a GitHub repo link (expected github.com/owner/repo)")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not re.match(r"^[\w.-]+$", owner) or not re.match(r"^[\w.-]+$", repo):
        raise ValueError("bad owner or repo name")
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        return owner, repo, parts[3:], parts[2]
    return owner, repo, [], "repo"


def _get(url, limit):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=_ctx) as r:
        n = r.headers.get("Content-Length")
        if n and n.isdigit() and int(n) > limit:
            raise ValueError("the repo download is too big (%d MB, limit %d MB)" % (int(n) >> 20, limit >> 20))
        buf = io.BytesIO()
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            buf.write(b)
            if buf.tell() > limit:
                raise ValueError("the repo download is too big (limit %d MB)" % (limit >> 20))
        return buf.getvalue()


def _archive(owner, repo, ref):
    key = (owner.lower(), repo.lower(), ref)
    with _lock:
        hit = _zips.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    url = "https://codeload.github.com/%s/%s/zip/%s" % (owner, repo, urllib.parse.quote(ref, safe="/"))
    data = _get(url, MAX_ZIP)
    with _lock:
        for k in [k for k, v in _zips.items() if time.time() - v[0] >= CACHE_SECONDS]:
            del _zips[k]
        _zips[key] = (time.time(), data)
    return data


def fetch(owner, repo, rest):
    """Download the repo zip. `rest` is the path after /tree/ or /blob/: branch names can contain slashes, so
    the ref is the shortest prefix GitHub accepts, and the remainder is the folder. -> (zipfile, ref, subpath)"""
    tries = [("HEAD", [])] if not rest else [("/".join(rest[:i]), rest[i:]) for i in range(1, len(rest) + 1)]
    last = None
    for ref, sub in tries:
        try:
            z = zipfile.ZipFile(io.BytesIO(_archive(owner, repo, ref)))
            return z, ref, "/".join(sub)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404:
                break
        except urllib.error.URLError as e:
            raise ValueError("couldn't reach GitHub (%s)" % e.reason)
    if last is not None and last.code == 404:
        raise ValueError("GitHub says %s/%s%s doesn't exist (or it's private)" % (owner, repo, " at " + "/".join(rest) if rest else ""))
    raise ValueError("GitHub download failed (%s)" % last)


def _entries(z):
    """{repo-relative path: ZipInfo} for regular files, dropping the archive's top folder (repo-ref/)."""
    out = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:   # symlink: never followed or installed
            continue
        name = info.filename.replace("\\", "/")
        if "/" not in name:
            continue
        rel = name.split("/", 1)[1]
        if not rel or rel.startswith("/") or any(p in ("..", "") for p in rel.split("/")) or ":" in rel:
            continue
        out[rel] = info
    return out


def scan(source):
    """List the skills in a GitHub link without installing anything."""
    owner, repo, rest, kind = parse_source(source)
    z, ref, sub = fetch(owner, repo, rest)
    files = _entries(z)
    if kind == "blob" and sub and not sub.endswith("SKILL.md"):
        sub = sub.rsplit("/", 1)[0] if "/" in sub else ""
    if sub.endswith("SKILL.md"):
        sub = sub.rsplit("/", 1)[0] if "/" in sub else ""
    pre = sub.strip("/") + "/" if sub.strip("/") else ""
    if pre and not any(p.startswith(pre) for p in files):
        raise ValueError("there's no folder %s in %s/%s" % (sub, owner, repo))
    have = {(s.get("source") or {}).get("path"): s["id"] for s in library()
            if (s.get("source") or {}).get("repo", "").lower() == ("%s/%s" % (owner, repo)).lower()}
    found = []
    for p in sorted(files):
        if not p.startswith(pre) or p.rsplit("/", 1)[-1] != "SKILL.md":
            continue
        folder = p.rsplit("/", 1)[0] if "/" in p else ""
        try:
            meta, body = front_matter(z.read(files[p]).decode("utf-8", "replace"))
        except (zipfile.BadZipFile, OSError):
            continue
        inside = [q for q in files if folder == "" and q == p or folder and q.startswith(folder + "/")]
        if folder == "":   # a SKILL.md at the repo root: the skill is just that file
            inside = [p]
        size = sum(files[q].file_size for q in inside)
        found.append({"path": folder, "name": str(meta.get("name") or (folder.rsplit("/", 1)[-1] if folder else repo)),
                      "description": str(meta.get("description") or _first_line(body))[:600],
                      "files": len(inside), "size": size, "installed": have.get(folder),
                      "too_big": size > MAX_SKILL or len(inside) > MAX_FILES})
    if not found:
        raise ValueError("no SKILL.md found in %s/%s%s" % (owner, repo, " under " + sub if sub else ""))
    return {"repo": "%s/%s" % (owner, repo), "ref": ref, "sub": sub, "skills": found}


def _first_line(body):
    for ln in body.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln:
            return ln
    return ""


def install(source, paths):
    """Install the picked skills (folder paths from scan()) from a GitHub link. Re-installing one replaces it."""
    owner, repo, rest, _ = parse_source(source)
    z, ref, _sub = fetch(owner, repo, rest)
    files = _entries(z)
    os.makedirs(ROOT, exist_ok=True)
    done, errors = [], []
    for folder in paths:
        folder = (folder or "").strip("/")
        md = (folder + "/SKILL.md") if folder else "SKILL.md"
        if md not in files:
            errors.append("%s: no SKILL.md there" % (folder or "(repo root)"))
            continue
        inside = [md] if not folder else [q for q in files if q.startswith(folder + "/")]
        if len(inside) > MAX_FILES or sum(files[q].file_size for q in inside) > MAX_SKILL:
            errors.append("%s: too big (limit %d files / %d MB)" % (folder, MAX_FILES, MAX_SKILL >> 20))
            continue
        meta, _ = front_matter(z.read(files[md]).decode("utf-8", "replace"))
        name = str(meta.get("name") or (folder.rsplit("/", 1)[-1] if folder else repo))
        src = {"repo": "%s/%s" % (owner, repo), "ref": ref, "path": folder}
        old = next((s for s in library() if s.get("source") == src), None)
        sid = old["id"] if old else _free_id(slug(name))
        dest = os.path.join(ROOT, sid)
        tmp = dest + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            for q in inside:
                rel = q[len(folder) + 1:] if folder else q
                out = os.path.normpath(os.path.join(tmp, *rel.split("/")))
                if not out.startswith(os.path.normpath(tmp) + os.sep):
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(files[q]) as r, open(out, "wb") as w:
                    shutil.copyfileobj(r, w)
            info = {"id": sid, "name": name, "description": str(meta.get("description") or "")[:1000],
                    "source": src, "installed": time.time(),
                    "enabled": old.get("enabled", True) if old else True}
            fsutil.write_json(os.path.join(tmp, "skill.json"), info, indent=1)
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(tmp, dest)
            done.append(sid)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            errors.append("%s: %s" % (name, e))
    return done, errors


def _free_id(base):
    ids = {s["id"] for s in library()}
    sid, n = base, 2
    while sid in ids or os.path.exists(os.path.join(ROOT, sid)):
        sid, n = "%s-%d" % (base, n), n + 1
    return sid


# ---------------------------------------------------------------------------------------------- library
def library():
    out = []
    try:
        names = sorted(os.listdir(ROOT))
    except OSError:
        return out
    for d in names:
        p = os.path.join(ROOT, d, "skill.json")
        if d.endswith(".new") or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        s["id"] = d
        out.append(s)
    return out


def get(sid):
    return next((s for s in library() if s["id"] == sid), None)


def set_enabled(sid, on):
    s = get(sid)
    if not s:
        return False
    s["enabled"] = bool(on)
    fsutil.write_json(os.path.join(ROOT, sid, "skill.json"), s, indent=1)
    return True


def remove(sid):
    if not get(sid):
        return False
    shutil.rmtree(os.path.join(ROOT, sid), ignore_errors=True)
    return not os.path.exists(os.path.join(ROOT, sid))


def update(sid):
    """Re-download a skill from where it came from (same repo, branch and folder)."""
    s = get(sid)
    if not s or not s.get("source"):
        return False, "no source recorded"
    src = s["source"]
    link = "https://github.com/%s/tree/%s/%s" % (src["repo"], src["ref"], src["path"]) if src.get("ref") != "HEAD" \
        else "https://github.com/%s" % src["repo"]
    with _lock:   # always fresh
        for k in [k for k in _zips if "%s/%s" % (k[0], k[1]) == src["repo"].lower()]:
            del _zips[k]
    done, errors = install(link, [src["path"]])
    return bool(done), "; ".join(errors) or "updated"


def enabled():
    return [s for s in library() if s.get("enabled", True)]


def files_of(sid):
    base = os.path.join(ROOT, sid)
    out = []
    for dp, dn, fn in os.walk(base):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), base).replace("\\", "/")
            if rel not in ("skill.json",):
                out.append(rel)
    return sorted(out)


def status(settings):
    lib = library()
    for s in lib:
        s["files"] = len(files_of(s["id"]))
    return dict(config(settings), skills=lib, folder=ROOT)


# ---------------------------------------------------------------------------------------------- for models
def _find(name):
    name = (name or "").strip().lower()
    on = enabled()
    return next((s for s in on if s["name"].lower() == name or s["id"] == name), None) or \
        next((s for s in on if slug(s["name"]) == slug(name)), None)


def catalog(settings, paths=False):
    """The short list the model sees. With paths, each entry carries its SKILL.md path (for Cline, which reads
    files itself)."""
    if not config(settings)["enabled"]:
        return ""
    on = enabled()
    if not on:
        return ""
    lines = []
    for s in on:
        d = re.sub(r"\s+", " ", s.get("description") or "").strip()
        d = d[:DESC_CHARS] + ("…" if len(d) > DESC_CHARS else "")
        lines.append("- %s: %s%s" % (s["name"], d, " (%s)" % os.path.join(ROOT, s["id"], "SKILL.md") if paths else ""))
    return "\n".join(lines)


def inject_cline(messages, cat):
    """Put the skill list (with file paths) at the start of Cline's first user message, the task, so every step
    of the task sends the same prefix. Cline reads the SKILL.md itself with read_file when a skill fits."""
    idx = next((i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
    if idx is None:
        return messages
    block = ("<skills>\nSkills installed on this PC: expert instructions for certain tasks. When the task matches "
             "one, read its SKILL.md (path in brackets) with read_file first and follow it; it may point to more "
             "files next to it. Otherwise ignore this list.\n%s\n</skills>" % cat)
    m = dict(messages[idx])
    c = m.get("content")
    if isinstance(c, list):
        m["content"] = [{"type": "text", "text": block}] + c
    else:
        m["content"] = block + "\n\n" + (c or "")
    out = list(messages)
    out[idx] = m
    return out


def tools_for(settings):
    return [TOOL] if config(settings)["enabled"] and enabled() else []


def run(args):
    """use_skill -> {"ok", "text", "sources", "error"} like the web tools."""
    s = _find(args.get("name"))
    if not s:
        names = ", ".join(x["name"] for x in enabled()) or "none"
        return {"ok": False, "error": "no skill called %r (you have: %s)" % (args.get("name"), names), "sources": []}
    base = os.path.join(ROOT, s["id"])
    want = (args.get("file") or "").strip().replace("\\", "/").lstrip("/") or "SKILL.md"
    if want.startswith("./"):
        want = want[2:]
    path = os.path.normpath(os.path.join(base, *want.split("/")))
    if not path.startswith(os.path.normpath(base) + os.sep) or want == "skill.json":
        return {"ok": False, "error": "that file isn't part of the skill", "sources": []}
    if not os.path.isfile(path):
        return {"ok": False, "error": "no file %s in skill %s (files: %s)" % (want, s["name"], ", ".join(files_of(s["id"])[:40])),
                "sources": []}
    with open(path, "rb") as f:
        raw = f.read(FILE_CHARS * 4 + 1)
    if b"\x00" in raw[:4096]:
        return {"ok": False, "error": "%s is a binary file; it can't be read as text" % want, "sources": []}
    text = raw.decode("utf-8", "replace")
    cut = len(text) > FILE_CHARS
    text = text[:FILE_CHARS] + ("\n\n[... cut at %d characters]" % FILE_CHARS if cut else "")
    if want == "SKILL.md":
        _, body = front_matter(text)
        others = [f for f in files_of(s["id"]) if f != "SKILL.md"]
        text = "# Skill: %s\n\n%s" % (s["name"], body.strip())
        if others:
            text += "\n\n---\nFiles in this skill (load one with use_skill name=%r file=<path> when the instructions " \
                    "point to it; scripts can't be run here, only read):\n%s" % (s["name"], "\n".join("- " + f for f in others[:80]))
    return {"ok": True, "text": text, "sources": [], "provider": "skill " + s["name"]}
