"""Long-term memory shared by every model the command center talks to.

Everything lives in ONE folder (the "memory home", settings.json -> memory_dir), so moving the AI's memory to
another drive is copying one folder:

    <home>/memory.db   SQLite: memories (plain text), chat snippets, the search index, embedding vectors
    <home>/chats/      saved chats (one JSON per chat)
    <home>/README.txt  what this folder is

Memories are plain sentences, never tied to a chat model: switch models (standard, uncensored, the laptop, a future
one) and they all read the same memory. Vectors are tagged with the embedding model that made them; change that
model and the index rebuilds itself in the background from the text.

How it works:
  recall  - before each chat request, the newest user message is matched against memories (meaning, via a small
            embedding model on the CPU, plus keywords via SQLite FTS5) and against snippets of other chats; the best
            matches are put in front of that message as a <memory> block. The block a message was sent with is
            stored, so later turns resend the identical text and the model server's prompt cache keeps working,
            also after the command center was restarted.
  learn   - after a reply is saved, the active model is asked (in the background, when it's idle) to pull durable
            facts out of the exchange: add new memories, update ones that changed, delete ones proven wrong.
  tidy    - when nothing else is waiting, near-duplicate memories are shown to the model to merge, and events whose
            date has long passed are archived. Archived memories are kept and can be restored; they just stop
            being recalled. Nothing is ever deleted by tidying.

The thresholds below were tuned with bench/memory_eval.py (a fixed set of memories and questions); run it again
after changing them or the embedding model.
"""
import array
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time

import importers

try:
    import numpy as np
except ImportError:  # still works, just slower on very large memories
    np = None

TOPICS = ("you", "preference", "project", "people", "fact", "event", "howto", "other")
EMBED_PORT = 8091
NOWIN = 0x08000000
QUERY_INSTRUCT = ("Instruct: Given a message from the user, retrieve memories about them and earlier conversation "
                  "snippets that help answer it\nQuery: ")
# Recall settings. Scores are cosine similarity for the embedding model (Qwen3-Embedding-0.6B).
RECALL = {
    "mem_min": 0.36,      # below this a memory is noise
    "rel_gap": 0.20,      # ...and it must be within this of the best match, which cuts the long tail
    "kw_bonus": 0.06,     # added when the memory also shares a distinctive word with the message
    "k": 8,               # memories per message at most (plus pinned ones)
    "ctx_penalty": 0.04,  # a match found only via the previous turn counts a little less
    "chunk_min": 0.45,    # old-chat snippets need a clearer match than memories
    "chunks": 3,          # snippets per message at most, one per chat
    "chunk_chars": 700,   # each snippet is cut to this
    "dup": 0.90,          # two recalled memories this similar say the same thing: only the better one is sent
}
MAX_BLOCK = 6000   # characters (~1,500 tokens) of recalled memory per request, at most
MEM_BUDGET = 3600  # of which the memories themselves may take this much; snippets get the rest
SAME = 0.93        # a new memory this close to an existing one is the same fact: the existing one is refreshed
TIDY_SIM = 0.80    # memories at least this similar are shown to the model as possible duplicates
EVENT_STALE_DAYS = 45   # an event whose date passed this long ago stops being recalled (archived)
README = """This folder is the long-term memory of the local AI command center.

memory.db  - every memory (plain text) plus the search index. Any model can use it.
chats/     - saved chats.

To move it: in the command center open 6:set -> location, and pick the new folder (it copies everything and
switches over). Or close the command center, copy this whole folder anywhere, then use "use existing" there.
Back it up by copying the folder. Deleting it erases the AI's memory.
"""


def _now():
    return time.time()


def _vec_bytes(v):
    return array.array("f", v).tobytes()


def _vec_from(b):
    a = array.array("f")
    a.frombytes(b)
    return a


def _norm(v):
    s = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / s for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _words(text):
    out = []
    for w in re.findall(r"[\w']{3,}", (text or "").lower()):
        w = w.strip("'")
        if w and w not in out and w not in _STOP:
            out.append(w)
    return out


def fts_query(text):
    return " OR ".join('"%s"' % w.replace('"', "") for w in _words(text)[:16])


_STOP = set("the and for you are was were what when where which who why how this that with have has had not but "
            "your from they them their there then than can could would should will just about into out all any "
            "its it's i'm i've don't doesn't did does get got make made want like also some more most very "
            "please thanks thank need help tell know think good best use using used one two way much many "
            "user user's".split())


# ---------------------------------------------------------------------------------------------- embedding server
_job = None


def _tie_to_parent(proc):
    """Put a child process in a Windows job object that closes when this process ends, however it ends (idle exit,
    crash, Task Manager, log-off). Without it a killed command center would leave the embedding server running."""
    global _job
    try:
        import ctypes
        from ctypes import wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job is None:
            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k.CreateJobObjectW.restype = wt.HANDLE
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
            k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
            h = k.CreateJobObjectW(None, None)
            info = Extended()
            info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not h or not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return False
            _job = h   # never closed on purpose: Windows closes it when this process is gone
        k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        return bool(k.AssignProcessToJobObject(_job, int(proc._handle)))
    except Exception:
        return False


def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.4).close()
        return True
    except OSError:
        return False


