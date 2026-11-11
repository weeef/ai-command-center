"""Shared fakes for the test suite: a model server, an embedding server, and a throwaway state folder.

Nothing here touches the real memory folder, api-key.txt, settings.json or ports 8090/8091/8099: every test runs
against temp folders and free ports picked at start-up.
"""
import hashlib
import http.server
import json
import os
import re
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.join(os.path.dirname(HERE), "command-center")
if CC not in sys.path:
    sys.path.insert(0, CC)

STOP = set("the a an and or of to in is are was were for with my i me what when where which who why how do does "
           "you your user user's it its this that on at as be have has".split())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fake_vector(text, dim=96):
    """A deterministic bag-of-words 'embedding': texts that share words get a high cosine similarity."""
    if "Query: " in text:
        text = text.split("Query: ", 1)[1]
    v = [0.0] * dim
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP:
            continue
        w = w[:-1] if len(w) > 3 and w.endswith("s") else w
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class _Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeEmbed:
    """Answers like llama-server --embedding for the model name it is given."""

    def __init__(self, model_name="fake-embed.gguf"):
        outer = self
        self.calls = 0

        class H(_Quiet):
            def do_GET(self):
                if self.path == "/health":
                    return self.send(200, {"status": "ok"})
                if self.path == "/props":
                    return self.send(200, {"model_path": "C:\\models\\" + model_name})
                self.send(404, {})

            def do_POST(self):
                outer.calls += 1
                inp = self.body().get("input") or []
                self.send(200, {"data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(inp)]})

        self.port = free_port()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FakeLlama:
    """A stand-in for llama-server: records every chat request, streams a canned reply (or scripted tool calls)."""

    def __init__(self, key="test-key"):
        outer = self
        self.key = key
        self.requests = []        # request bodies of /v1/chat/completions, in order
        self.auth = []            # the Authorization header of each
        self.script = []          # per request: {"text": ...} or {"tool_calls": [(name, args_json), ...]}
        self.reply = "Hello from the fake model."
        self.status = 200
        self.die_mid_stream = False

        class H(_Quiet):
            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/health":
                    return self.send(200, {"status": "ok"})
                if p == "/props":
                    return self.send(200, {"model_path": "C:\\models\\fake-standard.gguf",
                                           "default_generation_settings": {"n_ctx": 4096}, "modalities": {"vision": False},
                                           "build_info": "b1-test"})
                if p == "/v1/models":
                    return self.send(200, {"data": [{"id": "fake-standard"}]})
                if p == "/slots":
                    return self.send(200, [{"id": 0, "is_processing": False}])
                self.send(404, {})

            def do_POST(self):
                req = self.body()
                outer.requests.append(req)
                outer.auth.append(self.headers.get("Authorization"))
                if outer.status != 200:
                    return self.send(outer.status, {"error": {"message": "model says no"}})
                step = outer.script.pop(0) if outer.script else {"text": outer.reply}
                if not req.get("stream"):
                    return self.send(200, {"choices": [{"message": {"role": "assistant", "content": step.get("text", "")}}],
                                           "timings": {"prompt_n": 5, "cache_n": 0, "predicted_n": 3}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def ev(obj):
                    raw = b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                    self.wfile.flush()

                if step.get("tool_calls"):
                    for i, (name, args) in enumerate(step["tool_calls"]):
                        half = len(args) // 2   # arguments arrive in pieces, like the real thing
                        ev({"choices": [{"delta": {"tool_calls": [{"index": i, "id": "call_%d" % i,
                                                                   "function": {"name": name, "arguments": args[:half]}}]}}]})
                        ev({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": args[half:]}}]}}]})
                    ev({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
                else:
                    words = step.get("text", "").split(" ")
                    for i, w in enumerate(words):
                        ev({"choices": [{"delta": {"content": w + ("" if i == len(words) - 1 else " ")}}]})
                        if outer.die_mid_stream and i == 1:
                            self.connection.close()   # the model server crashes mid-reply
                            return
                    ev({"choices": [{"delta": {}, "finish_reason": "stop"}],
                        "timings": {"prompt_n": 7, "cache_n": 40, "predicted_n": len(words),
                                    "prompt_per_second": 1000.0, "predicted_per_second": 50.0}})
                ev(b"[DONE]")
                self.wfile.write(b"0\r\n\r\n")

        self.port = free_port()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FakeChats:
    """The part of chats.py that memory.py uses, in RAM."""

    def __init__(self):
        self.DIR = ""
        self.store = {}

    def get_chat(self, cid):
        return self.store.get(cid)

    def write_raw(self, chat):
        self.store[chat["id"]] = chat
        return True
