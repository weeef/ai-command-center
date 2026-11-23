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
    # a model whose compute buffers only fit with a smaller batch: placements are measured on top of that batch
    small = r({"ub": 256}, 60, 900, 13.6)
    check("tuner: with a smaller batch as the base, a faster placement on it wins",
          tuner.choose(small, [r({"fit_target": 1024, "ub": 256}, 70, 900, 13.9)], hw)["tune"] == {"fit_target": 1024, "ub": 256})
    check("tuner: with a smaller batch as the base, nothing better keeps that batch", tuner.choose(small, [r({"fit_target": 512, "ub": 256}, 60, 900, 14.0)], hw)["tune"] == {"ub": 256})
    oom = "E ggml_gallocr_reserve_n_impl: failed to allocate CUDA0 buffer\nE llama_init_from_model: failed to initialize the context: failed to allocate compute pp buffers"
    check("models: a compute-buffer out-of-memory is recognised", server.models.compute_oom(oom) and not server.models.compute_oom("E unknown model architecture"))
    check("models: the batch steps down 2048 -> 1024 -> ... -> 128, then stops",
          [server.models.smaller_ub(u) for u in (2048, 1024, 512, 256, 128)] == [1024, 512, 256, 128, None])
    # one retry, not one per halving: Inkling-2-Mini at 128K asked for 57 GB at batch 2048 on a 16 GB card, 256 fits
    fake = {"_meta": {"x.attention.head_count": 24, "x.attention.head_count_kv": 8, "x.attention.key_length": 128,
                      "x.attention.value_length": 128}, "arch": "x", "layers": 24}
    big = "E ggml_backend_cuda_buffer_type_alloc_buffer: allocating 57257.49 MiB on device 0: cudaMalloc failed: out of memory"
    check("models: the batch that fits is found from the logged size in one step",
          server.models.fit_ub(big, 2048, fake, 131072, {"vram_gb": 15.9}) == 256, server.models.fit_ub(big, 2048, fake, 131072, {"vram_gb": 15.9}))
    check("models: without a logged size it halves", server.models.fit_ub("failed to allocate compute pp buffers", 2048, fake, 131072, {"vram_gb": 15.9}) == 1024)
    check("models: any out-of-memory is recognised, a missing architecture is not",
          server.models.load_oom(big) and not server.models.load_oom("unknown model architecture: 'x'"))
    lib = server.models.scan()
    if not lib or not server.models.hardware()["vram_gb"]:
        return print("skip tuner: command line (needs a model in the models folder and an NVIDIA GPU)")
    m = lib[0]
    cmd = server.models.command(m, 16384, None, {"moe": 21, "ub": 4096})
    check("tuner: measured settings reach the command line", "--n-cpu-moe" in cmd and cmd[cmd.index("--n-cpu-moe") + 1] == "21"
          and cmd[cmd.index("-ub") + 1] == "4096" and cmd[cmd.index("-b") + 1] == "4096", cmd)


def t_learn():
    """Learning while in use: loads never wait, one new setting per load, the winner is kept (no model loaded)."""
    import tuner
    tmp = tempfile.mkdtemp()
    old = (tuner.PROFILES, tuner.placements, tuner._batches_for)
    tuner.PROFILES = os.path.join(tmp, "tuning.json")
    f = os.path.join(tmp, "x.gguf")
    with open(f, "w") as fh:
        fh.write("x")
    m, hw, C = {"id": "x.gguf", "_path": f, "name": "x", "mtp": False, "mmproj": None}, {"gpu": "test", "vram_gb": 16.0}, 32768
    tuner.placements = lambda m, ctx, hw: [{"fit_target": 1024}, {"moe": 20}]
    tuner._batches_for = lambda m, ctx, mode, hw, best, base: [dict(best, ub=4096)]
    use = lambda tg, pp, v=14.0: {"n_out": 2000, "s_out": 2000 / tg, "n_in": 3000, "s_in": 3000 / pp, "vram": v}
    lt = lambda **kw: tuner.launch_tune(m, C, None, {}, hw=hw, **kw)
    try:
        t, on = lt()
        check("learn: the first load starts at once with the defaults", t == {} and on, (t, on))
        check("learn: not judged before enough real use", not tuner.observe(m, C, None, t, dict(use(80, 2000), n_out=100), hw))
        check("learn: judged after enough real use", tuner.observe(m, C, None, t, use(80, 2000), hw))
        t, on = lt()
        check("learn: the next load tries one candidate", t == {"fit_target": 1024} and on, t)
        check("learn: a setting not judged yet is tried again on the next load", lt()[0] == {"fit_target": 1024})
        tuner.observe(m, C, None, t, use(81, 2000), hw)   # not clearly faster: the default stays
        t, _ = lt()
        check("learn: a clearly slower candidate is judged early (after a few hundred tokens)",
              t == {"moe": 20} and tuner.observe(m, C, None, t, dict(use(12, 2000), n_out=250, s_out=250 / 12), hw), t)
        tuner.forget(m, C, None, hw); t, _ = lt(); tuner.observe(m, C, None, t, use(80, 2000), hw); t, _ = lt()
        tuner.observe(m, C, None, t, use(81, 2000), hw)
        t, _ = lt()
        check("learn: then the next candidate", t == {"moe": 20}, t)
        check("learn: a candidate that didn't load is recorded", tuner.failed(m, C, None, t, "out of memory", hw))
        check("learn: after a failed candidate the best known starts, nothing new", lt(settle=True) == ({}, False))
        check("learn: the base itself is never marked failed", not tuner.failed(m, C, None, {}, "x", hw))
        t, _ = lt()
        check("learn: batch sizes come last, on the winning placement", t == {"ub": 4096}, t)
        tuner.observe(m, C, None, t, use(80, 2600, 14.3), hw)
        t, on = lt()
        check("learn: when everything is judged the winner is kept and nothing more is tried", t == {"ub": 4096} and not on, (t, on))
        check("learn: the model list shows the result", "optimised for this PC" in (tuner.summary(m, C, None, hw) or ""), tuner.summary(m, C, None, hw))
        tuner.forget(m, C, None, hw)
        t, on = tuner.launch_tune(m, C, None, {"ub": 256}, hw=hw)
        check("learn: a fallback batch is the base, and placements are tried on top of it",
              t == {"ub": 256} and on and lt()[0] == {"ub": 256} and tuner.observe(m, C, None, t, use(60, 900), hw) and lt()[0] == {"fit_target": 1024, "ub": 256})
        met = "\n".join(["# HELP x", "llamacpp:prompt_tokens_total 3000", "llamacpp:prompt_seconds_total 1.5",
                         "llamacpp:tokens_predicted_total 200", "llamacpp:tokens_predicted_seconds_total 2.5", ""])
        check("learn: llama-server's counters are read", tuner.parse_counters(met) == {"llamacpp:prompt_tokens_total": 3000.0,
              "llamacpp:prompt_seconds_total": 1.5, "llamacpp:tokens_predicted_total": 200.0, "llamacpp:tokens_predicted_seconds_total": 2.5})
        check("learn: missing counters are ignored", tuner.parse_counters("llamacpp:prompt_tokens_total 1") is None)
    finally:
        tuner.PROFILES, tuner.placements, tuner._batches_for = old


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