class Embedder:
    """A small embedding model in its own llama-server on 127.0.0.1:8091, CPU only, so it never takes VRAM from the
    chat model. Started on first use, stopped when the command center exits (see _tie_to_parent)."""

    def __init__(self, exe, model_path, port=EMBED_PORT):
        self.exe, self.model_path, self.proc = exe, model_path, None
        self.port = port
        self.lock = threading.Lock()
        self.last_start = 0
        self.error = None
        self.vetted = False   # whatever already listens on the port has been looked at (see vet)

    @property
    def name(self):
        return os.path.basename(self.model_path or "") or "none"

    def available(self):
        return bool(self.model_path) and os.path.exists(self.model_path) and os.path.exists(self.exe)

    def _get(self, path, timeout=1.0):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def ready(self):
        if not self.vetted and self.proc is None:
            return False
        try:
            return self._get("/health")[0] == 200
        except (OSError, http.client.HTTPException):
            return False

    def vet(self):
        """Once, at startup: decide what to do about a server that already listens on our port.
          - our embedding model, left behind by a command center that was killed -> stop it, start a fresh one
          - our embedding model, owned by another running command center        -> share it, never stop it
          - anything else (another program)                                      -> use the next free port instead
        """
        try:
            if not _port_open(self.port):
                return
            ours = False
            try:
                st, body = self._get("/props", timeout=3)
                ours = st == 200 and os.path.basename(str(json.loads(body).get("model_path") or "")) == self.name
            except (OSError, ValueError, AttributeError, http.client.HTTPException):
                pass
            if not ours:
                for p in range(self.port + 1, self.port + 6):
                    if not _port_open(p):
                        self.port = p
                        return
                self.error = "ports %d-%d are all taken by other programs" % (self.port, self.port + 5)
                return
            ps = ("$c=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %d -State Listen -EA SilentlyContinue|Select -First 1;"
                  "if(!$c){'{}';exit};$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\";"
                  "$q=Get-Process -Id $p.ParentProcessId -EA SilentlyContinue;"
                  "@{pid=$p.ProcessId;exe=$p.ExecutablePath;parent=[bool]$q}|ConvertTo-Json -Compress" % self.port)
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=20, creationflags=NOWIN).stdout.strip()
            info = json.loads(out or "{}")
            if (info.get("pid") and not info.get("parent")
                    and os.path.normcase(info.get("exe") or "") == os.path.normcase(self.exe)):
                subprocess.run(["taskkill", "/PID", str(int(info["pid"])), "/F"], capture_output=True, timeout=15,
                               creationflags=NOWIN)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        finally:
            self.vetted = True

    def ensure(self):
        """Start the server if it isn't up. Returns True once it answers."""
        if self.ready():
            return True
        if not self.vetted:
            return False
        if not self.available():
            self.error = "embedding model not found: " + (self.model_path or "(none set)")
            return False
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False  # still loading
            if _now() - self.last_start < 20 or _port_open(self.port):
                return False  # just started, or someone else's server is still coming up on the port
            self.last_start = _now()
            env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")  # CPU only: the GPU belongs to the chat model
            threads = str(min(6, max(2, (os.cpu_count() or 8) // 4)))
            try:
                self.proc = subprocess.Popen(
                    [self.exe, "-m", self.model_path, "--embedding", "--pooling", "last",
                     "--host", "127.0.0.1", "--port", str(self.port), "-ngl", "0",
                     "-c", "8192", "-b", "8192", "-ub", "8192", "-np", "1", "-t", threads, "--no-ui"],
                    env=env, creationflags=NOWIN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.error = "couldn't start the embedding server: %s" % e
                return False
            _tie_to_parent(self.proc)
            self.error = None
        return False

    def embed(self, texts, query=False, timeout=60):
        if not texts or not self.ensure():
            return None
        inp = [(QUERY_INSTRUCT + t if query else t)[:6000] for t in texts]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request("POST", "/v1/embeddings", json.dumps({"input": inp, "model": self.name}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                self.error = "embedding failed: HTTP %d" % r.status
                return None
            data = sorted(json.loads(body)["data"], key=lambda d: d["index"])
            if len(data) != len(inp):
                self.error = "embedding failed: wrong number of vectors"
                return None
            self.error = None
            return [_norm(d["embedding"]) for d in data]
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
            self.error = "embedding failed: " + e.__class__.__name__
            return None
        finally:
            c.close()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------------------------------------- the store
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, topic TEXT NOT NULL DEFAULT 'other',
  created REAL, updated REAL, source TEXT, uses INTEGER NOT NULL DEFAULT 0, last_used REAL,
  pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
  DELETE FROM vectors WHERE kind='m' AND ref=old.id; END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
  DELETE FROM vectors WHERE kind='m' AND ref=old.id; END;
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, n INTEGER NOT NULL, ts REAL,
  title TEXT, text TEXT NOT NULL, UNIQUE(chat_id, n));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  DELETE FROM vectors WHERE kind='c' AND ref=old.id; END;
CREATE TABLE IF NOT EXISTS vectors(kind TEXT NOT NULL, ref INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
  PRIMARY KEY(kind, ref, model));
CREATE TABLE IF NOT EXISTS learned(chat_id TEXT PRIMARY KEY, saved INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS blocks(k TEXT PRIMARY KEY, text TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reviewed(a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY(a, b));
"""


class Closed(sqlite3.Error):
    """The memory folder is closed (it is being moved, or its drive went away)."""


class VecIndex:
    """The vectors of one kind ('m' memories, 'c' chat snippets) in RAM for search. Kept up to date in place as
    vectors are added and removed, so indexing a big import never forces the whole index to be read again."""

    def __init__(self, rows):
        self.ids = [r[0] for r in rows]
        self.pos = {i: n for n, i in enumerate(self.ids)}
        if np is not None:
            self.mat = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1).copy()
                        if rows else None)
        else:
            self.mat = [_vec_from(r[1]) for r in rows]

    def __len__(self):
        return len(self.ids)

    def put(self, ref, vec):
        if np is not None:
            v = np.asarray(vec, dtype=np.float32)
            if self.mat is not None and self.mat.shape[1] != v.shape[0]:
                return False   # another embedding size: the caller rebuilds
            if ref in self.pos:
                self.mat[self.pos[ref]] = v
                return True
            n = len(self.ids)
            if self.mat is None:
                self.mat = np.empty((64, v.shape[0]), dtype=np.float32)
            elif n >= self.mat.shape[0]:   # grow by half, so adding stays cheap on average
                big = np.empty((max(64, n + n // 2), self.mat.shape[1]), dtype=np.float32)
                big[:n] = self.mat[:n]
                self.mat = big
            self.mat[n] = v
        else:
            if ref in self.pos:
                self.mat[self.pos[ref]] = list(vec)
                return True
            self.mat.append(list(vec))
        self.pos[ref] = len(self.ids)
        self.ids.append(ref)
        return True

    def drop(self, ref):
        p = self.pos.pop(ref, None)
        if p is None:
            return
        last = len(self.ids) - 1
        if p != last:   # move the last row into the hole
            self.ids[p] = self.ids[last]
            self.pos[self.ids[p]] = p
            self.mat[p] = self.mat[last]
        self.ids.pop()
        if np is None:
            self.mat.pop()

    def matrix(self):
        return self.mat[:len(self.ids)] if np is not None and self.mat is not None else self.mat

    def vec(self, ref):
        p = self.pos.get(ref)
        return None if p is None else self.mat[p]

    def scores(self, qv):
        """Cosine similarity of every vector with qv, as a list aligned with self.ids."""
        if not self.ids:
            return []
        if np is not None:
            return self.matrix() @ np.asarray(qv, dtype=np.float32)
        return [_dot(v, qv) for v in self.mat]

    def top(self, qv, n):
        s = self.scores(qv)
        if not len(s):
            return []
        if np is not None:
            idx = np.argsort(-s)[:n]
            return [(self.ids[i], float(s[i])) for i in idx]
        return sorted(zip(self.ids, s), key=lambda t: -t[1])[:n]


class Store:
    def __init__(self, home):
        self.home = home
        self.db = None
        self.lock = threading.RLock()
        self.error = None
        self._index = {}   # (kind, model) -> VecIndex
        self.ver = 0       # bumps when the set of memories or their text changes

    @property
    def path(self):
        return os.path.join(self.home, "memory.db")

    def open(self, create):
        with self.lock:
            if self.db:
                return True
            if not os.path.isdir(self.home):
                drive = os.path.splitdrive(self.home)[0]
                if not create or (drive and not os.path.exists(drive + "\\")):
                    self.error = "memory folder not found: %s (is the drive plugged in?)" % self.home
                    return False
                try:
                    os.makedirs(self.home, exist_ok=True)
                except OSError as e:
                    self.error = "couldn't create %s: %s" % (self.home, e)
                    return False
            try:
                self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                self.db.executescript(SCHEMA)
                cols = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
                if "archived" not in cols:   # a memory.db from before archiving existed
                    self.db.execute("ALTER TABLE memories ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                self.db.execute("INSERT OR IGNORE INTO meta VALUES('created', ?)", (str(int(_now())),))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema', '2')")
                self.db.execute("DELETE FROM blocks WHERE ts < ?", (_now() - 90 * 86400,))
                self.db.commit()
                os.makedirs(os.path.join(self.home, "chats"), exist_ok=True)
                readme = os.path.join(self.home, "README.txt")
                if not os.path.exists(readme):
                    with open(readme, "w", encoding="utf-8") as f:
                        f.write(README)
            except (sqlite3.Error, OSError) as e:
                if self.db:
                    self.db.close()
                self.db = None
                self.error = "could not open %s: %s" % (self.path, e)
                return False
            self.error = None
            return True

    def close(self):
        with self.lock:
            if self.db:
                try:
                    self.db.close()
                except sqlite3.Error:
                    pass
                self.db = None
            self._index.clear()

    def q(self, sql, args=()):
        with self.lock:
            if not self.db:
                raise Closed("the memory folder is closed")
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            if not self.db:
                raise Closed("the memory folder is closed")
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def meta(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0][0] if r else default

    def set_meta(self, k, v):
        self.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (k, str(v)))

    # ---- memories
    def row(self, r):
        return {"id": r[0], "text": r[1], "topic": r[2], "created": r[3], "updated": r[4], "source": r[5],
                "uses": r[6], "last_used": r[7], "pinned": bool(r[8]), "archived": bool(r[9])}

    COLS = "id, text, topic, created, updated, source, uses, last_used, pinned, archived"

    def get(self, mid):
        r = self.q("SELECT %s FROM memories WHERE id=?" % self.COLS, (mid,))
        return self.row(r[0]) if r else None

    def get_many(self, ids):
        ids = list(ids)
        if not ids:
            return {}
        rows = self.q("SELECT %s FROM memories WHERE id IN (%s)" % (self.COLS, ",".join("?" * len(ids))), ids)
        return {r[0]: self.row(r) for r in rows}

    def list(self, query="", topic="", limit=200, offset=0, archived=False):
        where, args = ["m.archived=?"], [int(bool(archived))]
        if topic in TOPICS:
            where.append("m.topic=?")
            args.append(topic)
        if query.strip():
            fq = fts_query(query)
            like = "%" + query.strip().lower() + "%"
            if fq:
                where.append("(m.id IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?) OR lower(m.text) LIKE ?)")
                args += [fq, like]
            else:
                where.append("lower(m.text) LIKE ?")
                args.append(like)
        sql = "SELECT %s FROM memories m WHERE %s ORDER BY m.pinned DESC, m.updated DESC LIMIT ? OFFSET ?" % (
            ", ".join("m." + c.strip() for c in self.COLS.split(",")), " AND ".join(where))
        return [self.row(r) for r in self.q(sql, args + [int(limit), int(offset)])]

    def counts(self):
        m = self.q("SELECT topic, count(*) FROM memories WHERE archived=0 GROUP BY topic")
        c = self.q("SELECT count(*) FROM chunks")[0][0]
        a = self.q("SELECT count(*) FROM memories WHERE archived=1")[0][0]
        return {t: n for t, n in m}, c, a

    def add(self, text, topic="other", source="", pinned=False):
        text = " ".join(str(text or "").split())[:1000]
        if not text:
            return None
        topic = topic if topic in TOPICS else "other"
        dup = self.q("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,))
        if dup:
            self.x("UPDATE memories SET updated=?, archived=0 WHERE id=?", (_now(), dup[0][0]))
            return dup[0][0]
        t = _now()
        cur = self.x("INSERT INTO memories(text, topic, created, updated, source, pinned) VALUES(?,?,?,?,?,?)",
                     (text, topic, t, t, str(source or "")[:200], int(bool(pinned))))
        self.ver += 1
        return cur.lastrowid

    def update(self, mid, text=None, topic=None, pinned=None, archived=None):
        m = self.get(mid)
        if not m:
            return False
        new = " ".join(str(text).split())[:1000] if text is not None else ""
        if new and new != m["text"]:
            self.x("UPDATE memories SET text=?, updated=? WHERE id=?", (new, _now(), mid))
            self._forget_vec("m", mid)   # the trigger removed its vector; the worker embeds the new text
        if topic in TOPICS and topic != m["topic"]:
            self.x("UPDATE memories SET topic=?, updated=? WHERE id=?", (topic, _now(), mid))
        if pinned is not None:
            self.x("UPDATE memories SET pinned=? WHERE id=?", (int(bool(pinned)), mid))
        if archived is not None:
            self.x("UPDATE memories SET archived=? WHERE id=?", (int(bool(archived)), mid))
        self.ver += 1
        return True

    def touch(self, mid):
        self.x("UPDATE memories SET updated=?, archived=0 WHERE id=?", (_now(), mid))

    def delete(self, mid):
        cur = self.x("DELETE FROM memories WHERE id=?", (mid,))
        self._forget_vec("m", mid)
        self.ver += 1
        return cur.rowcount > 0

    # ---- chat snippets
    def put_chunks(self, chat_id, title, exchanges):
        with self.lock:
            if not self.db:
                raise Closed("the memory folder is closed")
            have = {r[0] for r in self.db.execute("SELECT n FROM chunks WHERE chat_id=?", (chat_id,))}
            for n, ts, text in exchanges:
                if n not in have:
                    self.db.execute("INSERT OR IGNORE INTO chunks(chat_id, n, ts, title, text) VALUES(?,?,?,?,?)",
                                    (chat_id, n, ts, title, text))
            self.db.execute("UPDATE chunks SET title=? WHERE chat_id=? AND title IS NOT ?", (title, chat_id, title))
            self.db.commit()

    def drop_chat(self, chat_id):
        ids = [r[0] for r in self.q("SELECT id FROM chunks WHERE chat_id=?", (chat_id,))]
        self.x("DELETE FROM chunks WHERE chat_id=?", (chat_id,))
        self.x("DELETE FROM learned WHERE chat_id=?", (chat_id,))
        for i in ids:
            self._forget_vec("c", i)

    # ---- vectors
    def missing_vectors(self, model, limit=32):
        m = self.q("SELECT 'm', id, text FROM memories WHERE id NOT IN (SELECT ref FROM vectors WHERE kind='m' AND model=?) LIMIT ?",
                   (model, limit))
        if len(m) < limit:
            m += self.q("SELECT 'c', id, text FROM chunks WHERE id NOT IN (SELECT ref FROM vectors WHERE kind='c' AND model=?) LIMIT ?",
                        (model, limit - len(m)))
        return m

    def put_vectors(self, model, items):
        with self.lock:
            if not self.db:
                raise Closed("the memory folder is closed")
            self.db.executemany("INSERT OR REPLACE INTO vectors(kind, ref, model, vec) VALUES(?,?,?,?)",
                                [(k, ref, model, _vec_bytes(v)) for k, ref, v in items])
            self.db.commit()
            for k, ref, v in items:
                ix = self._index.get((k, model))
                if ix is not None and not ix.put(ref, v):
                    self._index.pop((k, model), None)

    def drop_other_vectors(self, model):
        if self.q("SELECT 1 FROM vectors WHERE model<>? LIMIT 1", (model,)):
            self.x("DELETE FROM vectors WHERE model<>?", (model,))
            with self.lock:
                for key in [key for key in self._index if key[1] != model]:
                    self._index.pop(key, None)

    def _forget_vec(self, kind, ref):
        with self.lock:
            for (k, _), ix in self._index.items():
                if k == kind:
                    ix.drop(ref)

    def index(self, kind, model):
        with self.lock:
            ix = self._index.get((kind, model))
            if ix is None:
                ix = self._index[(kind, model)] = VecIndex(
                    self.q("SELECT ref, vec FROM vectors WHERE kind=? AND model=?", (kind, model)))
            return ix

    def similar(self, kind, model, qv, top=20):
        with self.lock:
            return self.index(kind, model).top(qv, top)

    def keyword(self, kind, query, top=20):
        fq = fts_query(query)
        if not fq:
            return []
        t = "memories_fts" if kind == "m" else "chunks_fts"
        try:
            return [r[0] for r in self.q("SELECT rowid FROM %s WHERE %s MATCH ? ORDER BY bm25(%s) LIMIT ?" % (t, t, t), (fq, top))]
        except Closed:
            raise
        except sqlite3.Error:
            return []

    # ---- the memory block each chat message was sent with (see Memory.chat_inject)
    def block(self, k):
        r = self.q("SELECT text FROM blocks WHERE k=?", (k,))
        return r[0][0] if r else None

    def put_block(self, k, text):
        self.x("INSERT OR REPLACE INTO blocks(k, text, ts) VALUES(?,?,?)", (k, text, _now()))

    def size(self):
        total = 0
        for dp, _, fs in os.walk(self.home):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total


# ---------------------------------------------------------------------------------------------- the manager
LEARN_SCHEMA = {
    "type": "object",
    "properties": {"memories": {"type": "array", "items": {
        "type": "object",
        "properties": {"op": {"type": "string", "enum": ["add", "update", "delete"]},
                       "id": {"type": "integer"},
                       "text": {"type": "string"},
                       "topic": {"type": "string", "enum": list(TOPICS)}},
        "required": ["op"]}}},
    "required": ["memories"]}

LEARN_PROMPT = """You maintain the long-term memory of a personal AI assistant. The memory is shared by every model \
the user runs, so each memory must make sense on its own, months from now, to a model that never saw this chat.

Decide whether the LATEST EXCHANGE contains anything worth remembering. Almost always it does not: the right \
answer for most exchanges is {"memories": []}. A wrong or useless memory is worse than a missing one, because it \
will be shown to the model again and again.

Worth remembering (only when the USER said it or clearly confirmed it):
- who the user is: their work, setup, devices, where they live, their situation              topic: you
- lasting preferences: likes, dislikes, how they want answers or code written                topic: preference
- their own projects, goals, plans and decisions                                             topic: project
- people in their life and facts about them                                                  topic: people
- events and plans with a date                                                               topic: event
- a fix or procedure that the user says worked for them                                      topic: howto
- another lasting fact about the user's world that they stated                               topic: fact

Never remember:
- the question itself, or that the user asked about something ("The user asked how to ...")
- general knowledge, explanations, code or advice the assistant gave
- anything the assistant said, guessed or suggested that the user did not confirm
- one-off tasks ("translate this", "summarise that", "fix this error"), small talk, moods of the moment
- passwords, API keys, tokens, card or account numbers, or other secrets
- anything already covered by an EXISTING MEMORY

Rules:
- One short, self-contained sentence per memory, third person, starting with "The user" (e.g. "The user's laptop \
is a ThinkPad X1.").
- Never write relative dates ("next week", "tomorrow", "in March"): turn them into absolute dates using TODAY, \
e.g. "on 2026-11-18".
- If the exchange changes or corrects an EXISTING MEMORY, return {"op":"update","id":<its id>,"text":<the new \
sentence>} instead of adding a second memory about the same thing.
- If it proves an EXISTING MEMORY wrong, or the user asks to forget it, return {"op":"delete","id":<its id>}.
- If the user explicitly asks you to remember something, always save it. If an EXISTING MEMORY says the same thing \
in rougher words (e.g. a raw line pasted from another assistant), return an update for it with the clean sentence \
and the right topic.
Return JSON only: {"memories": [{"op":"add","text":"...","topic":"..."}]}"""

TIDY_SCHEMA = {
    "type": "object",
    "properties": {"merge": {"type": "array", "items": {
        "type": "object",
        "properties": {"keep": {"type": "integer"}, "drop": {"type": "array", "items": {"type": "integer"}},
                       "text": {"type": "string"}},
        "required": ["keep", "drop", "text"]}}},
    "required": ["merge"]}

TIDY_PROMPT = """You tidy the long-term memory of a personal AI assistant. Below are a few memories that look \
similar. Each has an id and the date it was last confirmed.

Merge memories ONLY when they are about the very same thing:
- they say the same fact in different words                 -> keep one, drop the others
- one is a newer version of the other (the fact changed)    -> keep the newer one, drop the older
- one adds detail to the other                              -> keep one, with a sentence that has all the detail

Do NOT merge memories that are merely related (two different people, two different projects, two different \
preferences). When in doubt, leave them alone: return {"merge": []}.

For each merge return {"keep": <id>, "drop": [<ids>], "text": <one self-contained sentence about "the user">}.
If the memories contradict each other, the newer date wins. Never invent details. Return JSON only."""

# a credential with its value ("password is hunter2", "api key: 9f8a..."), a long key-like string, a card number
_SECRET = re.compile(r"""(?i)\b(password|passphrase|passcode|api[ _-]?key|secret|token|pin|cvv)\b[^.]{0,20}?(\bis\b|:|=)\s*["']?(?=\S*\d)\S{6,}"""
                     r"""|\bsk-[A-Za-z0-9_-]{16,}|\b[A-Fa-f0-9]{32,}\b|\b(?:\d{4}[ -]?){3}\d{1,7}\b""")
_RELATIVE = re.compile(r"(?i)\b(tomorrow|yesterday|today|tonight|next (week|month|year|monday|tuesday|wednesday|"
                       r"thursday|friday|saturday|sunday)|last (week|month|year)|this (week|weekend|month|morning|"
                       r"afternoon|evening)|in (a|an|one|two|three|\d+) (day|week|month|year)s?|"
                       r"(a|an|one|two|three|\d+) (day|week|month|year)s? ago)\b")
_ASKED = re.compile(r"(?i)^the user (asked|asks|is asking|wanted to know|wants to know|requested|inquired|"
                    r"was curious|wondered|said (hi|hello|thanks)|greeted|thanked)\b")


def acceptable(text):
    """Server-side sanity check on a memory the model wants to add: the prompt asks for all of this, but a small
    model doesn't always listen. Returns the cleaned sentence, or None to drop it."""
    t = " ".join(str(text or "").split())
    if not (12 <= len(t) <= 400) or t.endswith("?"):
        return None
    if _SECRET.search(t) or _RELATIVE.search(t) or _ASKED.search(t):
        return None
    return t


class Memory:
    def __init__(self):
        self.store = None
        self.settings = {}
        self.save_settings = lambda: None
        self.embedder = None
        self.llm = None          # llm(messages, schema, max_tokens) -> dict | None
        self.busy = lambda: False
        self.default_home = ""
        self.seq = 0             # bumps whenever memory learns something; the UI shows the new ones
        self.last_learned = []
        self.last_recall = []
        self.status_msg = ""
        self.moving = False
        self.chats_mod = None
        self.stop = threading.Event()
        self.job = {}
        self._tasks = {}   # Cline task -> recalled memory, so a task's steps all get the same block
        self._blocks = {}  # chat message -> the memory block it was sent with (see chat_inject)
        self._syn = (None, [])   # cache for synapses()
        self._tidy_at = 0
        self._tidy_ver = None    # store.ver when the last look for duplicates found nothing new
        self._retry_open = 0

    # ---- setup
    def start(self, settings, save_settings, root, llama_exe, llm, busy, chats_mod, models_root=None,
              embed_port=EMBED_PORT):
        self.settings, self.save_settings = settings, save_settings
        self.default_home = os.path.join(root, "memory")
        self.llm, self.busy, self.chats_mod = llm, busy, chats_mod
        em = settings.get("embed_model") or os.path.join(models_root or root, "models", "Qwen3-Embedding-0.6B-GGUF",
                                                          "Qwen3-Embedding-0.6B-Q8_0.gguf")
        self.embedder = Embedder(llama_exe, em, embed_port)
        self.open_home(self.home)
        self._sweep_uploads()
        threading.Thread(target=self.embedder.vet, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()

    @property
    def home(self):
        return self.settings.get("memory_dir") or self.default_home

    @property
    def on(self):
        return self.settings.get("memory_on", True)

    @property
    def learn_on(self):
        return self.settings.get("memory_learn", True)

    def ok(self):
        return bool(self.store and self.store.db)

    def open_home(self, home):
        """Open (or reopen) the store at `home`. Only the default folder is created automatically: a missing custom
        folder means the drive isn't there, and memory pauses instead of starting an empty one somewhere else."""
        if self.store:
            self.store.close()
        self.store = Store(home)
        self._blocks.clear()
        self._tasks.clear()
        created = self.store.open(create=(os.path.normcase(home) == os.path.normcase(self.default_home)))
        if self.chats_mod:
            self.chats_mod.DIR = os.path.join(home, "chats")
            if created:
                self._adopt_old_chats()
        return created

    def _sweep_uploads(self):
        """Uploads are streamed to import-*.tmp in the memory folder and removed after the import. If the app was
        closed in between, the copy (it can be GBs) is still there; nothing uses it any more."""
        if not self.ok():
            return
        try:
            for f in os.listdir(self.home):
                if re.fullmatch(r"import-\d+\.tmp", f):
                    os.remove(os.path.join(self.home, f))
        except OSError:
            pass

    def _adopt_old_chats(self):
        """Chats saved before memory existed lived in command-center/chats; bring them into the memory home."""
        old = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chats")
        new = self.chats_mod.DIR
        if os.path.normcase(old) == os.path.normcase(new) or not os.path.isdir(old):
            return
        for f in os.listdir(old):
            if f.endswith(".json") and not os.path.exists(os.path.join(new, f)):
                try:
                    shutil.move(os.path.join(old, f), os.path.join(new, f))
                    with open(os.path.join(new, f), encoding="utf-8") as fh:
                        self.note_chat(json.load(fh), learn=False)
                except (OSError, ValueError, sqlite3.Error):
                    pass
        try:
            os.rmdir(old)
        except OSError:
            pass

    def set_flags(self, on=None, learn=None):
        if on is not None:
            self.settings["memory_on"] = bool(on)
        if learn is not None:
            self.settings["memory_learn"] = bool(learn)
        self.save_settings()

    # ---- status
    def _error(self):
        return None if self.ok() else ((self.store.error if self.store else None) or "memory isn't available")

    def brief(self):
        try:
            n = self.store.q("SELECT count(*) FROM memories WHERE archived=0")[0][0] if self.ok() else 0
        except sqlite3.Error:
            n = 0
        return {"on": self.on, "ok": self.ok(), "error": self._error(), "seq": self.seq,
                "learned": self.last_learned[-6:], "count": n}

    def status(self):
        st = {"home": self.home, "default_home": self.default_home, "on": self.on, "learn": self.learn_on,
              "ok": self.ok(), "error": None, "moving": self.moving, "topics": list(TOPICS),
              "embed": {"model": self.embedder.name, "available": self.embedder.available(),
                        "running": self.embedder.ready(), "error": self.embedder.error, "port": self.embedder.port},
              "numpy": np is not None, "msg": self.status_msg, "job": self.job}
        try:
            if not self.ok():
                raise Closed()
            by_topic, chunks, archived = self.store.counts()
            model = self.embedder.name
            indexed = self.store.q("SELECT count(*) FROM vectors WHERE model=?", (model,))[0][0]
            total = self.store.q("SELECT count(*) FROM memories")[0][0] + chunks
            pend = self.store.q("SELECT count(*) FROM learned WHERE saved>done")[0][0]
            st.update(count=sum(by_topic.values()), by_topic=by_topic, chunks=chunks, archived=archived,
                      size=self.store.size(), indexed=indexed, to_index=max(0, total - indexed), to_learn=pend,
                      free=shutil.disk_usage(self.home).free if os.path.isdir(self.home) else None)
        except (sqlite3.Error, OSError):
            st.update(ok=False, error=self._error() or "the memory folder can't be read right now")
        return st

    def tree(self, limit=3000):
        """Everything the 4:mem tree draws: all (not archived) memories plus what was just recalled and learned."""
        if not self.ok():
            return {"memories": [], "recalled": [], "learned": [], "error": self._error()}
        rows = self.store.q("SELECT %s FROM memories WHERE archived=0 ORDER BY pinned DESC, updated DESC LIMIT ?"
                            % Store.COLS, (int(limit),))
        total = self.store.q("SELECT count(*) FROM memories WHERE archived=0")[0][0]
        return {"memories": [self.store.row(r) for r in rows], "recalled": list(self.last_recall), "total": total,
                "learned": [e["id"] for e in self.last_learned if e.get("op") != "delete"]}

    def _pairs(self, per, floor, limit=4000):
        """{(id_a, id_b): similarity} for each memory's `per` nearest neighbours at or above `floor`."""
        with self.store.lock:
            ix = self.store.index("m", self.embedder.name)
            ids, mat = list(ix.ids), ix.matrix()
            if np is not None and mat is not None:
                mat = mat.copy()
            elif np is None:
                mat = [list(v) for v in mat]
        n = len(ids)
        out = {}
        if n < 2:
            return out
        if np is not None:
            if n > limit:
                ids, mat, n = ids[:limit], mat[:limit], limit
            k = min(per, n - 1)
            for a0 in range(0, n, 1024):   # in slices, so 4,000 memories never need a 4,000 x 4,000 table at once
                S = mat[a0:a0 + 1024] @ mat.T
                for i in range(S.shape[0]):
                    S[i, a0 + i] = -1.0
                idx = np.argpartition(-S, k - 1, axis=1)[:, :k]
                for i in range(S.shape[0]):
                    for j in idx[i]:
                        s = float(S[i, j])
                        if s >= floor:
                            a, b = sorted((ids[a0 + i], ids[int(j)]))
                            out[(a, b)] = round(s, 3)
        elif n <= 400:  # without numpy, only small memories (it's O(n^2) in pure Python)
            for i in range(n):
                sims = sorted(((_dot(mat[i], mat[j]), j) for j in range(n) if j != i), reverse=True)[:per]
                for s, j in sims:
                    if s >= floor:
                        a, b = sorted((ids[i], ids[j]))
                        out[(a, b)] = round(s, 3)
        return out

    def synapses(self, per=3, floor=0.5):
        """Links between memories with related meaning, for the brain view: each memory to its `per` nearest
        neighbours above `floor` cosine similarity. -> [[id_a, id_b, similarity], ...]"""
        if not self.ok():
            return []
        key = (self.store.ver, len(self.store.index("m", self.embedder.name)), per, floor)
        if self._syn[0] != key:
            live = {r[0] for r in self.store.q("SELECT id FROM memories WHERE archived=0")}
            self._syn = (key, [[a, b, s] for (a, b), s in self._pairs(per, floor).items() if a in live and b in live])
        return self._syn[1]

    def related(self, mid, k=6):
        """The memories closest in meaning to one memory (keyword match if it isn't indexed yet)."""
        m = self.store.get(mid) if self.ok() else None
        if not m:
            return []
        model = self.embedder.name
        with self.store.lock:
            v = self.store.index("m", model).vec(mid)
            hits = [(i, round(float(s), 3)) for i, s in self.store.similar("m", model, list(v), k + 4) if i != mid] if v is not None else None
        if hits is None:
            hits = [(i, None) for i in self.store.keyword("m", m["text"], k + 4) if i != mid]
        rows = self.store.get_many(i for i, _ in hits)
        out = []
        for i, s in hits:
            r = rows.get(i)
            if r and not r["archived"] and (s is None or s >= 0.3):
                out.append(dict(r, score=s))
        return out[:k]

    def graph(self):
        """Memories for the brain view: the most used and most recent, with their topic."""
        if not self.ok():
            return []
        rows = self.store.q("SELECT id, topic FROM memories WHERE archived=0 ORDER BY pinned DESC, uses DESC, updated DESC LIMIT 120")
        return [{"id": r[0], "topic": r[1]} for r in rows]

    # ---- recall: called on every chat request
    def search(self, q, ctx="", timeout=8):
        """Rank memories and old-chat snippets for a message. -> (memories [(id, score)], snippets [(id, score)],
        used_vectors). `ctx` is the previous turn: it only helps short follow-ups ("and the laptop?")."""
        P, st, model = RECALL, self.store, self.embedder.name
        texts = [q[-2000:]]
        if ctx.strip() and len(q) < 120:
            texts.append((ctx + "\n" + q)[-2000:])
        qv = self.embedder.embed(texts, query=True, timeout=timeout)
        kw = st.keyword("m", q, 12)
        if not qv:
            # No vectors (the embedding server is still loading, or its model is missing): keywords alone are a
            # blunt tool, so a memory needs to share at least two distinctive words with the message.
            want = set(_words(q))
            rows = st.get_many(kw)
            hits = [(mid, len(want & set(_words(rows[mid]["text"])))) for mid in kw if mid in rows and not rows[mid]["archived"]]
            return [(mid, float(n)) for mid, n in hits if n >= 2][:4], [], False
        with st.lock:
            ix = st.index("m", model)
            s0 = ix.scores(qv[0])
            s1 = ix.scores(qv[1]) if len(qv) > 1 else None
            scores = {}
            for n, mid in enumerate(ix.ids):
                s = float(s0[n])
                if s1 is not None:
                    s = max(s, float(s1[n]) - P["ctx_penalty"])
                scores[mid] = s
            chunk_hits = st.similar("c", model, qv[0], 16)
        for mid in kw:
            if mid in scores:
                scores[mid] += P["kw_bonus"]
        ranked = sorted(scores.items(), key=lambda t: -t[1])[:40]
        if ranked:
            floor = max(P["mem_min"], ranked[0][1] - P["rel_gap"])
            ranked = [(mid, s) for mid, s in ranked if s >= floor]
        return ranked, [(cid, s) for cid, s in chunk_hits if s >= P["chunk_min"]], True

    def recall(self, messages, chat_id=None, k=None):
        """Returns (text for the <memory> block or None, info) for the newest user message."""
        if not (self.on and self.ok()):
            return None, {}
        q = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                q = _text(m.get("content"))
                break
        if not q.strip():
            return None, {}
        P, st, model = RECALL, self.store, self.embedder.name
        k = k or P["k"]
        prev = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-3:-1]
        ctx = " ".join(_text(m.get("content"))[:300] for m in prev)
        ranked, chunk_hits, vectors = self.search(q, ctx)
        pinned = [r[0] for r in st.q("SELECT id FROM memories WHERE pinned=1 AND archived=0 ORDER BY updated DESC LIMIT 12")]
        rows = st.get_many(pinned + [mid for mid, _ in ranked])
        picked, vecs = [], []
        with st.lock:
            ix = st.index("m", model)
            for mid, s in ranked:
                r = rows.get(mid)
                if not r or r["archived"] or mid in pinned:
                    continue
                v = ix.vec(mid) if vectors else None
                # two memories that say the same thing: the better match is enough
                if v is not None and any(float(_dot(v, o) if np is None else v @ o) >= P["dup"] for o in vecs):
                    continue
                picked.append(mid)
                if v is not None:
                    vecs.append(v.copy() if np is not None else list(v))
                if len(picked) >= k:
                    break
        mems = [rows[i] for i in pinned + picked if i in rows]
        # snippets of earlier chats: one per chat, never from the chat being continued
        chunks, seen_chats = [], set()
        for cid, s in chunk_hits:
            r = st.q("SELECT chat_id, ts, title, text FROM chunks WHERE id=?", (cid,))
            if r and r[0][0] != chat_id and r[0][0] not in seen_chats:
                seen_chats.add(r[0][0])
                chunks.append(r[0])
            if len(chunks) >= P["chunks"]:
                break
        self.last_recall = [m["id"] for m in mems]
        if not mems and not chunks:
            return None, {"ids": [], "chunks": 0}
        lines = ["# Long-term memory (added automatically; the user didn't type this part)",
                 "What you remember about the user from earlier conversations. Today is %s. Use what is relevant, "
                 "naturally, without announcing it; ignore the rest. If memories conflict, the newer date wins; if a "
                 "memory conflicts with what the user says now, trust the user." % time.strftime("%A %Y-%m-%d")]
        used, kept = len(lines[0]) + len(lines[1]), []
        if mems:
            lines.append("\n## What you remember")
            for m in mems:
                line = "- [%s] %s (%s)" % (m["topic"], m["text"], time.strftime("%Y-%m-%d", time.localtime(m["updated"] or 0)))
                if used + len(line) > MEM_BUDGET and kept:
                    break   # whole memories only: never cut one in half
                lines.append(line)
                used += len(line) + 1
                kept.append(m["id"])
        n_chunks = 0
        for chat, ts, title, text in chunks:
            body = text if len(text) <= P["chunk_chars"] else text[:P["chunk_chars"]].rsplit(" ", 1)[0] + " …"
            entry = "--- %s · \"%s\"\n%s" % (time.strftime("%Y-%m-%d", time.localtime(ts or 0)), (title or "chat")[:80], body)
            if used + len(entry) > MAX_BLOCK - 60:
                break
            if not n_chunks:
                lines.append("\n## Possibly related earlier conversations")
            lines.append(entry)
            used += len(entry) + 1
            n_chunks += 1
        if kept:
            st.x("UPDATE memories SET uses=uses+1, last_used=? WHERE id IN (%s)" % ",".join("?" * len(kept)), [_now()] + kept)
        self.last_recall = kept
        text = "\n".join(lines)
        return text, {"ids": kept, "chunks": n_chunks, "chars": len(text)}

    @staticmethod
    def _block_key(chat_id, content):
        raw = "%s|%s" % (chat_id or "", json.dumps(content, sort_keys=True))
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def chat_inject(self, messages, chat_id, text):
        """Chat: the newest user message gets this turn's memory block, and every earlier user message gets back the
        exact block it was first sent with. The window keeps its history without the blocks, so without this the
        conversation would differ from what the model server cached and it would re-read the whole chat each turn.
        Blocks are kept in memory.db too, so this still holds after the command center was closed and reopened."""
        users = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
        if not users:
            return messages

        def lookup(k):
            if k not in self._blocks:
                try:
                    self._blocks[k] = self.store.block(k) if self.ok() else None
                except sqlite3.Error:
                    self._blocks[k] = None
                while len(self._blocks) > 5000:
                    self._blocks.pop(next(iter(self._blocks)))
            return self._blocks[k]

        out = messages
        for i in users[:-1]:
            b = lookup(self._block_key(chat_id, messages[i].get("content")))
            if b:
                out = _inject_at(out, i, b)
        k = self._block_key(chat_id, messages[users[-1]].get("content"))
        if text:
            self._blocks[k] = text
            try:
                if self.ok():
                    self.store.put_block(k, text)
            except sqlite3.Error:
                pass
            out = _inject_at(out, users[-1], text)
        else:
            # No block this turn. If this exact message was sent before with one (a retry), an earlier copy of the
            # conversation at the model server has it; forgetting it keeps later turns consistent with this one.
            if lookup(k):
                self._blocks[k] = None
                try:
                    if self.ok():
                        self.store.x("DELETE FROM blocks WHERE k=?", (k,))
                except sqlite3.Error:
                    pass
        return out

    # ---- Cline: recall once per task, so every step of the task sends the identical prompt prefix
    def recall_task(self, messages):
        task = cline_task(messages)
        if not task or not (self.on and self.ok()):
            return None, {}, task
        key = "task:" + hashlib.sha1(task.encode("utf-8", "replace")).hexdigest()
        hit = self._tasks.get(key)
        if hit is None:
            saved = self.store.block(key)   # the same task, continued after a restart
            if saved is not None:
                try:
                    hit = tuple(json.loads(saved))
                except ValueError:
                    hit = None
        if hit:
            self._tasks[key] = hit
            return hit[0], dict(hit[1], cached=True), task
        text, info = self.recall([{"role": "user", "content": task}], None)
        self._tasks[key] = (text, info)
        self.store.put_block(key, json.dumps([text, info]))
        while len(self._tasks) > 200:
            self._tasks.pop(next(iter(self._tasks)))
        if self.settings.get("cline_learn", True) and self.learn_on:
            self._note_cline_task(task)
        return text, info, task

    def _note_cline_task(self, task):
        """What you ask Cline is worth learning from too: one 'chat' per day collects the day's tasks."""
        if not self.chats_mod:
            return
        cid = "cline-" + time.strftime("%Y%m%d")
        chat = self.chats_mod.get_chat(cid) or {"id": cid, "title": "Cline · " + time.strftime("%Y-%m-%d"),
                                                 "created": _now(), "messages": []}
        chat["messages"].append({"role": "user", "content": task[:4000]})
        chat["messages"].append({"role": "assistant", "content": "(task handed to Cline, the coding agent in VS Code)"})
        chat["updated"] = _now()
        self.chats_mod.write_raw(chat)
        self.note_chat(chat)

    # ---- chats feed memory
    def note_chat(self, chat, learn=True):
        """Called whenever a chat is saved: index its exchanges for recall and queue the new ones for learning."""
        if not self.ok() or not isinstance(chat, dict):
            return
        cid, msgs = chat.get("id"), chat.get("messages") or []
        if not cid:
            return
        ex = list(_exchanges(msgs))
        ts = chat.get("updated") or _now()
        # Snippets are for finding an old conversation again, so one-liners ("thanks", "ok") aren't indexed.
        self.store.put_chunks(cid, chat.get("title") or "", [
            (n, ts, "User: %s\nAssistant: %s" % (u[:700], a[:1100])) for n, u, a in ex if len(u) + len(a) >= 80])
        if learn and self.learn_on:
            self.store.x("INSERT INTO learned(chat_id, saved, done) VALUES(?,?,0) "
                         "ON CONFLICT(chat_id) DO UPDATE SET saved=excluded.saved", (cid, len(ex)))
        else:
            self.store.x("INSERT INTO learned(chat_id, saved, done) VALUES(?,?,?) "
                         "ON CONFLICT(chat_id) DO UPDATE SET saved=excluded.saved, done=excluded.saved", (cid, len(ex), len(ex)))

    def forget_chat(self, cid):
        if self.ok():
            self.store.drop_chat(cid)

    # ---- background: index vectors, learn from new exchanges, tidy
    def worker(self):
        while not self.stop.wait(3):
            try:
                if self.moving:
                    continue
                if not self.ok():
                    # the drive may have been plugged back in: look again every 15 s (never creates a custom folder)
                    if _now() - self._retry_open > 15:
                        self._retry_open = _now()
                        self.open_home(self.home)
                    continue
                if not os.path.isdir(self.home):   # the drive went away while we were running
                    self.store.close()
                    self.store.error = "memory folder not found: %s (is the drive plugged in?)" % self.home
                    continue
                self._index_some()
                if self.on and self.learn_on and not self.busy():
                    if not self._learn_one():
                        self._tidy_one()
                self.status_msg = ""
            except Exception as e:  # keep the worker alive whatever happens
                self.status_msg = "memory worker: %s" % e.__class__.__name__

    def _index_some(self):
        model = self.embedder.name
        if not self.embedder.available() and not self.embedder.ready():
            return
        todo = self.store.missing_vectors(model, 32)
        if not todo:
            return
        vecs = self.embedder.embed([t for _, _, t in todo])
        if vecs:
            self.store.put_vectors(model, [(k, ref, v) for (k, ref, _), v in zip(todo, vecs)])
            self.store.drop_other_vectors(model)

    def _learn_one(self):
        """Learn from one waiting exchange (or batch). Returns True if there was something waiting."""
        r = self.store.q("SELECT chat_id, done FROM learned WHERE saved>done "
                         "ORDER BY (chat_id LIKE 'gpt-%' OR chat_id LIKE 'cl-%'), rowid LIMIT 1")
        if not r:
            return False
        cid, done = r[0]
        chat = self.chats_mod.get_chat(cid) if self.chats_mod else None
        if not chat:
            self.store.x("DELETE FROM learned WHERE chat_id=?", (cid,))
            return True
        ex = list(_exchanges(chat.get("messages") or []))
        if done >= len(ex):
            self.store.x("UPDATE learned SET done=? WHERE chat_id=?", (len(ex), cid))
            return True
        # live chats: one exchange at a time. Imported history: several per request, so years of chats finish sooner.
        imported = cid.startswith(("gpt-", "cl-"))
        batch, size = [], 0
        for n, u, a in ex[done:]:
            batch.append((u, a))
            size += len(u) + len(a)
            if not imported or len(batch) >= 8 or size > 7000:
                break
        res = self.learn_exchange(batch, source="chat:%s" % cid)
        if res is None:
            return True  # model not available: try again later
        self.store.x("UPDATE learned SET done=? WHERE chat_id=?", (done + len(batch), cid))
        return True

    def learn_exchange(self, batch, source=""):
        """batch: [(user_text, assistant_text), ...] from one conversation, oldest first."""
        if not self.llm:
            return None
        user = "\n".join(u for u, _ in batch)
        if not user.strip():
            return []   # nothing the user said: nothing to learn
        related = []
        qv = self.embedder.embed([user[-2000:]], query=True, timeout=8)
        if qv:
            related = [mid for mid, s in self.store.similar("m", self.embedder.name, qv[0], 8) if s > 0.35]
        related += [mid for mid in self.store.keyword("m", user, 6) if mid not in related]
        shown = [m for m in (self.store.get(i) for i in related[:12]) if m]
        existing = "\n".join("#%d [%s] %s" % (m["id"], m["topic"], m["text"]) for m in shown) or "(none)"
        per = max(600, 7000 // len(batch))
        convo = "\n\n".join("User: %s\nAssistant: %s" % (u[:per], a[:per]) for u, a in batch)
        msg = "TODAY: %s\n\nEXISTING MEMORIES:\n%s\n\nLATEST EXCHANGE:\n%s" % (time.strftime("%A %Y-%m-%d"), existing, convo)
        out = self.llm([{"role": "system", "content": LEARN_PROMPT}, {"role": "user", "content": msg}], LEARN_SCHEMA,
                       500 if len(batch) == 1 else 1200)
        if out is None:
            return None
        ops = out.get("memories")
        known = {m["id"] for m in shown}
        new = []
        for op in (ops if isinstance(ops, list) else [])[:3 if len(batch) == 1 else 8]:
            if not isinstance(op, dict):
                continue
            kind, text, topic = op.get("op"), acceptable(op.get("text")), op.get("topic")
            # update/delete only for memories the model was actually shown: an id it made up must not hit a stranger
            target = op.get("id") if isinstance(op.get("id"), int) and op.get("id") in known else None
            if kind == "add" and text:
                mid = self.add_checked(text, topic, source)
                if mid:
                    new.append({"op": "add", "id": mid, "text": text, "topic": topic})
            elif kind == "update" and target and text:
                self.store.update(target, text=text, topic=topic, archived=False)
                new.append({"op": "update", "id": target, "text": text, "topic": topic})
            elif kind == "delete" and target:
                m = self.store.get(target)
                if m and not m["pinned"] and self.store.delete(target):
                    new.append({"op": "delete", "id": target, "text": m["text"], "topic": m["topic"]})
        if new:
            self.seq += 1
            self.last_learned = [dict(e, seq=self.seq) for e in new]
        return new

    def add_checked(self, text, topic="other", source="", pinned=False):
        """Add, unless something with the same meaning is already there. Then the existing memory is kept and only
        marked as confirmed again: it may be pinned, hand-edited, or say more than the new sentence does. (A changed
        fact arrives as an explicit update from the learner, not through here.)"""
        qv = self.embedder.embed([text]) if self.embedder.ready() else None
        if qv:
            for mid, s in self.store.similar("m", self.embedder.name, qv[0], 1):
                if s >= SAME:
                    self.store.touch(mid)
                    if pinned:
                        self.store.update(mid, pinned=True)
                    return mid
        mid = self.store.add(text, topic, source, pinned)
        if mid and qv:
            self.store.put_vectors(self.embedder.name, [("m", mid, qv[0])])
        return mid

    def add_manual(self, text, topic="other", pinned=False):
        """A memory typed by the user (6:set, /remember)."""
        mid = self.add_checked(text, topic, "manual", pinned)
        if mid:
            self.seq += 1
            self.last_learned = [{"op": "add", "id": mid, "text": " ".join(str(text).split()), "topic": topic,
                                  "seq": self.seq, "manual": True}]
        return mid

    # ---- tidying: merge near-duplicates (with the model's judgement), archive long-past events
    def _tidy_one(self):
        """One small step of housekeeping, at most every 20 s and only when nothing is waiting to be learned."""
        if _now() - self._tidy_at < 20 or not self.llm:
            return
        self._tidy_at = _now()
        self.archive_stale()
        ver = (self.store.ver, len(self.store.index("m", self.embedder.name)))
        if ver == self._tidy_ver or not self.embedder.ready():
            return
        seen = {(a, b) for a, b in self.store.q("SELECT a, b FROM reviewed")}
        pairs = sorted(((s, p) for p, s in self._pairs(2, TIDY_SIM).items() if p not in seen), reverse=True)
        if not pairs:
            self._tidy_ver = ver   # nothing to look at until the memories change
            return
        _, (a, b) = pairs[0]
        group = [a, b] + [x for _, (p, q) in pairs[1:] for x in (p, q) if a in (p, q) or b in (p, q)]
        group = list(dict.fromkeys(group))[:5]
        res = self.merge_group(group)
        if res is None:
            return   # no model right now: ask again later
        ids = sorted(group)   # asked once: these are never shown to the model as a group again
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                self.store.x("INSERT OR IGNORE INTO reviewed(a, b) VALUES(?,?)", (x, y))

    def merge_group(self, ids):
        """Show a handful of similar memories to the model and apply the merges it is sure about. The memory that
        loses is archived, not deleted. Returns the list of changes, or None if no model answered."""
        rows = self.store.get_many(ids)
        mems = [rows[i] for i in ids if i in rows and not rows[i]["archived"]]
        if len(mems) < 2:
            return []
        listing = "\n".join("#%d (%s%s) %s" % (m["id"], time.strftime("%Y-%m-%d", time.localtime(m["updated"] or 0)),
                                               ", pinned" if m["pinned"] else "", m["text"]) for m in mems)
        out = self.llm([{"role": "system", "content": TIDY_PROMPT}, {"role": "user", "content": listing}], TIDY_SCHEMA, 400)
        if out is None:
            return None
        valid, done, gone = {m["id"]: m for m in mems}, [], set()
        for op in (out.get("merge") if isinstance(out.get("merge"), list) else [])[:3]:
            if not isinstance(op, dict):
                continue
            keep, text = op.get("keep"), acceptable(op.get("text"))
            drop = [d for d in (op.get("drop") if isinstance(op.get("drop"), list) else [])
                    if d in valid and d != keep and d not in gone and not valid[d]["pinned"]]
            if keep not in valid or keep in gone or not drop or not text:
                continue
            self.store.update(keep, text=text)
            for d in drop:
                self.store.update(d, archived=True)
                gone.add(d)
            done.append({"op": "update", "id": keep, "text": text, "topic": valid[keep]["topic"]})
        if done:
            self.seq += 1
            self.last_learned = [dict(e, seq=self.seq) for e in done]
        return done

    def archive_stale(self, today=None):
        """Events whose (latest) date passed more than EVENT_STALE_DAYS ago stop being recalled. They stay in the
        list as archived and can be restored. Pinned memories are never touched."""
        today = today or _now()
        n = 0
        for mid, text in self.store.q("SELECT id, text FROM memories WHERE topic='event' AND archived=0 AND pinned=0"):
            dates = []
            for y, mo, d in re.findall(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text):
                try:
                    dates.append(time.mktime((int(y), int(mo), int(d), 12, 0, 0, 0, 0, -1)))
                except (ValueError, OverflowError):
                    pass
            if dates and max(dates) < today - EVENT_STALE_DAYS * 86400:
                self.store.update(mid, archived=True)
                n += 1
        return n

    # ---- moving the memory home
    def move(self, dst, mode):
        """mode "move": copy the whole memory folder to dst (must be empty or new) and switch to it; the old folder
        is left in place as a backup. mode "use": switch to an existing memory folder at dst (or start a new
        empty memory there if it's empty)."""
        dst = str(dst or "").strip().strip('"')
        if not dst or not os.path.isabs(os.path.expanduser(dst)) or len(os.path.abspath(os.path.expanduser(dst))) < 4:
            return False, "pick a folder with its full path, e.g. E:\\ai-memory"
        dst = os.path.abspath(os.path.expanduser(dst))
        drive = os.path.splitdrive(dst)[0]
        if drive and not os.path.exists(drive + "\\"):
            return False, "drive %s isn't connected" % drive
        if os.path.normcase(dst) == os.path.normcase(self.home):
            return False, "that's already the memory folder"
        if self.moving or self.job.get("running"):
            return False, "wait for the running import or move to finish first"
        old = self.home
        inside = os.path.normcase(dst).startswith(os.path.normcase(old).rstrip("\\/") + os.sep)
        has_db = os.path.exists(os.path.join(dst, "memory.db"))
        if mode == "move":
            if not self.ok():
                return False, "the current memory folder isn't available, so there's nothing to copy. Use \"use existing\" instead"
            if inside:
                return False, "the new folder can't be inside the current memory folder"
            if os.path.isdir(dst) and os.listdir(dst):
                return False, "%s isn't empty. Pick an empty or new folder (or \"use existing\" if it already holds a memory)" % dst
            need = self.store.size()
            try:
                free = shutil.disk_usage(drive + "\\" if drive else os.path.dirname(dst)).free
            except OSError as e:
                return False, "can't read %s: %s" % (drive or dst, e)
            if free < need * 1.1 + 50e6:
                return False, "not enough space on %s" % drive
            self.moving = True
            try:
                n_before = self.store.q("SELECT count(*) FROM memories")[0][0]
                self.store.close()
                shutil.copytree(old, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("import-*.tmp", "*.tmp"))
                test = Store(dst)
                if not test.open(create=False) or test.q("SELECT count(*) FROM memories")[0][0] != n_before:
                    test.close()
                    self.open_home(old)
                    return False, "the copy didn't verify, so nothing changed"
                test.close()
                self.settings["memory_dir"] = dst
                self.save_settings()
                self.open_home(dst)
                return True, "moved: memory now lives in %s. The old copy is still in %s as a backup; delete it when you're happy." % (dst, old)
            except (OSError, sqlite3.Error) as e:
                self.open_home(old)
                return False, "copy failed (%s), nothing changed" % e
            finally:
                self.moving = False
        if mode == "use":
            if os.path.isdir(dst) and os.listdir(dst) and not has_db:
                return False, "%s has other files and no memory.db in it" % dst
            try:
                os.makedirs(dst, exist_ok=True)
            except OSError as e:
                return False, "couldn't create %s: %s" % (dst, e)
            probe = Store(dst)
            if not probe.open(create=True):   # check it opens before giving up the memory we have
                return False, probe.error or "couldn't open a memory in %s" % dst
            probe.close()
            self.settings["memory_dir"] = dst
            self.save_settings()
            self.open_home(dst)
            return True, ("switched to the memory in %s" % dst) if has_db else ("started a new, empty memory in %s" % dst)
        return False, "mode must be move or use"

    # ---- importing other services' exports (ChatGPT, Claude) -----------------------------------------------
    def import_file(self, path, learn=True, cleanup=False):
        """Background job: read a .zip/.json export, store every conversation as a chat (searchable at once) and
        queue it for learning. Progress is in self.job, which the 6:set tab shows."""
        if self.job.get("running"):
            return False, "an import is already running"
        self.job = {"running": True, "msg": "reading the export…", "chats": 0, "exchanges": 0, "memories": 0,
                    "skipped": 0, "started": _now(), "learn": bool(learn)}
        threading.Thread(target=self._import_run, args=(path, learn, cleanup), daemon=True).start()
        return True, "importing…"

    def _import_run(self, path, learn, cleanup):
        j = self.job
        try:
            found = False
            for kind, payload, name in importers.read_any(path):
                found = True
                if kind == "own":
                    j["memories"] += self.import_(payload)
                elif kind == "memories":
                    j["msg"] = "adding saved memories…"
                    for text in payload:
                        if self.store.add(text, "other", "import"):
                            j["memories"] += 1
                    if learn:
                        self._queue_tidy(payload)
                else:
                    label = "ChatGPT" if kind == "chatgpt" else "Claude"
                    for chat in importers.conversations(kind, payload):
                        j["msg"] = "%s: %s" % (label, chat["title"][len(label) + 3:][:60])
                        if self.chats_mod.get_chat(chat["id"]):
                            j["skipped"] += 1  # imported before: keep it, don't relearn
                            continue
                        self.chats_mod.write_raw(chat)
                        self.note_chat(chat, learn=learn and self.learn_on)
                        j["chats"] += 1
                        j["exchanges"] += sum(1 for _ in _exchanges(chat["messages"]))
            if not found:
                j["msg"] = "nothing importable found (expected a ChatGPT or Claude export: a .zip with conversations.json)"
                j["error"] = True
            else:
                j["msg"] = "done: %d chats (%d exchanges), %d memories%s%s" % (
                    j["chats"], j["exchanges"], j["memories"], ", %d already imported" % j["skipped"] if j["skipped"] else "",
                    ". Learning from them in the background while the model is idle" if j["chats"] and learn and self.learn_on else "")
        except Exception as e:
            done = (" (%d chats were imported before it stopped; importing the same file again continues)" % j["chats"]) if j["chats"] else ""
            j["msg"], j["error"] = "import failed: %s%s" % (str(e)[:200] or e.__class__.__name__, done), True
        finally:
            j["running"] = False
            if cleanup:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def paste_memories(self, text):
        """Memories copied from another service's memory page, one per line: each is saved right away, then
        (when a model is available) a tidy-up pass rewrites them as proper memories with topics."""
        lines = [re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s*", "", l).strip() for l in str(text or "").splitlines()]
        lines = [l for l in lines if len(l) > 3][:2000]
        n = sum(1 for l in lines if self.store.add(l, "other", "import"))
        self._queue_tidy(lines)
        return n

    def _queue_tidy(self, lines):
        """Store pasted/imported memories as a small 'chat' so the learner files them under the right topics."""
        lines = list(lines)
        if not (lines and self.learn_on and self.chats_mod):
            return
        for i in range(0, len(lines), 40):
            part = lines[i:i + 40]
            chat = {"id": "cl-memories%08x%04d" % (int(_now()) & 0xffffffff, i // 40), "title": "imported memories",
                    "created": _now(), "updated": _now(),
                    "messages": [{"role": "user", "content": "Remember these facts about me (from another assistant):\n" + "\n".join("- " + l for l in part)},
                                 {"role": "assistant", "content": "Noted."}]}
            self.chats_mod.write_raw(chat)
            self.note_chat(chat, learn=True)

    # ---- export / import: plain JSON, readable by anything
    def export(self):
        rows = self.store.q("SELECT %s FROM memories ORDER BY id" % Store.COLS) if self.ok() else []
        return {"format": "command-center-memory", "version": 1, "exported": int(_now()),
                "memories": [self.store.row(r) for r in rows]}

    def import_(self, data):
        if not self.ok():
            return 0
        n = 0
        mems = data.get("memories") if isinstance(data, dict) else None
        for m in (mems if isinstance(mems, list) else [])[:100000]:
            if isinstance(m, dict) and m.get("text"):
                mid = self.store.add(m["text"], m.get("topic", "other"), m.get("source", "import"), m.get("pinned", False))
                if mid:
                    n += 1
                    if m.get("archived"):
                        self.store.update(mid, archived=True)
        return n

    def shutdown(self):
        self.stop.set()
        if self.embedder:
            self.embedder.stop()


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def cline_task(messages):
    """The user's request in a Cline conversation: the <task> in its first user message."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            t = _text(m.get("content"))
            g = re.search(r"<task>\s*(.*?)\s*</task>", t, re.S)
            t = g.group(1) if g else re.split(r"<environment_details>", t)[0]
            return t.strip()[:4000]
    return ""


def inject(messages, text, where="last"):
    """Put the memory block at the start of the last (chat) or first (Cline task) user message. Everything before
    that message stays byte-identical, so the model server's prompt cache still covers it."""
    idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
    if not idx:
        return messages
    return _inject_at(messages, idx[-1] if where == "last" else idx[0], text)


def _inject_at(messages, i, text):
    block = "<memory>\n%s\n</memory>" % text
    m = dict(messages[i])
    c = m.get("content")
    if isinstance(c, list):
        m["content"] = [{"type": "text", "text": block}] + c
    else:
        m["content"] = block + "\n\n" + (c or "")
    out = list(messages)
    out[i] = m
    return out


def _exchanges(msgs):
    """(n, user_text, assistant_text) for each user message followed by an assistant reply."""
    n = 0
    for i in range(len(msgs) - 1):
        a, b = msgs[i], msgs[i + 1]
        if isinstance(a, dict) and isinstance(b, dict) and a.get("role") == "user" and b.get("role") == "assistant":
            u, r = _text(a.get("content")).strip(), _text(b.get("content")).strip()
            if u or r:
                yield n, u, r
            n += 1


M = Memory()
