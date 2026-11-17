"""Smoke tests for the command center. Standard library only; nothing is started except the request handler.

    python tests\\smoke.py               all groups
    python tests\\smoke.py guard phone   only these

The real Handler runs on a test port inside this process, without the background threads (no model, no memory,
no updater), so a few routes that need memory answer 500 here; the tests only use routes that don't. The cloud
call is stubbed: no key is used and nothing is billed. The `key` group puts the API key on the clipboard for a
moment and then restores the text that was there.
"""
import http.client
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

PORT = 8197
os.environ["CC_PORT"] = str(PORT)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "command-center"))
import server  # noqa: E402
from server import chats, cloud, workspaces  # noqa: E402
import importers  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cc-smoke-")
PHONE = {"Host": "mypc.tail1234.ts.net", "Tailscale-User-Login": "owner@example.com"}
fails = []


def check(name, cond, got=None):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (got,)))
    if not cond:
        fails.append(name)


def req(method, path, body=None, **headers):
    """One request. Header names may use _ for - (Sec_Fetch_Site=...). -> (status, parsed JSON or raw bytes)"""
    hd = {"Host": "127.0.0.1:%d" % PORT}
    hd.update({k.replace("_", "-"): v for k, v in headers.items()})
    if body is not None:
        hd.setdefault("Content-Type", "application/json")
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=body, headers=hd)
    r = c.getresponse()
    d = r.read()
    c.close()
    try:
        d = json.loads(d)
    except ValueError:
        pass
    return r.status, d


def as_phone(on):
    """Pretend Tailscale is up and this PC is mypc.tail1234.ts.net, signed in as owner@example.com."""
    workspaces._dns_cache.update(t=time.time() + 3600 if on else 0, v=("mypc.tail1234.ts.net", "owner@example.com"))


def clipboard():
    return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                          capture_output=True, text=True).stdout.strip("\r\n")


# ---------------------------------------------------------------- groups
def t_modules():
    """importers, chats, cloud, workspaces on their own."""
    convs = [{"title": "t%d" % i, "create_time": 1700000000 + i, "mapping": {"a": {
        "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["hi %d é" % i]},
                    "create_time": 1700000000}, "parent": None, "children": []}}} for i in range(50)]
    raw = json.dumps(convs).encode()
    check("importers: array streamed in 7-character reads", list(importers.iter_array(io.BytesIO(raw), chunk=7)) == convs)
    z = os.path.join(TMP, "export.zip")
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations-000.json", raw)
    n = sum(len(list(importers.conversations(kind, payload))) for kind, payload, _ in importers.read_any(z))
    check("importers: split export (conversations-000.json) read", n == 50, n)

    old_dir, chats.DIR = chats.DIR, os.path.join(TMP, "chats")
    try:
        ok = chats.save_chat({"id": "abc123", "title": "x", "messages": [{"role": "user", "content": "h"}]})[0]
        before = chats.list_chats()[0]["updated"] if ok else None
        chats.rename_chat("abc123", "y")
        after = chats.list_chats()[0]
        check("chats: rename changes the title and keeps the place", ok and after["title"] == "y" and after["updated"] == before, after)
    finally:
        chats.DIR = old_dir

    check("cloud: cut-off tool arguments recovered", cloud.parse_args("web_search", '{"query": "abc') == {"query": "abc"})
    check("cloud: good tool arguments unchanged", cloud.parse_args("web_search", '{"query": "ok"}') == {"query": "ok"})

    bat = os.path.join(TMP, "echo args.bat")
    with open(bat, "w") as f:
        f.write("@echo off\r\necho [%1]\r\n")
    d = os.path.join(TMP, "a&b") + "&echo PWNED"
    out = subprocess.run(workspaces._cmd_line(bat, [d]), capture_output=True, text=True).stdout.strip()
    check("workspaces: a folder named a&b&echo PWNED is one argument and runs nothing", out == '["%s"]' % d, out)


def t_smuggle():
    """A refused request's body must not be parsed as a second request."""
    inner = json.dumps({"id": "smuggled-chat", "title": "smuggled", "messages": []}).encode()
    inner = (b"POST /api/chats/save HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
             b"Content-Length: %d\r\n\r\n" % (PORT, len(inner))) + inner
    for name, extra in (("bad Origin", b"Origin: https://evil.example\r\nContent-Type: application/json\r\n"),
                        ("wrong Content-Type", b"Content-Type: text/plain\r\n")):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s.sendall(b"POST /api/chats/save HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%sContent-Length: %d\r\n\r\n" % (PORT, extra, len(inner)) + inner)
        got = b""
        try:
            while True:
                b = s.recv(65536)
                if not b:
                    break
                got += b
        except OSError:
            pass
        s.close()
        check("smuggling (%s): one refusal, then the connection closes" % name,
              got.count(b"HTTP/1.") == 1 and got.split(b" ")[1][:1] == b"4", got[:200])
    check("smuggling: the inner request was not carried out", chats.get_chat("smuggled-chat") is None)
    st, _ = req("POST", "/api/chats/rename", '{"id":"nope-nope","title":"x"}', Transfer_Encoding="chunked")
    check("Transfer-Encoding requests refused", st == 400, st)