def t_worktree():
    """The worktree helper, in a throw-away repo whose folder name has a space and an & in it."""
    git = workspaces.GIT
    base = os.path.join(TMP, "wt test")
    repo = os.path.join(base, "my app&co")
    os.makedirs(repo)
    g = lambda *a, cwd=repo: subprocess.run([git, "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a),
                                            capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    ok, msg, _ = workspaces.agent_worktree(repo, "pc", "first")
    check("worktree: a repo with no commits is refused", not ok and "no commits" in msg, msg)
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("a\n")
    g("add", "a.txt"); g("commit", "-q", "-m", "one")
    head = g("rev-parse", "HEAD").stdout.strip()

    ok, wt, created = workspaces.laptop_worktree(repo)
    check("worktree: the laptop worktree is still <repo>-laptop on cline-laptop",
          ok and created and wt == repo + "-laptop" and g("branch", "--show-current", cwd=wt).stdout.strip() == "cline-laptop", (ok, wt))
    check("worktree: the laptop worktree is reused", workspaces.laptop_worktree(repo) == (True, wt, False))

    ok, pc, created = workspaces.agent_worktree(repo, "pc", "export-api", head)
    check("worktree: an agent gets <repo>-agents\\<agent> on agent/<agent>/<task>",
          ok and created and pc == os.path.join(repo + "-agents", "pc")
          and g("branch", "--show-current", cwd=pc).stdout.strip() == "agent/pc/export-api", (ok, pc))
    ok, lap, _ = workspaces.agent_worktree(repo, "laptop", "export-ui", head)
    check("worktree: two agents never share a folder", ok and lap != pc and os.path.isdir(lap), lap)
    check("worktree: the same task reuses the folder", workspaces.agent_worktree(repo, "pc", "export-api") == (True, pc, False))

    with open(os.path.join(pc, "b.txt"), "w") as f:
        f.write("work in progress\n")
    ok, msg, _ = workspaces.agent_worktree(repo, "pc", "next-task")
    check("worktree: a folder with uncommitted work isn't switched to a new task", not ok and "uncommitted" in msg, msg)
    ok, msg = workspaces.remove_worktree(repo, pc)
    check("worktree: a folder with uncommitted work isn't removed", not ok and os.path.exists(os.path.join(pc, "b.txt")), msg)
    os.remove(os.path.join(pc, "b.txt"))
    ok, again, created = workspaces.agent_worktree(repo, "pc", "next-task")
    check("worktree: a clean folder moves on to the next task's branch",
          ok and again == pc and created and g("branch", "--show-current", cwd=pc).stdout.strip() == "agent/pc/next-task", (ok, again))

    os.makedirs(os.path.join(repo + "-agents", "stranger"))
    ok, msg, _ = workspaces.agent_worktree(repo, "stranger", "x")
    check("worktree: a folder that isn't a worktree is left alone", not ok and "isn't a worktree" in msg, msg)
    for bad in (("..", "x"), ("pc", "../x"), ("PC", "x"), ("pc", "a b"), ("pc", "")):
        ok, msg, _ = workspaces.agent_worktree(repo, *bad)
        check("worktree: name %r / task %r refused" % bad, not ok and not os.path.exists(os.path.join(repo + "-agents", bad[0], bad[1] or "_")), msg)
    check("worktree: removing the repo itself is refused", not workspaces.remove_worktree(repo, repo)[0])
    ok, msg = workspaces.remove_worktree(repo, lap)
    check("worktree: a clean agent folder is removed and its branch kept",
          ok and not os.path.exists(lap) and g("rev-parse", "--verify", "--quiet", "refs/heads/agent/laptop/export-ui").returncode == 0, msg)


def t_team():
    """The agent registry and who may change it."""
    import team
    ok, _, a = team.check_agents([{"name": "pc", "backend": "pc", "client": "vscode", "cmd": "calc.exe"},
                                  {"name": "cc1", "backend": "cloud:anthropic", "client": "claude-code"}])
    check("team: a valid registry is kept, unknown keys dropped", ok and a[0] == {"name": "pc", "backend": "pc", "client": "vscode"}, a)
    for bad, why in (([], "empty"), ([{"name": "pc", "backend": "pc", "client": "vscode"}] * 2, "duplicate"),
                     ([{"name": "x y", "backend": "pc", "client": "claude-code"}], "bad name"),
                     ([{"name": "a", "backend": "http://evil", "client": "claude-code"}], "unknown backend"),
                     ([{"name": "a", "backend": "pc", "client": "cmd /c calc"}], "unknown client"),
                     ([{"name": "third", "backend": "pc", "client": "vscode"}], "a third VS Code"),
                     ([{"name": "pc", "backend": "cloud:anthropic", "client": "vscode"}], "cloud in VS Code"),
                     ([{"name": "a%d" % i, "backend": "pc", "client": "claude-code"} for i in range(9)], "too many")):
        check("team: registry refused (%s)" % why, not team.check_agents(bad)[0])
    check("team: no registry means the two VS Code agents", [x["name"] for x in team.agents({})] == ["pc", "laptop"])
    check("team: a broken registry falls back to the default", [x["name"] for x in team.agents({"agents": "junk"})] == ["pc", "laptop"])

    st, d = req("GET", "/api/team")
    check("team: GET /api/team lists agents and installed clients", st == 200 and d["agents"] and "claude-code" in d["clients"], st)
    saved, save = server.settings.get("agents"), server.save_settings
    server.save_settings = lambda: None
    try:
        body = json.dumps({"agents": [{"name": "pc", "backend": "pc", "client": "vscode"}]})
        st, _ = req("POST", "/api/team/agents", body, Origin="https://evil.example")
        check("team: saving the registry from another origin refused", st == 403, st)
        st, d = req("POST", "/api/team/agents", body)
        check("team: saving the registry from the PC works", st == 200 and d["ok"] and server.settings["agents"] == d["agents"], d)
        st, d = req("POST", "/api/team/agents", json.dumps({"agents": [{"name": "pc", "backend": "x", "client": "vscode"}]}))
        check("team: an invalid registry is refused and nothing changes", st == 400 and len(server.settings["agents"]) == 1, d)
        as_phone(True)
        try:
            st, _ = req("GET", "/api/team", **PHONE)
            check("phone: may watch the team", st == 200, st)
            for p in ("/api/team/agents", "/api/team/plan", "/api/team/start", "/api/team/stop", "/api/team/merge",
                      "/api/team/sendback", "/api/team/discard", "/api/team/cleanup", "/api/team/next-thing"):
                st, _ = req("POST", p, body, **PHONE)
                check("phone POST %s refused" % p, st == 403, st)
        finally:
            as_phone(False)
    finally:
        server.save_settings = save
        if saved is None:
            server.settings.pop("agents", None)
        else:
            server.settings["agents"] = saved


def temp_repo(name):
    """A throw-away git repo with one commit, in the test folder."""
    repo = os.path.join(TMP, name)
    os.makedirs(os.path.join(repo, "server"))
    os.makedirs(os.path.join(repo, "ui"))
    for f in ("server/api.py", "ui/app.js", "README.md"):
        with open(os.path.join(repo, f), "w") as fh:
            fh.write("x\n")
    g = lambda *a: subprocess.run([workspaces.GIT, "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a),
                                  capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("add", "-A"); g("commit", "-q", "-m", "one")
    return repo


def t_board():
    """The task board: the split's rules, where it is kept, the planner and the brief each agent gets."""
    import team
    reg = team.DEFAULT_AGENTS
    split = [{"agent": "pc", "title": "Export API", "goal": "GET /api/export", "owns": ["server/", "tests/test_export.py"]},
             {"agent": "laptop", "title": "Export button", "goal": "a button", "owns": "ui/app.js\nui/export.css"}]
    ok, msg, clean, _ = team.check_tasks(split, reg)
    check("board: a clean split passes (owns may be a list or lines)", ok and clean[1]["owns"] == ["ui/app.js", "ui/export.css"]
          and clean[0]["slug"] == "export-api", (msg, clean))
    for owns, why in ((["server/api.py"], "a file inside the other's folder"), (["Server"], "the same folder, other case"),
                      (["tests/test_export.py"], "the same file")):
        bad = [split[0], dict(split[1], owns=owns)]
        ok, msg, _, clash = team.check_tasks(bad, reg)
        check("board: overlap refused (%s)" % why, not ok and "both own" in msg and clash, msg)
    for owns in (["../x"], ["C:/Windows/x"], ["/etc/x"], ["a/../../b"], [], ["a\x00b"]):
        ok, msg, _, _ = team.check_tasks([dict(split[0], owns=owns)], reg)
        check("board: owned path %r refused" % (owns,), not ok, msg)
    check("board: unknown agent refused", not team.check_tasks([dict(split[0], agent="ghost")], reg)[0])
    check("board: two tasks for one agent refused", not team.check_tasks([split[0], dict(split[0], title="b", owns=["docs/"])], reg)[0])

    repo = temp_repo("board repo")
    ok, msg, b = team.edit_board(repo, reg, "an export feature", split)
    p = team.board_path(repo)
    check("board: kept in the repo's .git folder, not in the working tree",
          ok and p == os.path.join(repo, ".git", "cc-team", "board.json") and os.path.exists(p), (msg, p))
    st = subprocess.run([workspaces.GIT, "-C", repo, "status", "--porcelain"], capture_output=True, text=True).stdout
    check("board: git sees no new files", st.strip() == "", st)
    ok, wt, _ = workspaces.agent_worktree(repo, "pc", "export-api")
    check("board: an agent's worktree finds the same board", ok and team.board_path(wt) == p, team.board_path(wt))
    b["state"] = "running"; team.save_board(repo, b)
    check("board: the split can't change while the team works", not team.edit_board(repo, reg, "x", split)[0])
    b["state"] = "draft"; team.save_board(repo, b)

    br = team.brief(b, b["tasks"][0])
    check("board: the brief has the task, the owned files and the other agent's status",
          "Export API" in br and "`server/`" in br and "**laptop** (draft): Export button" in br and "`ui/app.js`" in br, br)
    team.add_note(repo, "t2", "laptop", "the button calls GET /api/export?fmt=csv")
    br = team.brief(team.load_board(repo), b["tasks"][0])
    check("board: handoff notes reach the other agent's brief", "- laptop: the button calls GET /api/export?fmt=csv" in br, br)
    mine = br + "- pc: export returns {rows: [...]}\n- laptop: not mine to add\n"
    check("board: only the agent's own new handoff lines are picked up",
          team.new_notes(mine, "pc", []) == ["export returns {rows: [...]}"] and team.new_notes(mine, "laptop", ["the button calls GET /api/export?fmt=csv"]) == ["not mine to add"],
          (team.new_notes(mine, "pc", []), team.new_notes(mine, "laptop", [])))

    # the planner, with the models stubbed: the strongest one that's on, cloud only when ticked
    asked = []
    real = server.llm_json, server.model_info, server.laptop_info, server.save_settings
    state = {"pc": "ON", "laptop": "OFF"}
    server.model_info = lambda *a: {"state": state["pc"], "name": "PC model"}
    server.laptop_info = lambda *a, **k: {"state": state["laptop"], "name": "laptop model", "id": "x"}
    server.llm_json = lambda msgs, schema, n, backend=None, **k: asked.append((backend, msgs[1]["content"])) or {"tasks": split}
    server.save_settings = lambda: None
    repos_was = server.settings.get("team_repos")
    try:
        ok, msg, b2 = server.team_plan(repo, "an export feature", use_cloud=True)   # cloud isn't set up here
        check("planner: the PC model plans when cloud isn't set up", ok and asked and asked[0][0] == "pc" and b2["planner"]["backend"] == "pc", (msg, asked[:1]))
        check("planner: it sees the goal and the repo's file list", "an export feature" in asked[0][1] and "server/api.py" in asked[0][1])
        state["pc"] = "OFF"; asked.clear()
        ok, msg, _ = server.team_plan(repo, "x")
        check("planner: nothing on -> a clear refusal, no request", not ok and not asked and "no model is on" in msg, msg)
        state["laptop"] = "ON"
        ok, msg, b3 = server.team_plan(repo, "x")
        check("planner: the laptop plans when it's the only one on", ok and asked[0][0] == "laptop", msg)
        server.llm_json = lambda *a, **k: {"tasks": [split[0], dict(split[1], owns=["server/x.py"])]}
        ok, msg, b4 = server.team_plan(repo, "x")
        check("planner: a split with an overlap comes back to be fixed, not saved", not ok and b4 and "both own" in b4.get("problem", ""), msg)
        check("planner: the saved board is still the last good one", team.load_board(repo)["tasks"][1]["owns"] == ["ui/app.js", "ui/export.css"])

        st, d = req("POST", "/api/team/board", json.dumps({"repo": os.path.join(repo, "server"), "goal": "g2", "tasks": split}))
        check("board route: a folder inside the repo means the repo", st == 200 and d["repo"] == repo and d["board"]["goal"] == "g2", d)
        st, d = req("POST", "/api/team/board", json.dumps({"repo": TMP, "goal": "g", "tasks": split}))
        check("board route: a folder that isn't a repo is refused", st == 400, d)
        st, d = req("GET", "/api/team?repo=" + urllib_quote(repo))
        check("board route: GET returns the board", st == 200 and d["board"]["goal"] == "g2", st)
        as_phone(True)
        try:
            st, d = req("GET", "/api/team?repo=" + urllib_quote(repo), **PHONE)
            check("phone: sees the board", st == 200 and d["board"]["goal"] == "g2", st)
            st, _ = req("POST", "/api/team/plan", json.dumps({"repo": repo, "goal": "g"}), **PHONE)
            check("phone: can't plan", st == 403, st)
            st, _ = req("POST", "/api/team/board", json.dumps({"repo": repo, "goal": "g", "tasks": split}), **PHONE)
            check("phone: can't edit the split", st == 403, st)
        finally:
            as_phone(False)
    finally:
        server.llm_json, server.model_info, server.laptop_info, server.save_settings = real
        if repos_was is None:
            server.settings.pop("team_repos", None)
        else:
            server.settings["team_repos"] = repos_was


FAKE_CLAUDE = r'''
import json, os, sys, urllib.request, urllib.error
base, tok = os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_AUTH_TOKEN"]
def ask(token):
    r = urllib.request.Request(base + "/v1/messages", json.dumps({"model": "local", "max_tokens": 5, "messages": []}).encode(),
                               {"Content-Type": "application/json", "x-api-key": token})
    try:
        return urllib.request.urlopen(r, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code
codes = [ask("wrong-token"), ask(tok)]
with open("server/proxy-codes.txt", "w") as f:
    f.write(json.dumps(codes))
brief = open(".cc-agent.md", encoding="utf-8").read()
with open(".cc-agent.md", "a", encoding="utf-8") as f:
    f.write("- cc1: the export route is GET /api/export\n")
with open("server/export.py", "w") as f:
    f.write("def export(): return []\n")
ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "server/export.py"}}],
                                       "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 40}}}
print(json.dumps(ev), flush=True)
print(json.dumps({"type": "result", "is_error": False, "result": "done: " + str("Export API" in brief)}), flush=True)
'''


def t_teamrun():
    """Starting a team, live steps from Cline and Claude Code, the Claude Code proxy's token, stop."""
    import team
    repo = temp_repo("run repo")
    fake_dir = os.path.join(TMP, "fake claude")
    os.makedirs(fake_dir, exist_ok=True)
    with open(os.path.join(fake_dir, "fake_claude.py"), "w") as f:
        f.write(FAKE_CLAUDE)
    got = []

    class Model(http.server.BaseHTTPRequestHandler):   # stands in for llama-server on :8090
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for d in ({"content": "I'll add the button.\n<write_to_file>\n<path>ui/app.js</path>\n<content>x"}, {"content": "</content>\n</write_to_file>"}):
                self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() + b"\n\n")
            self.wfile.write(b'data: {"choices":[{"delta":{}}],"timings":{"prompt_n":50,"cache_n":950,"predicted_n":30}}\n\ndata: [DONE]\n\n')

    fake = http.server.ThreadingHTTPServer(("127.0.0.1", PORT - 1), Model)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    saved = {k: server.settings.get(k) for k in ("agents", "team_active", "team_repos", "recent_projects")}
    real = (server.LLAMA, server.model_info, server.laptop_info, server.save_settings, server.copy_to_clipboard,
            workspaces.open_code, team.claude_bin, team.claude_command, server.api_key)
    opened, clip = [], []
    server.LLAMA = ("127.0.0.1", PORT - 1)
    server.model_info = lambda *a: {"state": "ON", "name": "PC model"}
    server.laptop_info = lambda *a, **k: {"state": "OFF", "id": None}
    server.save_settings = lambda: None
    server.copy_to_clipboard = lambda t: clip.append(t) or True
    server.api_key = lambda: "test-key"
    workspaces.open_code = lambda which, path, *a, **k: opened.append((which, path)) or (True, "opening")
    team.claude_bin = lambda: sys.executable
    team.claude_command = lambda exe: [exe, "-I", os.path.join(fake_dir, "fake_claude.py")]
    server.settings["agents"] = [{"name": "pc", "backend": "pc", "client": "vscode"},
                                 {"name": "cc1", "backend": "pc", "client": "claude-code"},
                                 {"name": "cc2", "backend": "cloud:anthropic", "client": "claude-code"}]
    split = [{"agent": "pc", "title": "Export button", "goal": "a button", "owns": ["ui/"]},
             {"agent": "cc1", "title": "Export API", "goal": "GET /api/export", "owns": ["server/"]}]
    try:
        st, d = req("POST", "/api/team/board", json.dumps({"repo": repo, "goal": "export", "tasks": split + [
            {"agent": "cc2", "title": "Docs", "goal": "", "owns": ["README.md"]}]}))
        st, d = req("POST", "/api/team/start", json.dumps({"repo": repo}))
        check("start: a cloud agent needs a confirmation first, and nothing starts", st == 409 and d.get("needs_confirm") == ["cc2"]
              and not opened and not os.path.isdir(repo + "-agents"), d)
        st, d = req("POST", "/api/team/board", json.dumps({"repo": repo, "goal": "export", "tasks": split}))
        st, d = req("POST", "/api/team/start", json.dumps({"repo": repo}), Origin="https://evil.example")
        check("start: refused from another origin", st == 403, st)
        st, d = req("POST", "/api/team/start", json.dumps({"repo": repo}))
        b = d.get("board") or {}
        pc = os.path.join(repo + "-agents", "pc")
        check("start: every agent gets its own worktree on its task branch", st == 200 and [t["worktree"] for t in b["tasks"]]
              == [pc, os.path.join(repo + "-agents", "cc1")] and b["state"] == "running", d)
        check("start: VS Code opens on the pc agent's worktree, the starter prompt goes on the clipboard",
              opened == [("pc", pc)] and clip == [team.STARTER], (opened, clip))
        br = open(os.path.join(pc, ".cc-agent.md"), encoding="utf-8").read()
        check("start: the brief and the Cline rule are in the worktree", "Export button" in br and "**cc1** (running)" in br
              and os.path.exists(os.path.join(pc, ".clinerules", "cc-team.md")), br[:200])
        stt = subprocess.run([workspaces.GIT, "-C", pc, "status", "--porcelain"], capture_output=True, text=True).stdout
        check("start: git doesn't see the brief files", stt.strip() == "", stt)
        st, d = req("POST", "/api/team/start", json.dumps({"repo": repo}))
        check("start: a running team can't be started twice", st == 409, d)

        # Cline in the pc agent's VS Code takes one step through the command center
        st, raw = req("POST", "/cline/pc/v1/chat/completions", json.dumps({"messages": [{"role": "user", "content": "<task>go</task>"}], "stream": True}))
        for _ in range(50):   # the step is recorded just after the reply's last byte
            tb = team.load_board(repo)["tasks"][0]
            if tb.get("steps"):
                break
            time.sleep(0.05)
        step = (tb.get("steps") or [{}])[-1]
        check("cline: the step reaches the model with the key added", st == 200 and got and got[-1][1] == "Bearer test-key", st)
        check("cline: the step shows in the agent's panel (tool, file, tokens, cache)",
              step.get("tool") == "write_to_file" and step.get("target") == "ui/app.js" and step.get("prompt") == 1000
              and step.get("cached") == 950 and tb["stats"]["steps"] == 1, step)

        # the headless agent: its process ran, used the proxy, wrote a file and a handoff note
        for _ in range(100):
            t2 = team.load_board(repo)["tasks"][1]
            if t2["status"] != "running":
                break
            time.sleep(0.1)
        cc = os.path.join(repo + "-agents", "cc1")
        pf = os.path.join(cc, "server", "proxy-codes.txt")
        codes = json.loads(open(pf).read()) if os.path.exists(pf) else None
        check("claude-code: finished, and its stream became steps", t2["status"] == "done" and t2["stats"].get("prompt") == 1000
              and t2["steps"][0]["target"] == "server/export.py" and "done: True" in (t2.get("message") or ""), t2)
        check("claude-code: the proxy refuses a wrong token and forwards the right one to /v1/messages",
              codes == [403, 200] and any(p == "/v1/messages" and a == "Bearer test-key" for p, a, _ in got), (codes, [g[:2] for g in got]))
        st, d = req("POST", "/agent/cc1/v1/messages", json.dumps({"messages": []}), x_api_key="anything")
        check("claude-code: after the run the route is closed", st == 403, st)
        st, d = req("GET", "/api/team?repo=" + urllib_quote(repo))
        v = {t["agent"]: t for t in d["board"]["tasks"]}
        check("view: changed files per agent", any(f["path"] == "server/export.py" for f in v["cc1"]["files"]) and not v["cc1"]["outside"], v["cc1"].get("files"))
        check("view: the handoff note went to the board", any(n["text"] == "the export route is GET /api/export" for n in v["cc1"]["notes"]), v["cc1"]["notes"])
        br = open(os.path.join(pc, ".cc-agent.md"), encoding="utf-8").read()
        check("view: and into the other agent's brief", "- cc1: the export route is GET /api/export" in br and "**cc1** (done)" in br, br[-400:])

        as_phone(True)
        try:
            st, _ = req("POST", "/api/team/stop", json.dumps({"repo": repo, "task": "t1"}), **PHONE)
            check("phone: can't stop an agent", st == 403, st)
            st, _ = req("POST", "/agent/cc1/v1/messages", "{}", **PHONE)
            check("phone: no way into the agent proxy", st == 403, st)
        finally:
            as_phone(False)
        st, d = req("POST", "/api/team/stop", json.dumps({"repo": repo, "task": "t1"}))
        check("stop: the VS Code agent is marked stopped", st == 200 and team.load_board(repo)["tasks"][0]["status"] == "stopped", d)
        n = len(got)
        st, d = req("POST", "/cline/pc/v1/chat/completions", json.dumps({"messages": [{"role": "user", "content": "<task>go</task>"}], "stream": True}))
        check("stop: its next Cline step is refused and never reaches the model", st == 409 and len(got) == n, (st, d))
        check("stop: everything finished -> the board is up for review", team.load_board(repo)["state"] == "review")
    finally:
        (server.LLAMA, server.model_info, server.laptop_info, server.save_settings, server.copy_to_clipboard,
         workspaces.open_code, team.claude_bin, team.claude_command, server.api_key) = real
        for k, v in saved.items():
            if v is None:
                server.settings.pop(k, None)
            else:
                server.settings[k] = v
        fake.shutdown()


def t_review():
    """Review and merge: commit on the agent's branch, tests, diff, merge (never with your changes in the way, a
    conflict leaves everything as it was), send back, discard, clean up (asks first, keeps unsaved work)."""
    import team
    repo = temp_repo("review repo")
    g = lambda *a, cwd=repo: subprocess.run([workspaces.GIT, "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a),
                                            capture_output=True, text=True)
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "smoke.py"), "w") as f:
        f.write("import os, sys\nprint('ran in', os.path.basename(os.getcwd()))\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n")
    g("add", "-A"); g("commit", "-q", "-m", "tests")
    split = [{"agent": "pc", "title": "UI part", "goal": "", "owns": ["ui/", "BROKEN"]},
             {"agent": "laptop", "title": "Server part", "goal": "", "owns": ["server/", "README.md"]}]
    ok, msg, b = team.edit_board(repo, team.DEFAULT_AGENTS, "a feature", split)
    for t in b["tasks"]:
        team.prepare(repo, b, t, "vscode")
        t["status"] = "done"
    b["state"] = "review"
    team.save_board(repo, b)
    pc, lap = b["tasks"][0]["worktree"], b["tasks"][1]["worktree"]
    with open(os.path.join(pc, "ui", "app.js"), "a") as f:
        f.write("button()\n")
    with open(os.path.join(pc, "ui", "new.js"), "w") as f:
        f.write("export const x = 1\n")
    ok, msg = team.commit_work(b["tasks"][0])
    shown = g("show", "--stat", "--format=%s", "agent/pc/ui-part").stdout
    check("review: the agent's work is committed on its own branch, without the brief files",
          ok and "ui/new.js" in shown and ".cc-agent.md" not in shown and ".clinerules" not in shown and workspaces.is_clean(pc), (msg, shown))
    check("review: the project's own test command is found", team.test_command(pc)[-1] == os.path.join("tests", "smoke.py"))

    def wait_test(tid):
        for _ in range(200):
            x = next(t for t in team.load_board(repo)["tasks"] if t["id"] == tid).get("test") or {}
            if x.get("state") not in (None, "running"):
                return x
            time.sleep(0.05)
        return {}
    team.run_tests(repo, "t1")
    x = wait_test("t1")
    check("review: tests run in the agent's worktree", x.get("state") == "passed" and "ran in pc" in x.get("tail", ""), x)
    open(os.path.join(pc, "BROKEN"), "w").close()
    team.run_tests(repo, "t1")
    check("review: a failing test run is shown as failed", wait_test("t1").get("state") == "failed")
    os.remove(os.path.join(pc, "BROKEN"))

    d = team.diff(repo, "t1")
    check("review: the diff has the change and the new file", d and "+button()" in d["diff"] and "ui/new.js" in d["diff"], (d or {}).get("stat"))
    st, d2 = req("GET", "/api/team/diff?repo=%s&task=t1" % urllib_quote(repo))
    check("review: GET /api/team/diff", st == 200 and d2["branch"] == "agent/pc/ui-part", st)

    # your own checkout has an unsaved change: merge refuses and changes nothing
    head = g("rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(repo, "README.md"), "a") as f:
        f.write("my unsaved line\n")
    ok, msg = team.merge(repo, "t1")
    check("merge: refused while your checkout has uncommitted changes", not ok and "uncommitted" in msg
          and g("rev-parse", "HEAD").stdout.strip() == head, msg)
    g("checkout", "--", "README.md")
    st, d = req("POST", "/api/team/merge", json.dumps({"repo": repo, "task": "t1"}))
    log = g("log", "--format=%P|%s", "-1").stdout.strip()
    check("merge: --no-ff into the base branch in your checkout", st == 200 and len(log.split("|")[0].split()) == 2
          and "agent/pc/ui-part" in log and os.path.exists(os.path.join(repo, "ui", "new.js")), (d, log))
    check("merge: the task is marked merged", team.load_board(repo)["tasks"][0]["status"] == "merged")

    # a conflict: the laptop agent and you both changed README.md
    with open(os.path.join(lap, "README.md"), "w") as f:
        f.write("agent version\n")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("owner version\n")
    g("commit", "-q", "-am", "owner edit")
    head = g("rev-parse", "HEAD").stdout.strip()
    ok, msg = team.merge(repo, "t2")
    check("merge: a conflict is aborted and reported, nothing changes", not ok and "conflict in README.md" in msg
          and g("rev-parse", "HEAD").stdout.strip() == head and workspaces.is_clean(repo), (msg, g("status", "--porcelain").stdout))

    saved_clip, server.copy_to_clipboard = server.copy_to_clipboard, lambda t: True
    try:
        st, d = req("POST", "/api/team/sendback", json.dumps({"repo": repo, "task": "t2", "note": "keep the owner's README text"}))
    finally:
        server.copy_to_clipboard = saved_clip
    br = open(os.path.join(lap, ".cc-agent.md"), encoding="utf-8").read()
    check("send back: the note is in the agent's brief and it works again", st == 200 and "## Sent back for changes" in br and "from the owner" in br
          and "keep the owner's README text" in br and team.load_board(repo)["tasks"][1]["status"] == "running", (d, br[:300]))
    team.set_status(repo, "t2", "done")
    with open(os.path.join(lap, "server", "wip.py"), "w") as f:
        f.write("half done\n")
    ok, msg = team.discard(repo, "t2")
    check("discard: marked discarded, nothing deleted", ok and os.path.isdir(lap) and team.load_board(repo)["tasks"][1]["status"] == "discarded", msg)

    st, d = req("POST", "/api/team/cleanup", json.dumps({"repo": repo}))
    check("clean up: asks first and lists what it would do", st == 409 and {p["agent"] for p in d["plan"]} == {"pc", "laptop"}
          and os.path.isdir(pc), d)
    as_phone(True)
    try:
        for op in ("merge", "cleanup", "sendback", "discard", "test", "finish"):
            st, _ = req("POST", "/api/team/" + op, json.dumps({"repo": repo, "task": "t1", "confirm": True}), **PHONE)
            check("phone POST /api/team/%s refused" % op, st == 403, st)
        st, _ = req("GET", "/api/team/diff?repo=%s&task=t1" % urllib_quote(repo), **PHONE)
        check("phone: may read a diff", st == 200, st)
    finally:
        as_phone(False)
    st, d = req("POST", "/api/team/cleanup", json.dumps({"repo": repo, "confirm": True}))
    check("clean up: the merged agent's folder and branch go", st == 200 and not os.path.exists(pc)
          and g("rev-parse", "--verify", "--quiet", "refs/heads/agent/pc/ui-part").returncode != 0, d)
    check("clean up: a folder with uncommitted work stays, and so does discarded work's branch",
          os.path.exists(os.path.join(lap, "server", "wip.py")) and g("rev-parse", "--verify", "--quiet", "refs/heads/agent/laptop/server-part").returncode == 0, d)


def t_slots():
    """Two server slots: off by default, exactly the measured flags when on, PC-only to change."""
    lib = server.models.scan()
    if lib:
        m = lib[0]
        one, two = server.models.command(m, 32768), server.models.command(m, 32768, slots=2)
        check("slots: one slot by default", one[one.index("-np") + 1] == "1" and one[one.index("-c") + 1] == "32768"
              and "--kv-unified-per-slot" not in one, one)
        check("slots: two slots = 2 x ctx, each capped at ctx", two[two.index("-np") + 1] == "2" and two[two.index("-c") + 1] == "65536"
              and two[two.index("--kv-unified-per-slot") + 1] == "32768", two)
        check("slots: anything else means one", server.models.command(m, 32768, slots=7)[one.index("-np") + 1] == "1")
    saved, save = server.settings.get("slots"), server.save_settings
    server.save_settings = lambda: None
    try:
        st, d = req("POST", "/api/models/slots", json.dumps({"slots": 3}))
        check("slots: only 1 or 2", st == 400, st)
        st, d = req("POST", "/api/models/slots", json.dumps({"slots": 2}))
        check("slots: saved for the next start", st == 200 and server.settings["slots"] == 2, d)
        as_phone(True)
        try:
            st, _ = req("POST", "/api/models/slots", json.dumps({"slots": 1}), **PHONE)
            check("phone: can't change the slots", st == 403 and server.settings["slots"] == 2, st)
        finally:
            as_phone(False)
    finally:
        server.save_settings = save
        if saved is None:
            server.settings.pop("slots", None)
        else:
            server.settings["slots"] = saved

def t_claude():
    """What the live run with a real Claude Code agent on Qwen3.8 taught (2026-11-22)."""
    import team
    req_ = {"system": [{"type": "text", "text": "S"}], "messages": [
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        {"role": "system", "content": [{"type": "text", "text": "reminder"}]},
        {"role": "assistant", "content": "ok"}, {"role": "user", "content": "next"}]}
    out = server.anthropic_to_local(req_)
    roles = [m["role"] for m in out["messages"]]
    check("claude: a system message mid-conversation joins the user turn (Qwen refuses it otherwise)",
          roles == ["user", "assistant", "user"] and "<system-reminder>\nreminder\n</system-reminder>" in out["messages"][0]["content"][1]["text"]
          and out["system"] == [{"type": "text", "text": "S"}], out["messages"])
    seen = set()
    u = {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 0}
    a = team._step_from_claude({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "x"}]}}, seen, r"C:\wt")
    b = team._step_from_claude({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": r"C:\WT\server\export.py"}}]}}, seen, r"C:\wt")
    c = team._step_from_claude({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "y"}]}}, seen, r"C:\wt")
    check("claude: one reply's tokens count once, its tool call still shows", a["prompt"] == 1000 and b["prompt"] == 0
          and b["tool"] == "Write" and c is None, (a, b, c))
    check("claude: paths in the worktree are shown relative to it", b["target"] == r"server\export.py", b["target"])
    repo = temp_repo("ctx repo")
    saved, real = server.settings.get("agents"), (server.model_info, server.laptop_info)
    server.settings["agents"] = [{"name": "cc1", "backend": "pc", "client": "claude-code"}]
    server.model_info = lambda *a: {"state": "ON", "name": "PC model", "ctx": 32768}
    server.laptop_info = lambda *a, **k: {"state": "OFF", "id": None}
    try:
        team.edit_board(repo, team.agents(server.settings), "g", [{"agent": "cc1", "title": "x", "owns": ["server/"]}])
        ok, msg, _, _ = server.team_start(repo)
        check("claude: refused up front on a 32K model (its own prompt needs ~30k)", not ok and "64K" in msg
              and not os.path.isdir(repo + "-agents"), msg)
    finally:
        server.model_info, server.laptop_info = real
        if saved is None:
            server.settings.pop("agents", None)
        else:
            server.settings["agents"] = saved

