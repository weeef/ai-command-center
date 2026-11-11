"""Read conversation exports from cloud chat services into the command center's own chat format.

Supported (detected automatically, from a .zip export or a bare .json):
  - ChatGPT  "Export data" zip -> conversations.json (each conversation is a tree in "mapping"; the shown branch
             is walked from "current_node" back to the root)
  - Claude   "Export data" zip -> conversations.json (each conversation has "chat_messages" with sender human/assistant)
  - any file whose name contains "memor" (e.g. a memories.json) holding a list of strings or {content|text|memory}
  - this app's own memory export (format "command-center-memory")

Everything returns plain dicts: {"id", "title", "created", "updated", "messages": [{"role", "content"}]}.
Images, files, tool calls and hidden system messages are skipped; only the visible text of the user and the
assistant is kept.
"""
import datetime
import hashlib
import json
import re
import zipfile


def _ts(v):
    """epoch seconds from a float, an int, or an ISO-8601 string."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _cid(prefix, raw):
    """A chat id the app accepts ([a-z0-9-], at most 40), stable across re-imports so nothing duplicates."""
    h = re.sub(r"[^a-z0-9]", "", str(raw).lower())
    if len(h) < 12:
        h = hashlib.sha1(str(raw).encode()).hexdigest()
    return (prefix + h)[:40]


def _merge(msgs):
    """Join consecutive messages from the same speaker (tool-using replies arrive in pieces)."""
    out = []
    for m in msgs:
        t = (m["content"] or "").strip()
        if not t:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + t
        else:
            out.append({"role": m["role"], "content": t})
    return out


# ---------------------------------------------------------------------------------------------- ChatGPT
def _gpt_text(content):
    if not isinstance(content, dict):
        return ""
    ct = content.get("content_type")
    if ct in ("text", "multimodal_text"):
        return "\n".join(p for p in (content.get("parts") or []) if isinstance(p, str))
    if ct == "user_editable_context":  # custom instructions
        return ""
    return ""


def chatgpt(convs):
    for c in convs:
        mapping = c.get("mapping") or {}
        node = c.get("current_node")
        if node not in mapping:  # no pointer: take the newest leaf
            leaves = [k for k, v in mapping.items() if not (v or {}).get("children")]
            node = max(leaves, key=lambda k: _ts(((mapping[k] or {}).get("message") or {}).get("create_time")), default=None)
        chain, seen = [], set()
        while node and node in mapping and node not in seen:
            seen.add(node)
            chain.append(mapping[node])
            node = (mapping[node] or {}).get("parent")
        msgs = []
        for n in reversed(chain):
            m = (n or {}).get("message") or {}
            role = (m.get("author") or {}).get("role")
            meta = m.get("metadata") or {}
            if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation"):
                continue
            if m.get("recipient") not in (None, "all"):  # assistant -> tool calls
                continue
            msgs.append({"role": role, "content": _gpt_text(m.get("content"))})
        msgs = _merge(msgs)
        if msgs:
            yield {"id": _cid("gpt-", c.get("conversation_id") or c.get("id") or c.get("title", "") + str(c.get("create_time"))),
                   "title": "ChatGPT · " + (c.get("title") or "untitled"), "created": _ts(c.get("create_time")),
                   "updated": _ts(c.get("update_time") or c.get("create_time")), "messages": msgs}


# ---------------------------------------------------------------------------------------------- Claude
def _claude_text(m):
    parts = m.get("content")
    if isinstance(parts, list):
        t = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))
        if t.strip():
            return t
    return m.get("text") or ""


def claude(convs):
    for c in convs:
        msgs = []
        for m in c.get("chat_messages") or []:
            role = {"human": "user", "user": "user", "assistant": "assistant"}.get(m.get("sender"))
            if role:
                msgs.append({"role": role, "content": _claude_text(m)})
        msgs = _merge(msgs)
        if msgs:
            yield {"id": _cid("cl-", c.get("uuid") or c.get("id") or c.get("name", "") + str(c.get("created_at"))),
                   "title": "Claude · " + (c.get("name") or "untitled"), "created": _ts(c.get("created_at")),
                   "updated": _ts(c.get("updated_at") or c.get("created_at")), "messages": msgs}


# ---------------------------------------------------------------------------------------------- detection
def _memory_strings(data):
    """Saved memories in whatever shape: list of strings, list of dicts, or {"memories": [...]}."""
    if isinstance(data, dict):
        for k in ("memories", "memory", "items", "data"):
            if isinstance(data.get(k), list):
                return _memory_strings(data[k])
        return []
    out = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k in ("content", "text", "memory", "value"):
                if isinstance(x.get(k), str):
                    out.append(x[k])
                    break
    return [s.strip() for s in out if s and s.strip()]


def detect(data, name=""):
    """-> ("chatgpt"|"claude"|"memories"|"own"|None, payload)"""
    if isinstance(data, dict) and data.get("format") == "command-center-memory":
        return "own", data
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        data = data["conversations"]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "mapping" in data[0]:
            return "chatgpt", data
        if "chat_messages" in data[0]:
            return "claude", data
    if "memor" in name.lower():
        mem = _memory_strings(data)
        if mem:
            return "memories", mem
    return None, None


def read_any(path):
    """Yield (kind, payload, name) for everything importable in a .zip or .json file."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                n = info.filename
                if not n.lower().endswith(".json") or info.file_size > 4 * 1024 ** 3:
                    continue
                base = n.rsplit("/", 1)[-1].lower()
                if base not in ("conversations.json",) and "memor" not in base:
                    continue
                try:
                    with z.open(info) as f:
                        data = json.load(f)
                except (ValueError, UnicodeDecodeError):
                    continue
                kind, payload = detect(data, base)
                if kind:
                    yield kind, payload, n
        return
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8-sig"))
    kind, payload = detect(data, path)
    if kind:
        yield kind, payload, path


def conversations(kind, payload):
    return chatgpt(payload) if kind == "chatgpt" else claude(payload) if kind == "claude" else iter(())
