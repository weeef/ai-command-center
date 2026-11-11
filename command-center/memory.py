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
            matches are added to the prompt as a system message.
  learn   - after a reply is saved, the active model is asked (in the background, when it's idle) to pull durable
            facts out of the exchange: add new memories, update ones that changed, delete ones proven wrong.
"""
import array
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time

try:
    import numpy as np
except ImportError:  # still works, just slower on very large memories
    np = None

TOPICS = ("you", "preference", "project", "people", "fact", "event", "howto", "other")
EMBED_PORT = 8091
NOWIN = 0x08000000
QUERY_INSTRUCT = ("Instruct: Given a message from the user, retrieve memories about them and earlier conversation "
                  "snippets that help answer it\nQuery: ")
# Scores are cosine similarity for the embedding model. Below these, a match is noise.
MEM_MIN, CHUNK_MIN = 0.36, 0.45
README = """This folder is the long-term memory of the local AI command center.

memory.db  - every memory (plain text) plus the search index. Any model can use it.
chats/     - saved chats.

To move it: in the command center open 4:mem -> location, and pick the new folder (it copies everything and
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


def fts_query(text):
    words = []
    for w in re.findall(r"[\w']{3,}", (text or "").lower()):
        w = w.strip("'")
        if w and w not in words and w not in _STOP:
            words.append(w)
    return " OR ".join('"%s"' % w.replace('"', "") for w in words[:16])


_STOP = set("the and for you are was were what when where which who why how this that with have has had not but "
            "your from they them their there then than can could would should will just about into out all any "
            "its it's i'm i've don't doesn't did does get got make made want like also some more most very".split())


# ---------------------------------------------------------------------------------------------- embedding server
class Embedder:
    """A small embedding model in its own llama-server on 127.0.0.1:8091, CPU only, so it never takes VRAM from the
    chat model. Started on first use, stopped when the command center exits."""

    def __init__(self, exe, model_path):
        self.exe, self.model_path, self.proc = exe, model_path, None
        self.lock = threading.Lock()
        self.last_start = 0
        self.error = None

    @property
    def name(self):
        return os.path.basename(self.model_path or "") or "none"

    def available(self):
        return bool(self.model_path) and os.path.exists(self.model_path) and os.path.exists(self.exe)

    def _get(self, path, timeout=1.0):
        c = http.client.HTTPConnection("127.0.0.1", EMBED_PORT, timeout=timeout)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def ready(self):
        try:
            return self._get("/health")[0] == 200
        except OSError:
            return False

    def ensure(self):
        """Start the server if it isn't up. Returns True once it answers."""
        if self.ready():
            return True
        if not self.available():
            self.error = "embedding model not found: " + (self.model_path or "(none set)")
            return False
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False  # still loading
            if _now() - self.last_start < 20:
                return False
            self.last_start = _now()
            env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")  # CPU only: the GPU belongs to the chat model
            self.proc = subprocess.Popen(
                [self.exe, "-m", self.model_path, "--embedding", "--pooling", "last",
                 "--host", "127.0.0.1", "--port", str(EMBED_PORT), "-ngl", "0",
                 "-c", "8192", "-b", "8192", "-ub", "8192", "-np", "1", "-t", "6"],
                env=env, creationflags=NOWIN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.error = None
        return False

    def embed(self, texts, query=False, timeout=60):
        if not texts or not self.ensure():
            return None
        inp = [(QUERY_INSTRUCT + t if query else t)[:6000] for t in texts]
        c = http.client.HTTPConnection("127.0.0.1", EMBED_PORT, timeout=timeout)
        try:
            c.request("POST", "/v1/embeddings", json.dumps({"input": inp, "model": self.name}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                self.error = "embedding failed: HTTP %d" % r.status
                return None
            data = sorted(json.loads(body)["data"], key=lambda d: d["index"])
            return [_norm(d["embedding"]) for d in data]
        except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
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
  pinned INTEGER NOT NULL DEFAULT 0);
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
"""


class Store:
    def __init__(self, home):
        self.home = home
        self.db = None
        self.lock = threading.RLock()
        self.error = None
        self._cache = {}   # (kind, model) -> (ids, matrix)
        self._ver = 0

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
                os.makedirs(self.home, exist_ok=True)
            try:
                self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                self.db.executescript(SCHEMA)
                self.db.execute("INSERT OR IGNORE INTO meta VALUES('created', ?)", (str(int(_now())),))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema', '1')")
                self.db.commit()
            except sqlite3.Error as e:
                self.db = None
                self.error = "could not open %s: %s" % (self.path, e)
                return False
            os.makedirs(os.path.join(self.home, "chats"), exist_ok=True)
            readme = os.path.join(self.home, "README.txt")
            if not os.path.exists(readme):
                with open(readme, "w", encoding="utf-8") as f:
                    f.write(README)
            self.error = None
            return True

    def close(self):
        with self.lock:
            if self.db:
                self.db.close()
                self.db = None
            self._cache.clear()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ---- memories
    def row(self, r):
        return {"id": r[0], "text": r[1], "topic": r[2], "created": r[3], "updated": r[4], "source": r[5],
                "uses": r[6], "last_used": r[7], "pinned": bool(r[8])}

    COLS = "id, text, topic, created, updated, source, uses, last_used, pinned"

    def get(self, mid):
        r = self.q("SELECT %s FROM memories WHERE id=?" % self.COLS, (mid,))
        return self.row(r[0]) if r else None

    def list(self, query="", topic="", limit=200, offset=0):
        where, args = [], []
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
        sql = "SELECT %s FROM memories m %s ORDER BY m.pinned DESC, m.updated DESC LIMIT ? OFFSET ?" % (
            ", ".join("m." + c.strip() for c in self.COLS.split(",")), ("WHERE " + " AND ".join(where)) if where else "")
        return [self.row(r) for r in self.q(sql, args + [int(limit), int(offset)])]

    def counts(self):
        m = self.q("SELECT topic, count(*) FROM memories GROUP BY topic")
        c = self.q("SELECT count(*) FROM chunks")[0][0]
        return {t: n for t, n in m}, c

    def add(self, text, topic="other", source="", pinned=False):
        text = " ".join(str(text or "").split())[:1000]
        if not text:
            return None
        topic = topic if topic in TOPICS else "other"
        dup = self.q("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,))
        if dup:
            self.x("UPDATE memories SET updated=? WHERE id=?", (_now(), dup[0][0]))
            return dup[0][0]
        t = _now()
        cur = self.x("INSERT INTO memories(text, topic, created, updated, source, pinned) VALUES(?,?,?,?,?,?)",
                     (text, topic, t, t, str(source or "")[:200], int(bool(pinned))))
        self._ver += 1
        return cur.lastrowid

    def update(self, mid, text=None, topic=None, pinned=None):
        m = self.get(mid)
        if not m:
            return False
        if text is not None and " ".join(str(text).split()):
            self.x("UPDATE memories SET text=?, updated=? WHERE id=?", (" ".join(str(text).split())[:1000], _now(), mid))
        if topic in TOPICS:
            self.x("UPDATE memories SET topic=?, updated=? WHERE id=?", (topic, _now(), mid))
        if pinned is not None:
            self.x("UPDATE memories SET pinned=? WHERE id=?", (int(bool(pinned)), mid))
        self._ver += 1
        return True

    def delete(self, mid):
        cur = self.x("DELETE FROM memories WHERE id=?", (mid,))
        self._ver += 1
        return cur.rowcount > 0

    # ---- chat snippets
    def put_chunks(self, chat_id, title, exchanges):
        have = {r[0] for r in self.q("SELECT n FROM chunks WHERE chat_id=?", (chat_id,))}
        with self.lock:
            for n, ts, text in exchanges:
                if n not in have:
                    self.db.execute("INSERT OR IGNORE INTO chunks(chat_id, n, ts, title, text) VALUES(?,?,?,?,?)",
                                    (chat_id, n, ts, title, text))
            self.db.execute("UPDATE chunks SET title=? WHERE chat_id=?", (title, chat_id))
            self.db.commit()
        self._ver += 1

    def drop_chat(self, chat_id):
        self.x("DELETE FROM chunks WHERE chat_id=?", (chat_id,))
        self.x("DELETE FROM learned WHERE chat_id=?", (chat_id,))
        self._ver += 1

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
            self.db.executemany("INSERT OR REPLACE INTO vectors(kind, ref, model, vec) VALUES(?,?,?,?)",
                                [(k, ref, model, _vec_bytes(v)) for k, ref, v in items])
            self.db.commit()
        self._ver += 1

    def drop_other_vectors(self, model):
        self.x("DELETE FROM vectors WHERE model<>?", (model,))

    def matrix(self, kind, model):
        key = (kind, model)
        c = self._cache.get(key)
        if c and c[0] == self._ver:
            return c[1], c[2]
        rows = self.q("SELECT ref, vec FROM vectors WHERE kind=? AND model=?", (kind, model))
        ids = [r[0] for r in rows]
        if np is not None:
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else None
        else:
            mat = [_vec_from(r[1]) for r in rows]
        self._cache[key] = (self._ver, ids, mat)
        return ids, mat

    def similar(self, kind, model, qv, top=20):
        ids, mat = self.matrix(kind, model)
        if not ids:
            return []
        if np is not None:
            s = mat @ np.asarray(qv, dtype=np.float32)
            idx = np.argsort(-s)[:top]
            return [(ids[i], float(s[i])) for i in idx]
        s = [(ids[i], sum(a * b for a, b in zip(v, qv))) for i, v in enumerate(mat)]
        return sorted(s, key=lambda t: -t[1])[:top]

    def keyword(self, kind, query, top=20):
        fq = fts_query(query)
        if not fq:
            return []
        t = "memories_fts" if kind == "m" else "chunks_fts"
        try:
            return [r[0] for r in self.q("SELECT rowid FROM %s WHERE %s MATCH ? ORDER BY bm25(%s) LIMIT ?" % (t, t, t), (fq, top))]
        except sqlite3.Error:
            return []

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
the user runs, so write memories that make sense on their own, months from now, to a model that never saw this chat.

From the LATEST EXCHANGE, extract only durable things worth knowing in future conversations:
- facts about the user (who they are, their setup, devices, work, situation)        topic: you
- the user's preferences, likes, dislikes, how they want answers                    topic: preference
- projects, goals, plans, decisions, what they are building                          topic: project
- people in the user's life and facts about them                                     topic: people
- other durable facts the user stated or confirmed                                   topic: fact
- events and plans with dates                                                        topic: event
- solutions or how-tos that worked for the user                                      topic: howto
- anything else important                                                            topic: other

Rules:
- One self-contained sentence per memory, third person, about "the user" (e.g. "The user's laptop is called JordansLaptop.").
- Never write relative dates ("next week", "tomorrow", "in March"): turn them into absolute dates using TODAY, e.g. "around 2026-11-18".
- Skip small talk, greetings, one-off questions, general knowledge, and things the assistant claimed but the user didn't confirm.
- Skip anything already covered by an EXISTING MEMORY. If the exchange changes or corrects one, return {"op":"update","id":<its id>,"text":<new sentence>}.
  If it proves one wrong or the user asks to forget it, return {"op":"delete","id":<its id>}.
- If the user explicitly asks you to remember something, always save it.
- Most exchanges contain nothing worth remembering: then return {"memories": []}.
Return JSON only: {"memories": [{"op":"add","text":"...","topic":"..."}]}"""


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

    # ---- setup
    def start(self, settings, save_settings, root, llama_exe, llm, busy, chats_mod):
        self.settings, self.save_settings = settings, save_settings
        self.default_home = os.path.join(root, "memory")
        self.llm, self.busy, self.chats_mod = llm, busy, chats_mod
        em = settings.get("embed_model") or os.path.join(root, "models", "Qwen3-Embedding-0.6B-GGUF",
                                                          "Qwen3-Embedding-0.6B-Q8_0.gguf")
        self.embedder = Embedder(llama_exe, em)
        self.open_home(self.home)
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
        created = self.store.open(create=(os.path.normcase(home) == os.path.normcase(self.default_home)))
        if self.chats_mod:
            self.chats_mod.DIR = os.path.join(home, "chats")
            if created:
                self._adopt_old_chats()
        return created

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
                except (OSError, ValueError):
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
    def brief(self):
        return {"on": self.on, "ok": self.ok(), "error": None if self.ok() else (self.store.error if self.store else "not started"),
                "seq": self.seq, "learned": self.last_learned[-6:],
                "count": self.store.q("SELECT count(*) FROM memories")[0][0] if self.ok() else 0}

    def status(self):
        st = {"home": self.home, "default_home": self.default_home, "on": self.on, "learn": self.learn_on,
              "ok": self.ok(), "error": None, "moving": self.moving, "topics": list(TOPICS),
              "embed": {"model": self.embedder.name, "available": self.embedder.available(),
                        "running": self.embedder.ready(), "error": self.embedder.error},
              "numpy": np is not None, "msg": self.status_msg}
        if not self.ok():
            st["error"] = self.store.error if self.store else "not started"
            return st
        by_topic, chunks = self.store.counts()
        model = self.embedder.name
        indexed = self.store.q("SELECT count(*) FROM vectors WHERE model=?", (model,))[0][0]
        pend = self.store.q("SELECT count(*) FROM learned WHERE saved>done")[0][0]
        st.update(count=sum(by_topic.values()), by_topic=by_topic, chunks=chunks, size=self.store.size(),
                  indexed=indexed, to_index=max(0, sum(by_topic.values()) + chunks - indexed), to_learn=pend,
                  free=shutil.disk_usage(self.home).free if os.path.isdir(self.home) else None)
        return st

    def graph(self):
        """Memories for the brain view: the most used and most recent, with their topic."""
        if not self.ok():
            return []
        rows = self.store.q("SELECT id, topic FROM memories ORDER BY pinned DESC, uses DESC, updated DESC LIMIT 120")
        return [{"id": r[0], "topic": r[1]} for r in rows]

    # ---- recall: called on every chat request
    def recall(self, messages, chat_id=None, k=8):
        """Returns (system_text or None, info) for the newest user message."""
        if not (self.on and self.ok()):
            return None, {}
        q = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                q = _text(m.get("content"))
                break
        if not q.strip():
            return None, {}
        # a short previous turn helps short follow-ups ("and the laptop?") find the right memories
        prev = [m for m in messages if m.get("role") in ("user", "assistant")][-3:-1]
        ctx = " ".join(_text(m.get("content"))[:300] for m in prev)
        qv = self.embedder.embed([(ctx + "\n" + q)[-2000:]], query=True, timeout=8)
        model = self.embedder.name
        st = self.store
        picked, scores = [], {}
        if qv:
            for mid, s in st.similar("m", model, qv[0], 24):
                scores[mid] = s
        kw = st.keyword("m", q, 12)
        for i, mid in enumerate(kw):
            scores[mid] = scores.get(mid, 0.0) + (0.12 if qv else 1.0 / (1 + i))
        ranked = sorted(scores.items(), key=lambda t: -t[1])
        floor = MEM_MIN if qv else 0.0
        picked = [mid for mid, s in ranked if s >= floor][:k]
        pinned = [r[0] for r in st.q("SELECT id FROM memories WHERE pinned=1 ORDER BY updated DESC LIMIT 12")]
        ids = list(dict.fromkeys(pinned + picked))
        mems = [st.get(i) for i in ids]
        mems = [m for m in mems if m]
        # snippets of earlier chats
        chunks = []
        if qv:
            for cid, s in st.similar("c", model, qv[0], 12):
                if s < CHUNK_MIN:
                    break
                r = st.q("SELECT chat_id, ts, title, text FROM chunks WHERE id=?", (cid,))
                if r and r[0][0] != chat_id:
                    chunks.append(r[0])
                if len(chunks) >= 3:
                    break
        if mems:
            st.x("UPDATE memories SET uses=uses+1, last_used=? WHERE id IN (%s)" % ",".join("?" * len(mems)),
                 [_now()] + [m["id"] for m in mems])
        self.last_recall = [m["id"] for m in mems]
        if not mems and not chunks:
            return None, {"ids": [], "chunks": 0}
        lines = ["# Long-term memory",
                 "You have a long-term memory shared across all of the user's conversations and models. "
                 "Today is %s. Use what is relevant, naturally, without announcing it; ignore what isn't. "
                 "If a memory conflicts with what the user says now, trust the user." % time.strftime("%A %Y-%m-%d")]
        if mems:
            lines.append("\n## What you remember")
            for m in mems:
                lines.append("- [%s] %s (%s)" % (m["topic"], m["text"], time.strftime("%Y-%m-%d", time.localtime(m["updated"]))))
        if chunks:
            lines.append("\n## Possibly related earlier conversations")
            for chat, ts, title, text in chunks:
                lines.append("--- %s · \"%s\"\n%s" % (time.strftime("%Y-%m-%d", time.localtime(ts or 0)), title or "chat", text))
        return "\n".join(lines), {"ids": [m["id"] for m in mems], "chunks": len(chunks)}

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
        self.store.put_chunks(cid, chat.get("title") or "", [(n, ts, "User: %s\nAssistant: %s" % (u[:700], a[:1100])) for n, u, a in ex])
        if learn and self.learn_on:
            self.store.x("INSERT INTO learned(chat_id, saved, done) VALUES(?,?,0) "
                         "ON CONFLICT(chat_id) DO UPDATE SET saved=excluded.saved", (cid, len(ex)))
        else:
            self.store.x("INSERT INTO learned(chat_id, saved, done) VALUES(?,?,?) "
                         "ON CONFLICT(chat_id) DO UPDATE SET saved=excluded.saved, done=excluded.saved", (cid, len(ex), len(ex)))

    def forget_chat(self, cid):
        if self.ok():
            self.store.drop_chat(cid)

    # ---- background: index vectors, learn from new exchanges
    def worker(self):
        while not self.stop.wait(3):
            try:
                if not self.ok() or self.moving:
                    continue
                self._index_some()
                if self.on and self.learn_on and not self.busy():
                    self._learn_one()
            except Exception as e:  # keep the worker alive whatever happens
                self.status_msg = "memory worker: %s" % e.__class__.__name__

    def _index_some(self):
        model = self.embedder.name
        if not self.embedder.available():
            return
        todo = self.store.missing_vectors(model, 32)
        if not todo:
            return
        vecs = self.embedder.embed([t for _, _, t in todo])
        if vecs:
            self.store.put_vectors(model, [(k, ref, v) for (k, ref, _), v in zip(todo, vecs)])
            self.store.drop_other_vectors(model)

    def _learn_one(self):
        r = self.store.q("SELECT chat_id, done FROM learned WHERE saved>done ORDER BY rowid LIMIT 1")
        if not r:
            return
        cid, done = r[0]
        chat = self.chats_mod.get_chat(cid) if self.chats_mod else None
        if not chat:
            self.store.x("DELETE FROM learned WHERE chat_id=?", (cid,))
            return
        ex = list(_exchanges(chat.get("messages") or []))
        if done >= len(ex):
            self.store.x("UPDATE learned SET done=? WHERE chat_id=?", (len(ex), cid))
            return
        n, u, a = ex[done]
        res = self.learn_exchange(u, a, source="chat:%s" % cid)
        if res is None:
            return  # model not available: try again later
        self.store.x("UPDATE learned SET done=? WHERE chat_id=?", (done + 1, cid))

    def learn_exchange(self, user, assistant, source=""):
        if not self.llm:
            return None
        related = []
        qv = self.embedder.embed([user[-2000:]], query=True, timeout=8)
        if qv:
            related = [mid for mid, s in self.store.similar("m", self.embedder.name, qv[0], 8) if s > 0.35]
        related += [mid for mid in self.store.keyword("m", user, 6) if mid not in related]
        existing = [self.store.get(i) for i in related[:12]]
        existing = "\n".join("#%d [%s] %s" % (m["id"], m["topic"], m["text"]) for m in existing if m) or "(none)"
        msg = "TODAY: %s\n\nEXISTING MEMORIES:\n%s\n\nLATEST EXCHANGE:\nUser: %s\nAssistant: %s" % (
            time.strftime("%A %Y-%m-%d"), existing, user[:4000], assistant[:3000])
        out = self.llm([{"role": "system", "content": LEARN_PROMPT}, {"role": "user", "content": msg}], LEARN_SCHEMA, 700)
        if out is None:
            return None
        new = []
        for op in (out.get("memories") or [])[:8]:
            if not isinstance(op, dict):
                continue
            kind, text, topic = op.get("op"), op.get("text"), op.get("topic")
            if kind == "add" and text:
                mid = self.add_checked(text, topic, source)
                if mid:
                    new.append({"op": "add", "id": mid, "text": text, "topic": topic})
            elif kind == "update" and isinstance(op.get("id"), int) and text and self.store.get(op["id"]):
                self.store.update(op["id"], text=text, topic=topic)
                new.append({"op": "update", "id": op["id"], "text": text, "topic": topic})
            elif kind == "delete" and isinstance(op.get("id"), int):
                m = self.store.get(op["id"])
                if m and not m["pinned"] and self.store.delete(op["id"]):
                    new.append({"op": "delete", "id": op["id"], "text": m["text"], "topic": m["topic"]})
        if new:
            self.seq += 1
            self.last_learned = [dict(e, seq=self.seq) for e in new]
        return new

    def add_checked(self, text, topic="other", source="", pinned=False):
        """Add, unless something with the same meaning is already there (then refresh that one instead)."""
        qv = self.embedder.embed([text]) if self.embedder.ready() else None
        if qv:
            for mid, s in self.store.similar("m", self.embedder.name, qv[0], 1):
                if s >= 0.93:
                    self.store.update(mid, text=text)
                    return mid
        mid = self.store.add(text, topic, source, pinned)
        if mid and qv:
            self.store.put_vectors(self.embedder.name, [("m", mid, qv[0])])
        return mid

    # ---- moving the memory home
    def move(self, dst, mode):
        """mode "move": copy the whole memory folder to dst (must be empty or new) and switch to it; the old folder
        is left in place as a backup. mode "use": switch to an existing memory folder at dst (or start a new
        empty memory there if it's empty)."""
        dst = os.path.abspath(os.path.expanduser(str(dst or "").strip().strip('"')))
        if not dst or len(dst) < 4:
            return False, "pick a folder, e.g. E:\\ai-memory"
        drive = os.path.splitdrive(dst)[0]
        if drive and not os.path.exists(drive + "\\"):
            return False, "drive %s isn't connected" % drive
        if os.path.normcase(dst) == os.path.normcase(self.home):
            return False, "that's already the memory folder"
        old = self.home
        has_db = os.path.exists(os.path.join(dst, "memory.db"))
        if mode == "move":
            if not self.ok():
                return False, "the current memory folder isn't available, so there's nothing to copy. Use \"use existing\" instead"
            if os.path.isdir(dst) and os.listdir(dst):
                return False, "%s isn't empty. Pick an empty or new folder (or \"use existing\" if it already holds a memory)" % dst
            need = self.store.size()
            free = shutil.disk_usage(drive + "\\" if drive else dst).free
            if free < need * 1.1 + 50e6:
                return False, "not enough space on %s" % drive
            self.moving = True
            try:
                n_before = self.store.q("SELECT count(*) FROM memories")[0][0]
                self.store.close()
                shutil.copytree(old, dst, dirs_exist_ok=True)
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
            except OSError as e:
                self.open_home(old)
                return False, "copy failed (%s), nothing changed" % e
            finally:
                self.moving = False
        if mode == "use":
            if os.path.isdir(dst) and os.listdir(dst) and not has_db:
                return False, "%s has other files and no memory.db in it" % dst
            os.makedirs(dst, exist_ok=True)
            self.settings["memory_dir"] = dst
            self.save_settings()
            if self.store:
                self.store.close()
            self.store = Store(dst)
            self.store.open(create=True)
            if self.chats_mod:
                self.chats_mod.DIR = os.path.join(dst, "chats")
            return True, ("switched to the memory in %s" % dst) if has_db else ("started a new, empty memory in %s" % dst)
        return False, "mode must be move or use"

    # ---- export / import: plain JSON, readable by anything
    def export(self):
        rows = self.store.q("SELECT %s FROM memories ORDER BY id" % Store.COLS) if self.ok() else []
        return {"format": "command-center-memory", "version": 1, "exported": int(_now()),
                "memories": [self.store.row(r) for r in rows]}

    def import_(self, data):
        if not self.ok():
            return 0
        n = 0
        for m in (data.get("memories") or [])[:100000]:
            if isinstance(m, dict) and m.get("text"):
                if self.store.add(m["text"], m.get("topic", "other"), m.get("source", "import"), m.get("pinned", False)):
                    n += 1
        return n

    def shutdown(self):
        self.stop.set()
        if self.embedder:
            self.embedder.stop()


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _exchanges(msgs):
    """(n, user_text, assistant_text) for each user message followed by an assistant reply."""
    n = 0
    for i in range(len(msgs) - 1):
        if msgs[i].get("role") == "user" and msgs[i + 1].get("role") == "assistant":
            u, a = _text(msgs[i].get("content")).strip(), _text(msgs[i + 1].get("content")).strip()
            if u or a:
                yield n, u, a
            n += 1


M = Memory()