def t_conductor():
    """Claude conducting the team, with a fake Claude: the fixed command line, plan, check each part, send back with
    the exact note, give up after MAX_ROUNDS, the whole-feature check, and who may switch it on."""
    import team
    import conductor
    calls, answers = [], []

    class Done:
        def __init__(self, out, rc=0, err=b""):
            self.stdout, self.stderr, self.returncode = out, err, rc

    def fake(cmd, input=None, cwd=None, env=None, **kw):
        calls.append({"cmd": cmd, "input": input.decode("utf-8"), "env": env, "cwd": cwd})
        a = answers.pop(0) if answers else {"verdict": "approve", "note": "", "summary": "fine"}
        if isinstance(a, Done):
            return a
        return Done(json.dumps({"type": "result", "is_error": False, "result": json.dumps(a), "structured_output": a,
                                "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "cache_read_input_tokens": 100,
                                                                  "output_tokens": 20}}).encode())
    real = (conductor.RUNNER, team.claude_bin, server.save_settings, server.copy_to_clipboard, server.settings.get("conductor"))
    clip = []
    conductor.RUNNER, team.claude_bin = fake, (lambda: r"C:\fake\claude.exe")
    server.save_settings, server.copy_to_clipboard = (lambda: None), clip.append
    try:
        cmd = conductor.command("claude.exe", "sonnet", conductor.REVIEW_SCHEMA, conductor.REVIEW_SYSTEM)
        check("conductor: fixed command line, every tool off, nothing from the goal in it",
              cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
              and cmd[cmd.index("--model") + 1] in conductor.MODELS, cmd[:12])
        env = team.claude_env()
        check("conductor: no ANTHROPIC_* variables, so it runs on your own Claude Code login",
              not any(k.upper().startswith("ANTHROPIC_") for k in env))

        repo = temp_repo("conductor repo")
        with open(os.path.join(repo, "api-key.txt"), "w") as f:
            f.write("sk-not-a-real-key\n")
        subprocess.run([workspaces.GIT, "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A"], capture_output=True)
        subprocess.run([workspaces.GIT, "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "k"], capture_output=True)
        answers.append({"tasks": [{"agent": "pc", "title": "UI part", "goal": "1. add a button\nDone when: it shows", "owns": ["ui/"]},
                                  {"agent": "laptop", "title": "Server part", "goal": "1. add /api/x\nDone when: it answers", "owns": ["server/"]}]})
        ok, msg, b = server.team_plan(repo, "a feature", "conductor")
        sent = calls[-1]["input"]
        check("conductor: Claude plans, and the team is marked as conducted", ok and b["conductor"]["on"]
              and b["planner"]["backend"] == "conductor" and len(b["tasks"]) == 2, msg)
        check("conductor: the plan sees the small repo whole, but never a file that looks like a key",
              "=== server/api.py ===" in sent and "sk-not-a-real-key" not in sent and "a feature" in sent)
        check("conductor: the plan's call is in the log", [c["kind"] for c in b["conductor"].get("calls", [])] == ["plan"], b["conductor"])
        ok, msg, b2 = team.edit_board(repo, team.DEFAULT_AGENTS, "a feature", b["tasks"])
        check("conductor: editing the split keeps Claude conducting", b2["conductor"]["on"] is True)

        for t in b2["tasks"]:
            team.prepare(repo, b2, t, "vscode")
            t["status"] = "done"
        b2["state"] = "review"
        team.save_board(repo, b2)
        pc = b2["tasks"][0]["worktree"]
        with open(os.path.join(pc, "ui", "app.js"), "a") as f:
            f.write("buton()\n")
        team.commit_work(b2["tasks"][0])

        # 1: sent back with Claude's exact note, the agent is running again and its brief has the note
        answers.append({"verdict": "send_back", "note": "1. In ui/app.js rename buton() to button().", "summary": "typo"})
        server.conduct_after(repo, "t1")
        b = team.load_board(repo)
        t1 = b["tasks"][0]
        brief = open(os.path.join(pc, team.BRIEF_FILE), encoding="utf-8").read()
        check("conductor: Claude got the part's diff and task", "+buton()" in calls[-1]["input"] and "Done when: it shows" in calls[-1]["input"])
        check("conductor: send_back restarts the agent with Claude's note in its brief",
              t1["status"] == "running" and t1["feedback"][-1]["by"] == "conductor" and "rename buton() to button()" in brief
              and "Claude, who conducts this team" in brief, (t1["status"], t1.get("feedback")))
        check("conductor: a VS Code agent sent back by Claude doesn't touch your clipboard", not clip)
        check("conductor: the verdict shows on the task", t1["reviews"][-1]["verdict"] == "send_back" and t1["reviews"][-1]["sent"])

        # rounds: after MAX_ROUNDS send-backs, the next one is "over to you"
        for i in range(conductor.MAX_ROUNDS):
            team.set_status(repo, "t1", "done")
            answers.append({"verdict": "send_back", "note": "still wrong %d" % i, "summary": "no"})
            server.conduct_after(repo, "t1")
        t1 = team.load_board(repo)["tasks"][0]
        check("conductor: gives up after %d rounds and leaves it to you" % conductor.MAX_ROUNDS,
              t1["status"] == "done" and t1["reviews"][-1]["verdict"] == "give_up" and t1["reviews"][-1].get("out_of_rounds")
              and conductor.rounds(t1) == conductor.MAX_ROUNDS, [r["verdict"] for r in t1["reviews"]])
        check("conductor: each round shows Claude its earlier feedback", "still wrong 0" in calls[-1]["input"])

        # a failed Claude call changes nothing
        n = len(calls)
        answers.append(Done(b"", rc=1, err=b"Not logged in"))
        b = team.load_board(repo)
        b["tasks"][1]["status"] = "done"
        team.save_board(repo, b)
        server.conduct_after(repo, "t2")
        t2 = team.load_board(repo)["tasks"][1]
        check("conductor: a failed call is shown and the part stays done", t2["status"] == "done"
              and t2["reviews"][-1]["verdict"] == "error" and "Not logged in" in t2["reviews"][-1]["summary"], t2.get("reviews"))

        # both approved -> the whole-feature check; a fix goes back to its owner
        b = team.load_board(repo)
        b["tasks"][0]["reviews"], b["tasks"][1]["reviews"] = [], []
        team.save_board(repo, b)
        answers += [{"verdict": "approve", "note": "", "summary": "ok"}]
        server.conduct_after(repo, "t1")
        check("conductor: no whole-feature check while a part isn't approved", not team.load_board(repo)["conductor"].get("final"))
        answers += [{"verdict": "approve", "note": "", "summary": "ok"},
                    {"ok": False, "summary": "the route names differ", "fixes": [{"agent": "laptop", "note": "1. call it /api/x, not /api/y"}]}]
        server.conduct_after(repo, "t2")
        b = team.load_board(repo)
        check("conductor: when all parts are approved Claude checks them together and sends the fix to its owner",
              calls[-1]["input"].startswith("FEATURE:") and "=== AGENT pc" in calls[-1]["input"]
              and b["tasks"][1]["status"] == "running" and "/api/x, not /api/y" in b["tasks"][1]["feedback"][-1]["text"], b["conductor"].get("final"))
        team.set_status(repo, "t2", "done")
        answers += [{"verdict": "approve", "note": "", "summary": "ok"}, {"ok": True, "summary": "they fit", "fixes": []}]
        server.conduct_after(repo, "t2")
        b = team.load_board(repo)
        check("conductor: all approved and fitting: ready for you to merge, nothing merged",
              b["conductor"]["final"]["ok"] and all(t["status"] == "done" for t in b["tasks"]), b["conductor"].get("final"))
        check("conductor: calls and tokens are counted", b["conductor"]["stats"]["calls"] == len(calls)
              and b["conductor"]["stats"]["in"] > 0, b["conductor"]["stats"])

        # tests without pytest installed: the test file runs as a plain script (live run 2026-11-23)
        os.makedirs(os.path.join(pc, "tests"), exist_ok=True)
        open(os.path.join(pc, "tests", "test_export.py"), "w").close()
        py = __import__("shutil").which("python") or __import__("shutil").which("py")
        saved_pt = dict(team._pytest)
        team._pytest[py] = False
        cmd_np = team.test_command(pc)
        team._pytest[py] = True
        cmd_p = team.test_command(pc)
        team._pytest.clear(); team._pytest.update(saved_pt)
        os.remove(os.path.join(pc, "tests", "test_export.py"))
        check("tests: pytest only when it's installed, else the test file as a plain script",
              cmd_np[1:] == [os.path.join("tests", "test_export.py")] and cmd_p[1:] == ["-m", "pytest", "-q"], (cmd_np, cmd_p))

        # off: nothing is checked
        n = len(calls)
        conductor.set_on(repo, False)
        server.conduct_after(repo, "t1")
        check("conductor: switched off, Claude isn't called", len(calls) == n)

        # routes: PC only, a fixed list of models
        st, d = req("POST", "/api/team/conductor", json.dumps({"repo": repo, "on": True}))
        check("conductor: switched on from the page", st == 200 and team.load_board(repo)["conductor"]["on"], d)
        st, d = req("POST", "/api/team/conductor", json.dumps({"model": "claude-anything --tools Bash"}))
        check("conductor: only a model from the fixed list", st == 400 and (server.settings.get("conductor") or {}).get("model") != "claude-anything --tools Bash", d)
        as_phone(True)
        try:
            st, _ = req("POST", "/api/team/conductor", json.dumps({"repo": repo, "on": False}), **PHONE)
            st2, _ = req("POST", "/api/team/review", json.dumps({"repo": repo, "task": "t1"}), **PHONE)
        finally:
            as_phone(False)
        check("conductor: the phone can't switch it or ask for a check", st == 403 and st2 == 403 and team.load_board(repo)["conductor"]["on"], (st, st2))
        st, d = req("POST", "/api/team/conductor", json.dumps({"repo": repo, "on": False}), Origin="https://evil.example")
        check("conductor: a cross-site request is refused", st == 403, st)
    finally:
        conductor.RUNNER, team.claude_bin, server.save_settings, server.copy_to_clipboard, cnd = real
        if cnd is None:
            server.settings.pop("conductor", None)
        else:
            server.settings["conductor"] = cnd


def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="")


GROUPS = {"web": t_web, "modules": t_modules, "smuggle": t_smuggle, "key": t_key, "guard": t_guard, "phone": t_phone, "tools": t_tools, "tuner": t_tuner, "learn": t_learn, "laptop": t_laptop,
          "worktree": t_worktree, "team": t_team, "board": t_board, "teamrun": t_teamrun, "review": t_review, "slots": t_slots, "claude": t_claude,
          "conductor": t_conductor}

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