def t_key():
    """The page never receives the API key."""
    key = server.api_key()
    for p in ("/api/cline", "/api/code", "/api/web"):
        st, d = req("GET", p)
        check("GET %s answers without the real key" % p, st == 200 and (not key or key not in json.dumps(d)), st)
    st, d = req("GET", "/")
    check("the page has the server-side copy button", st == 200 and b"/api/key/copy" in d, st)
    st, d = req("POST", "/api/key/copy", "{}", Origin="https://evil.example")
    check("key copy refused from another origin", st == 403, st)
    old = clipboard()
    try:
        st, d = req("POST", "/api/key/copy", "{}")
        check("key copy puts the key on the clipboard", (st == 200 and clipboard() == key) if key else st == 500, (st, d))
    finally:
        subprocess.run(["clip"], input=b"\xff\xfe" + old.encode("utf-16-le"))


def t_guard():
    """Cross-site requests and other people's Tailscale logins."""
    for site, want in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), (None, 200)):
        st, _ = req("GET", "/api/cline", **({"Sec_Fetch_Site": site} if site else {}))
        check("API call with Sec-Fetch-Site %s -> %d" % (site, want), st == want, st)
    st, _ = req("GET", "/", Sec_Fetch_Site="cross-site", Sec_Fetch_Mode="navigate")
    check("a link from another site may open the page", st == 200, st)
    st, _ = req("POST", "/api/key/copy", "{}", Sec_Fetch_Site="cross-site", Content_Type="text/plain")
    check("cross-site simple POST refused", st == 403, st)
    as_phone(True)
    try:
        st, d = req("GET", "/api/web", **dict(PHONE, **{"Tailscale-User-Login": "Owner@Example.com"}))
        check("phone with the owner's login allowed, without a key", st == 200 and d.get("remote") is True and not d.get("api_key"), st)
        st, _ = req("GET", "/api/web", **dict(PHONE, **{"Tailscale-User-Login": "guest@other.com"}))
        check("phone with someone else's login refused", st == 403, st)
        st, _ = req("GET", "/api/web", Host=PHONE["Host"])
        check("tailnet host without a login refused", st == 403, st)
    finally:
        as_phone(False)


def t_phone():
    """The phone gets only the listed routes."""
    as_phone(True)
    try:
        for p in ("/", "/api/ping", "/api/stats", "/api/models", "/api/chats", "/api/web", "/api/tokens/log", "/api/memory"):
            st, _ = req("GET", p, **PHONE)
            check("phone GET %s allowed" % p, st != 403, st)
        for p in ("/api/code", "/api/cline", "/api/cloud", "/api/update", "/api/llama-update", "/api/clinecc", "/api/some-future-route"):
            st, _ = req("GET", p, **PHONE)
            check("phone GET %s refused" % p, st == 403, st)
        for p in ("/api/key/copy", "/api/web/serve", "/api/cloud/key", "/api/code/open", "/api/memory/location",
                  "/api/models/download", "/api/update/apply"):
            st, _ = req("POST", p, "{}", **PHONE)
            check("phone POST %s refused" % p, st == 403, st)
        st, _ = req("POST", "/api/chats/rename", '{"id":"nope-nope","title":"x"}', **PHONE)
        check("phone POST /api/chats/rename reaches the handler", st != 403, st)
    finally:
        as_phone(False)


