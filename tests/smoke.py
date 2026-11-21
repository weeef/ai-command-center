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
    web_was, server.settings["web"] = server.settings.get("web"), {"enabled": False}   # the cloud provider searches here
    h = H()
    try:
        h.tool_chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, ("127.0.0.1", PORT - 1), {}, None,
                    [{"type": "function", "function": {"name": "web_search"}}])
    finally:
        cloud.run = real
        server.settings["web"] = web_was
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


def t_web():
    """The web connector's rules, with the internet stubbed out: nothing is requested from any site."""
    import webtool as w
    for u in ("http://127.0.0.1/", "http://localhost/x", "http://192.168.1.1/", "http://100.100.1.2/", "http://[::1]/",
              "http://169.254.169.254/latest", "https://127.0.0.1:8099/api/key", "file:///c:/x", "ftp://1.1.1.1/", "http://user:pw@1.1.1.1/"):
        try:
            w.check_url(u)
            check("web: %s is refused" % u, False)
        except w.Refused:
            check("web: %s is refused" % u, True)
    check("web: a public address passes", w.check_url("https://1.1.1.1/a b?q=1")[3:] == ("/a%20b?q=1", "1.1.1.1"))
    check("web: links are found and normalised", w.urls_in("see https://Example.com/a/ and (http://x.org/p?q=1). or https://e.com/w_(x)")
          == {"https://example.com/a", "http://x.org/p?q=1", "https://e.com/w_(x)"})
    title, text = w.page_text("<html><head><title>T &amp; t</title><style>x{}</style></head><body><nav>menu</nav><h1>Head</h1>"
                              "<p>one<br>two</p><script>bad()</script><ul><li>a</li></ul></body></html>")
    check("web: a page becomes plain text without menus and scripts", (title, text) == ("T & t", "# Head\n\none\ntwo\n\n- a"), (title, text))
    ddg = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.org%2Fp&amp;rut=1">A <b>page</b></a>'
           '<a class="result__snippet" href="x">about <b>a</b></a>')
    check("web: DuckDuckGo results are read", w.parse_ddg(ddg) == [{"title": "A page", "url": "https://a.org/p", "snippet": "about a"}], w.parse_ddg(ddg))
    brave = ('<div class="snippet" data-type="web"><a href="https://b.org/q" class="l1"><div class="title search-snippet-title x">B page</div></a>'
             '<div class="content t-primary">about b</div></div>')
    check("web: Brave results are read", w.parse_brave(brave) == [{"title": "B page", "url": "https://b.org/q", "snippet": "about b"}], w.parse_brave(brave))
    # Current Brave: each title links to /go/v1/<token>, whose page names the destination. A result whose own link
    # is missing must be dropped, not given the next result's link (it used to slide every link one result over).
    brave2 = ('<div data-type="web"><div class="title search-snippet-title">No link</div><div class="content x">none</div></div>'
              '<div data-type="web"><a href="/go/v1/tokA" class="l1"><div class="title search-snippet-title">Gh</div></a>'
              '<div class="content x">releases</div><a href="https://www.youtube.com/watch?v=1">video</a></div>'
              '<div data-type="web"><a href="/go/v1/tokB"><div class="title search-snippet-title">Gone</div></a></div>')
    pages = {"/go/v1/tokA": b'<p>sending you to <a class="destination svelte-1" href="https://github.com/o/r/releases">x</a></p>',
             "/go/v1/tokB": b'<p>expired</p>'}
    get, asked = w._get, []
    w._get = lambda u, *a: asked.append(u) or (200, "text/html", pages[u[len(w.BRAVE):]], u)
    try:
        r = w.parse_brave(brave2)
    finally:
        w._get = get
    check("web: Brave /go/v1 links are resolved, a link-less result doesn't take its neighbour's link",
          r == [{"title": "Gh", "url": "https://github.com/o/r/releases", "snippet": "releases"}] and len(asked) == 2, (r, asked))

    hop, w._via_curl = w._via_curl, lambda *a: (302, {"location": "http://127.0.0.1/api/key"}, b"")
    curl, w.CURL = w.CURL, "stub"
    try:
        r = w.fetch("https://1.1.1.1/")
        check("web: a public page that redirects to this PC is not followed", r[0] is False and "private network" in r[1], r)
    finally:
        w._via_curl, w.CURL = hop, curl

    on, got = {"web": {"enabled": True}}, []
    real = w.search, w.fetch
    w.search = lambda q: (True, [{"title": "A", "url": "https://a.org/p", "snippet": "s"}], "Stub")
    w.fetch = lambda u: (got.append(u) or True, "A", "body with http://c.org/next", u)
    w._cache.clear()
    try:
        seen = set()
        r = w.run(on, "web_fetch", {"url": "https://evil.example/?data=secret"}, seen)
        check("web: a made-up link is not opened", not r["ok"] and not got, r)
        check("web: off means off", not w.run({}, "web_search", {"query": "x"}, seen)["ok"])
        r = w.run(on, "web_search", {"query": "x"}, seen)
        check("web: a search returns results and their links become openable", r["ok"] and "https://a.org/p" in seen and r["provider"] == "Stub", r)
        r = w.run(on, "web_fetch", {"url": "https://a.org/p/"}, seen)
        check("web: a link from the results is opened", r["ok"] and got == ["https://a.org/p/"] and "http://c.org/next" in seen, r)
        check("web: reading pages can be switched off", not w.run({"web": {"enabled": True, "fetch": False}}, "web_fetch", {"url": "https://a.org/p"}, seen)["ok"])

        # through the chat: 8 searches asked for in one turn, 6 allowed; the user's own link may be opened
        asked = []

        class Fake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if len(asked) == 1:
                    tc = [{"index": 0, "id": "f0", "function": {"name": "web_fetch", "arguments": '{"url": "https://u.org/mine"}'}}]
                    tc += [{"index": i, "id": "c%d" % i, "function": {"name": "web_search", "arguments": '{"query": "q%d"}' % i}} for i in range(1, 8)]
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

        web_was, server.settings["web"] = server.settings.get("web"), {"enabled": True}
        h = H()
        try:
            tools = server.chat_tools()
            check("web: the chat offers search and page reading", [t["function"]["name"] for t in tools][:2] == ["web_search", "web_fetch"], tools)
            h.tool_chat({"messages": [{"role": "user", "content": "read https://u.org/mine please"}], "stream": True},
                        ("127.0.0.1", PORT - 1), {}, None, tools)
        finally:
            server.settings["web"] = web_was
            fake.shutdown()
        out = h.wfile.getvalue().decode("utf-8", "replace")
        check("web: a link the user wrote is opened", "https://u.org/mine" in got, got)
        check("web: %d web calls per reply, the rest are refused" % w.MAX_CALLS, out.count('"status": "done"') == w.MAX_CALLS and "limit of %d per reply" % w.MAX_CALLS in out,
              out.count('"status": "done"'))
        tool_msgs = [m["content"] for m in asked[1]["messages"] if m.get("role") == "tool"]
        check("web: results go back to the model, which then answers", len(tool_msgs) == 8 and "final answer" in out and "never" in asked[1]["messages"][0]["content"], len(tool_msgs))
    finally:
        w.search, w.fetch = real
        w._cache.clear()


