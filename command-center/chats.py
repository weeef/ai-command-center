"""Saved chats for the 1:llm chat pane.

One JSON file per chat in <memory folder>\\chats\\ (never committed: it's personal). The browser only ever
names a chat by its id, which must match ID_RE, so a request can't reach any other file.
"""
import json
import os
import re
import threading
import time

import fsutil

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chats")   # memory.py points this at its folder
ID_RE = re.compile(r"^[a-z0-9-]{6,40}$")
_lock = threading.Lock()
ON_SAVE = None     # memory indexes every saved chat
ON_DELETE = None
_index = {}        # file path -> (mtime_ns, size, summary): listing thousands of imported chats stays fast


def _path(cid):
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None
    return os.path.join(DIR, cid + ".json")


def _summary(c):
    upd = c.get("updated", 0)
    return {"id": c["id"], "title": c.get("title") or "untitled", "updated": upd if isinstance(upd, (int, float)) else 0,
            "count": len(c.get("messages") or []), "model": c.get("model", "")}


def list_chats():
    """Every saved chat, newest first. Only files that changed since the last call are read again."""
    out, seen = [], set()
    try:
        entries = list(os.scandir(DIR))
    except OSError:
        return out
    for e in entries:
        if not e.name.endswith(".json") or not ID_RE.match(e.name[:-5]):
            continue
        try:
            st = e.stat()
            hit = _index.get(e.path)
            if not hit or hit[0] != st.st_mtime_ns or hit[1] != st.st_size:
                with open(e.path, encoding="utf-8") as f:
                    hit = (st.st_mtime_ns, st.st_size, _summary(json.load(f)))
                _index[e.path] = hit
            out.append(dict(hit[2]))
            seen.add(e.path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    for p in [p for p in _index if p not in seen and os.path.dirname(p) == DIR]:
        _index.pop(p, None)
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out


def get_chat(cid):
    p = _path(cid)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else None
    except (OSError, ValueError):
        return None


def _write(p, chat):
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        fsutil.write_json(p, chat, ensure_ascii=False)  # atomic, so a crash mid-write never leaves a half chat


def save_chat(data):
    p = _path(data.get("id"))
    msgs = data.get("messages")
    if not p or not isinstance(msgs, list):
        return False, "bad chat"
    title = str(data.get("title") or "untitled").strip()[:120]
    created = data.get("created")
    chat = {"id": data["id"], "title": title, "model": str(data.get("model") or "")[:80],
            "created": created if isinstance(created, (int, float)) and created else int(time.time()),
            "updated": int(time.time()), "messages": [m for m in msgs if isinstance(m, dict)]}
    try:
        _write(p, chat)
    except OSError:
        return False, "couldn't write to %s (is the memory drive connected?)" % DIR
    if ON_SAVE:
        try:
            ON_SAVE(chat)
        except Exception:
            pass
    return True, "saved"


def write_raw(chat):
    """Store an imported chat as-is (keeps its own dates), without the save hooks."""
    p = _path(chat.get("id"))
    if not p:
        return False
    _write(p, chat)
    return True


def delete_chat(cid):
    p = _path(cid)
    if not p:
        return False, "bad id"
    try:
        os.remove(p)
    except OSError:
        return False, "not found"
    if ON_DELETE:
        try:
            ON_DELETE(cid)
        except Exception:
            pass
    return True, "deleted"


def rename_chat(cid, title):
    c = get_chat(cid)
    if not c:
        return False, "not found"
    c["title"] = str(title or "").strip()[:120] or c.get("title") or "untitled"
    # a rename isn't a new message: the chat keeps its place in the history list
    try:
        _write(_path(cid), c)
    except OSError:
        return False, "couldn't write to %s (is the memory drive connected?)" % DIR
    if ON_SAVE:
        try:
            ON_SAVE(c)
        except Exception:
            pass
    return True, "saved"


def prune(days, keep=""):
    """Delete chats not updated in `days` days (0 = all), never the one named by `keep` (the open chat)."""
    try:
        days = max(0, float(days))
    except (TypeError, ValueError):
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for c in list_chats():
        if c["id"] == keep or (days and c["updated"] >= cutoff):
            continue
        if delete_chat(c["id"])[0]:
            n += 1
    return n