def t_tools():
    """A model that asks for 5 searches in one turn gets cloud.MAX_CALLS of them."""
    seen, ran = [], []

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if len(seen) == 1:
                tc = [{"index": i, "id": "c%d" % i, "function": {"name": "web_search", "arguments": '{"query": "q%d"' % i}} for i in range(5)]
                ev = {"choices": [{"delta": {"tool_calls": tc}}]}
            else:
                ev = {"choices": [{"delta": {"content": "final answer"}}]}
            self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\ndata: [DONE]\n\n")

    fake = http.server.ThreadingHTTPServer(("127.0.0.1", PORT - 1), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    class H(server.Handler):
        def __init__(self):
            self.wfile, self.close_connection, self.remote, self.headers = io.BytesIO(), False, False, {}
            self.requestline, self.request_version, self.command = "POST /v1/chat/completions HTTP/1.1", "HTTP/1.1", "POST"
            self.client_address, self.path = ("127.0.0.1", 1), "/v1/chat/completions"

    def stub(settings, name, args):
        ran.append(args)
        return {"ok": True, "text": "r", "sources": [], "provider": "x", "model": "m"}

    real, cloud.run = cloud.run, stub
    h = H()
    try:
        h.tool_chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, ("127.0.0.1", PORT - 1), {}, None,
                    [{"type": "function", "function": {"name": "web_search"}}])
    finally:
        cloud.run = real
        fake.shutdown()
    out = h.wfile.getvalue().decode("utf-8", "replace")
    check("%d of 5 tool calls ran" % cloud.MAX_CALLS, len(ran) == cloud.MAX_CALLS, len(ran))
    check("cut-off arguments reached the tool repaired", ran[:1] == [{"query": "q0"}], ran[:1])
    check("the browser is told about the limit", "limit of %d per reply" % cloud.MAX_CALLS in out)
    check("the model still answers, in a round sent without tools", "final answer" in out and len(seen) == 2 and "tools" not in seen[1])


def t_tuner():
    """The optimiser's choice among measured trials (no model is loaded here)."""
    import tuner
    hw = {"gpu": "test", "vram_gb": 16.0}
    r = lambda tune, tg, pp, vram, ok=True: {"tune": tune, "ok": ok, "tg": tg, "pp": pp, "vram_gb": vram, "load_s": 9, "why": ""}
    base = r({}, 80, 2000, 14.9)
    check("tuner: nothing clearly faster -> the default stays", tuner.choose(base, [r({"fit_target": 256}, 81, 2000, 15.0)], hw)["tune"] == {})
    check("tuner: a 10% faster placement wins", tuner.choose(base, [r({"moe": 20}, 88, 2000, 15.2)], hw)["tune"] == {"moe": 20})
    check("tuner: faster but out of VRAM headroom loses", tuner.choose(base, [r({"moe": 18}, 95, 2000, 15.8)], hw)["tune"] == {})
    check("tuner: a trial that didn't load loses", tuner.choose(base, [r({"moe": 17}, 0, 0, 15.9, ok=False)], hw)["tune"] == {})
    two = [r({"moe": 20}, 88, 2000, 15.3), r({"moe": 21}, 87, 2000, 14.8)]
    check("tuner: of two near-equal placements the leaner one wins", tuner.choose(base, two, hw)["tune"] == {"moe": 21})
    edge = [r({"moe": 22}, 82, 2000, 15.1), r({"moe": 21}, 84, 2000, 15.5)]
    check("tuner: the leaner near-tie must itself beat the default, and the edge of VRAM is off limits", tuner.choose(base, edge, hw)["tune"] == {})
    check("tuner: a batch size that reads faster wins", tuner.choose(base, [r({"ub": 4096}, 80, 2600, 15.3)], hw)["tune"] == {"ub": 4096})
    check("tuner: a batch size that slows writing loses", tuner.choose(base, [r({"ub": 4096}, 43, 2600, 15.2)], hw)["tune"] == {})
    check("tuner: same speed with 0.7 GB less VRAM wins", tuner.choose(base, [r({"ub": 1024}, 81, 2010, 14.2)], hw)["tune"] == {"ub": 1024})
    lib = server.models.scan()
    if not lib or not server.models.hardware()["vram_gb"]:
        return print("skip tuner: command line (needs a model in the models folder and an NVIDIA GPU)")
    m = lib[0]
    cmd = server.models.command(m, 16384, None, {"moe": 21, "ub": 4096})
    check("tuner: measured settings reach the command line", "--n-cpu-moe" in cmd and cmd[cmd.index("--n-cpu-moe") + 1] == "21"
          and cmd[cmd.index("-ub") + 1] == "4096" and cmd[cmd.index("-b") + 1] == "4096", cmd)


GROUPS = {"modules": t_modules, "smuggle": t_smuggle, "key": t_key, "guard": t_guard, "phone": t_phone, "tools": t_tools, "tuner": t_tuner}

if __name__ == "__main__":
    want = sys.argv[1:] or list(GROUPS)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_dir, chats.DIR = chats.DIR, os.path.join(TMP, "chats-http")   # requests never touch the real chats
    try:
        for g in want:
            print("--", g)
            GROUPS[g]()
    finally:
        chats.DIR = real_dir
        srv.shutdown()
    print("FAILED (%d): %s" % (len(fails), fails) if fails else "ALL OK")
    sys.stdout.flush()
    os._exit(1 if fails else 0)
