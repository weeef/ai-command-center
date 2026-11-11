"""Saved chats for the 1:llm chat pane.

One JSON file per chat in command-center\\chats\\ (never committed: it's personal). The browser only ever
names a chat by its id, which must match ID_RE, so a request can't reach any other file.
"""
import json
import os
import re
import threading
import time

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chats")
ID_RE = re.compile(r"^[a-z0-9-]{6,40}$")
_lock = threading.Lock()


def _path(cid):
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None
    return os.path.join(DIR, cid + ".json")


def list_chats():
    out = []
    try:
        names = os.listdir(DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(DIR, n), encoding="utf-8") as f:
                c = json.load(f)
            out.append({"id": c["id"], "title": c.get("title") or "untitled", "updated": c.get("updated", 0),
                        "count": len(c.get("messages") or []), "model": c.get("model", "")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out


def get_chat(cid):
    p = _path(cid)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_chat(data):
    p = _path(data.get("id"))
    msgs = data.get("messages")
    if not p or not isinstance(msgs, list):
        return False, "bad chat"
    title = str(data.get("title") or "untitled").strip()[:120]
    chat = {"id": data["id"], "title": title, "model": str(data.get("model") or "")[:80],
            "created": data.get("created") or int(time.time()), "updated": int(time.time()), "messages": msgs}
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(chat, f, ensure_ascii=False)
        os.replace(tmp, p)  # atomic, so a crash mid-write never leaves a half chat
    return True, "saved"


def delete_chat(cid):
    p = _path(cid)
    if not p:
        return False, "bad id"
    try:
        os.remove(p)
    except OSError:
        return False, "not found"
    return True, "deleted"


def rename_chat(cid, title):
    c = get_chat(cid)
    if not c:
        return False, "not found"
    c["title"] = str(title or "").strip()[:120] or c.get("title") or "untitled"
    return save_chat(c)


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