def t_laptop():
    """The laptop picker: switching unloads what's up before loading the pick; unknown ids are refused."""
    st = {"unsloth/A-GGUF:Q4_K_M": "loaded", "unsloth/B-GGUF:Q6_K": "unloaded"}
    calls = []

    def fake(method, path, obj=None, timeout=2.0):
        if method == "GET" and path == "/models":
            return 200, {"data": [{"id": k, "status": {"value": v, "args": ["--ctx-size", "32768"]},
                                   "architecture": {"input_modalities": ["text"]}} for k, v in st.items()]}
        calls.append((path, obj["model"]))
        st[obj["model"]] = "loaded" if path == "/models/load" else "unloaded"
        return 200, {"success": True}
    real, save, pick = server.pair_req, server.save_settings, server.settings.get("laptop_pick")
    server.pair_req, server.save_settings = fake, lambda: None
    try:
        ok, msg = server.laptop_action("on", "unsloth/B-GGUF:Q6_K")
        check("laptop: picking another model unloads the running one, then loads the pick",
              ok and calls == [("/models/unload", "unsloth/A-GGUF:Q4_K_M"), ("/models/load", "unsloth/B-GGUF:Q6_K")], (ok, msg, calls))
        check("laptop: the pick is remembered", server.settings.get("laptop_pick") == "unsloth/B-GGUF:Q6_K")
        calls.clear()
        ok, msg = server.laptop_action("on", "unsloth/../evil")
        check("laptop: a model the laptop doesn't list is refused", not ok and not calls, (ok, msg))
        ok, msg = server.laptop_action("on")
        check("laptop: on with the pick already running does nothing", ok and not calls and "already" in msg, (ok, msg))
        ok, msg = server.laptop_action("off")
        check("laptop: off unloads it", ok and calls == [("/models/unload", "unsloth/B-GGUF:Q6_K")], (ok, calls))
        v = server.laptop_models_state()
        check("laptop: the picker lists both with names and context", [m["name"] for m in v["models"]] == ["A", "B"]
              and v["models"][1]["quant"] == "Q6_K" and v["models"][0]["ctx"] == 32768 and v["picked"] == "unsloth/B-GGUF:Q6_K", v)
    finally:
        server.pair_req, server.save_settings = real, save
        if pick is None:
            server.settings.pop("laptop_pick", None)
        else:
            server.settings["laptop_pick"] = pick


GROUPS = {"web": t_web, "modules": t_modules, "smuggle": t_smuggle, "key": t_key, "guard": t_guard, "phone": t_phone, "tools": t_tools, "tuner": t_tuner, "laptop": t_laptop}

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
