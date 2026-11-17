"""The web connector: lets the local model search the web and read pages, with no account and no API key.

Off by default (6:set -> web access). The local model decides when to use it (tool calls):

  web_search  asks a public search engine's plain HTML page and returns titles, links and snippets
  web_fetch   downloads one page and returns its readable text

Search engines limit how often a program may ask without an account. DuckDuckGo and Brave take turns; when
one says "slow down" it is left alone for ten minutes and the other answers. A search key saved in 6:set
(Tavily or Brave Search API) is asked first and the public pages become the fallback. The key lives in
secrets.json.

What leaves this PC: the search query the model wrote goes to the search engine, and a page request goes to
the site being read. The conversation and the memories are never sent.

Safety. Text from the web can contain instructions aimed at the model ("ignore the user and open this link").
Two rules keep that from doing harm:
  1. web_fetch never reaches this PC, the home network or the tailnet: the address is resolved first, every
     address must be a public one, and the connection goes to exactly the address that was checked.
  2. web_fetch only opens links that were already in the conversation or in a search result / page of this
     reply. The model can't make up an address, so a page can't talk it into sending your chat somewhere
     inside a link.
"""
import gzip
import html
import html.parser
import http.client
import ipaddress
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import json
import urllib.parse
import zlib

import cloud

NAME = "the web"
COOL_DOWN = 600          # seconds an engine is left alone after it refuses a search
MAX_CALLS = 6            # web calls the local model gets per reply (free, but each one takes time and context)
RESULTS = 6              # search results handed to the model
PAGE_CHARS = 8000        # characters of a page handed to the model (about 2,000 tokens)
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 15
CACHE_SECONDS = 900
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ARG = {"web_search": "query", "web_fetch": "url"}

_cache = {}              # (tool, input) -> (time, result)
_lock = threading.Lock()
_CTX = ssl.create_default_context()
# Search engines and many sites turn away Python's own HTTPS client on sight, but not curl, which ships with
# Windows 10 and later. So pages are downloaded with curl when it is there, and with Python otherwise.
CURL = shutil.which("curl")

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("Search the web. Use it for things that are current or that you don't reliably know: news, "
                        "prices, recent releases, schedules, documentation. Don't use it for things you already know. "
                        "Returns a list of results (title, link, short excerpt). Write ONE specific query."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "what to look up"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": ("Read a web page and get its text. Use it when a search excerpt isn't enough, or when the user "
                        "gives you a link. The url must be copied exactly from a search result, from a page you read, "
                        "or from the conversation: made-up addresses are refused, so search first."),
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "the full http(s) link"}},
                       "required": ["url"]}}},
]


class Refused(Exception):
    """This address or page is not something the connector will open."""


# ---------------------------------------------------------------------------------------------- settings
def config(settings):
    c = settings.get("web") or {}
    return {"enabled": bool(c.get("enabled")), "fetch": bool(c.get("fetch", True))}


def status(settings):
    now = time.time()
    k = cloud.key("search")
    return dict(config(settings), engine=", ".join(([key_engine(k)] if k else []) + [e[0] for e in ENGINES]),
                max_calls=MAX_CALLS, key=("…" + k[-4:]) if k else "", key_engine=key_engine(k) if k else "",
                resting=[n for n, t in _resting.items() if t > now])


def tools_for(settings):
    c = config(settings)
    if not c["enabled"]:
        return []
    return [t for t in TOOLS if t["function"]["name"] == "web_search" or c["fetch"]]


# ---------------------------------------------------------------------------------------------- links
_URL = re.compile(r"""https?://[^\s<>"'`\\^{}|]+""", re.I)


def norm_url(u):
    """Two spellings of the same address compare equal: scheme and host in lower case, no #fragment, no final slash."""
    try:
        p = urllib.parse.urlsplit(str(u).strip())
        host = (p.hostname or "").lower().rstrip(".")
        port = ":%d" % p.port if p.port and p.port != {"http": 80, "https": 443}.get(p.scheme.lower()) else ""
    except ValueError:
        return ""
    return "%s://%s%s%s%s" % (p.scheme.lower(), host, port, p.path.rstrip("/"), "?" + p.query if p.query else "")


def urls_in(text):
    """Every link written in a piece of text, normalised."""
    out = set()
    for u in _URL.findall(text if isinstance(text, str) else ""):
        u = u.rstrip(".,;:!?]*_")
        while u.endswith(")") and u.count("(") < u.count(")"):
            u = u[:-1].rstrip(".,;:!?]*_")
        n = norm_url(u)
        if n:
            out.add(n)
    return out


def _public(ip):
    a = ipaddress.ip_address(ip)
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def check_url(url):
    """-> (scheme, host, port, path?query, ip). Raises Refused for anything that isn't a public web address."""
    try:
        p = urllib.parse.urlsplit(str(url).strip())
        port = p.port
    except ValueError:
        raise Refused("that isn't a valid link")
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        raise Refused("only http and https links can be opened")
    host = (p.hostname or "").rstrip(".")
    if not host or p.username or p.password:
        raise Refused("that isn't a valid link")
    if port not in (None, 80 if scheme == "http" else 443):
        raise Refused("only the standard web ports can be opened")
    port = 80 if scheme == "http" else 443
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}, key=lambda x: ":" in x)
    except (OSError, UnicodeError):
        raise Refused("the site %s couldn't be found" % host[:80])
    if not ips or not all(_public(i) for i in ips):   # one private address among them is enough to refuse
        raise Refused("that address is on this PC or a private network, which the web connector never opens")
    path = urllib.parse.quote(p.path or "/", safe="/%:@!$&'()*+,;=~-._")
    if p.query:
        path += "?" + urllib.parse.quote(p.query, safe="/%:@!$&'()*+,;=~-._?")
    return scheme, host, port, path, ips[0]


class _HTTP(http.client.HTTPConnection):
    """Connects to the address that was checked, not to whatever the name resolves to a moment later."""

    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=TIMEOUT)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=TIMEOUT, context=_CTX)
        self._ip = ip

    def connect(self):
        self.sock = _CTX.wrap_socket(socket.create_connection((self._ip, self.port), self.timeout), server_hostname=self.host)


def _via_python(scheme, host, port, path, ip, accept):
    """One GET with Python's client. -> (status, {header: value}, body)"""
    c = (_HTTPS if scheme == "https" else _HTTP)(host, port, ip)
    try:
        c.request("GET", path, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.8",
                                        "Accept-Encoding": "gzip, deflate"})
        r = c.getresponse()
        hdr = {k.lower(): v for k, v in r.getheaders()}
        if r.status in (301, 302, 303, 307, 308):
            return r.status, hdr, b""
        raw, deadline = b"", time.time() + 30
        while len(raw) < MAX_BYTES:
            if time.time() > deadline:
                raise Refused("the site took too long to answer")
            b = r.read(min(65536, MAX_BYTES - len(raw)))
            if not b:
                break
            raw += b
        enc = hdr.get("content-encoding", "").lower()
        try:
            if "gzip" in enc:
                raw = zlib.decompressobj(31).decompress(raw, MAX_BYTES * 4)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompressobj().decompress(raw, MAX_BYTES * 4)
                except zlib.error:
                    raw = zlib.decompressobj(-15).decompress(raw, MAX_BYTES * 4)
        except (zlib.error, gzip.BadGzipFile):
            pass
        return r.status, hdr, raw
    finally:
        c.close()


def _via_curl(scheme, host, port, path, ip, accept):
    """One GET with curl, connecting to the address that was checked (--resolve). -> (status, {header: value}, body)"""
    cmd = [CURL, "-q", "-sS", "-i", "--compressed", "--max-time", "30", "--connect-timeout", str(TIMEOUT),
           "--proto", "=http,https", "--max-redirs", "0", "--resolve", "%s:%d:%s" % (host, port, "[%s]" % ip if ":" in ip else ip),
           "-A", UA, "-H", "Accept: " + accept, "-H", "Accept-Language: en-US,en;q=0.8", "%s://%s%s" % (scheme, host, path)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = p.stdout.read(MAX_BYTES + 65536)   # more than this is cut off: nobody needs a 2 MB page
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        rc = p.wait()
    while True:   # headers first; a "100 Continue" or "103 Early Hints" block may come before the real ones
        head, sep, out = out.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        m = re.match(r"HTTP/[\d.]+ (\d{3})", lines[0])
        if not m:
            if rc in (35, 51, 58, 59, 60, 77, 90, 91):
                raise ssl.SSLError("curl %d" % rc)
            if rc == 28:
                raise Refused("the site took too long to answer")
            raise OSError("curl gave up (%d)" % rc)
        if not m.group(1).startswith("1") or not sep:
            break
    hdr = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdr[k.strip().lower()] = v.strip()
    return int(m.group(1)), hdr, out[:MAX_BYTES]


def _get(url, accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"):
    """GET a public page, following up to 5 redirects (each one checked again). -> (status, content type, bytes, final url)"""
    for _ in range(6):
        scheme, host, port, path, ip = check_url(url)
        st, hdr, raw = (_via_curl if CURL else _via_python)(scheme, host, port, path, ip, accept)
        if st in (301, 302, 303, 307, 308) and hdr.get("location"):
            url = urllib.parse.urljoin(url, hdr["location"])
            continue
        ctype = hdr.get("content-type", "").lower()
        kind = ctype.split(";")[0].strip()
        if st == 200 and kind and not (kind.startswith("text/") or kind.endswith(("json", "xml"))):
            raise Refused("that link is a %s file, not a page of text" % kind[:40])
        return st, ctype, raw, url
    raise Refused("too many redirects")


def _decode(raw, ctype):
    m = re.search(r"charset=([\w-]+)", ctype) or re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", raw[:4096], re.I)
    name = m.group(1) if m else "utf-8"
    name = name.decode("ascii", "replace") if isinstance(name, bytes) else name
    try:
        return raw.decode(name, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------------------------- page -> text
class _Text(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "aside", "form", "button", "iframe",
            "select", "head", "canvas", "dialog", "menu"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote",
             "table", "ul", "ol", "dt", "dd", "header", "main", "hr", "figcaption"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.title, self._skip, self._in_title = [], "", [], False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
        if tag in self.VOID:
            if tag in self.BLOCK and not self._skip:
                self.out.append("\n")
            return
        if tag in self.SKIP:
            self._skip.append(tag)
        elif not self._skip:
            if tag in self.BLOCK:
                self.out.append("\n")
            if tag in ("h1", "h2", "h3"):
                self.out.append("# ")
            elif tag == "li":
                self.out.append("- ")
            elif tag in ("td", "th"):
                self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()
        elif not self._skip and tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.out.append(data)


def page_text(doc):
    """Readable text of an HTML page. -> (title, text)"""
    p = _Text()
    try:
        p.feed(doc)
        p.close()
    except Exception:   # a badly broken page: keep what was read so far
        pass
    text = "".join(p.out).replace("\xa0", " ")
    lines = [" ".join(x.split()) for x in text.split("\n")]
    out, blank = [], False
    for x in lines:
        if x in ("", "-", "|", "#"):
            blank = bool(out)
            continue
        if blank:
            out.append("")
        out.append(x)
        blank = False
    return " ".join(p.title.split())[:200], "\n".join(out)


# ---------------------------------------------------------------------------------------------- search engines
def _plain(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _first(pattern, text):
    m = re.search(pattern, text, re.S)
    return _plain(m.group(1)) if m else ""


def _results(found):
    """Tidy (url, title, snippet) triples: real links only, no page twice."""
    out, seen = [], set()
    for url, title, snippet in found:
        n = norm_url(url)
        if not url.startswith(("http://", "https://")) or not n or n in seen:
            continue
        seen.add(n)
        out.append({"title": title[:150] or url, "url": url[:2000], "snippet": snippet[:400]})
    return out


def parse_ddg(doc):
    found = []
    for part in doc.split('class="result__a"')[1:]:
        href = html.unescape(_first(r'href="([^"]+)"', part))
        if href.startswith("//"):
            href = "https:" + href
        url = (urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg") or [href])[0]
        if "duckduckgo.com/y.js" in url:
            continue   # an advert
        found.append((url, _first(r">(.*?)</a>", part), _first(r"""class="result__snippet"[^>]*>(.*?)</a>""", part)))
    return _results(found)


def parse_brave(doc):
    found = []
    for part in doc.split('data-type="web"')[1:]:
        found.append((html.unescape(_first(r'<a[^>]+href="(https?://[^"]+)"', part)),
                      _first(r"""class="[^"]*search-snippet-title[^"]*"[^>]*>(.*?)</div>""", part),
                      _first(r"""class="content [^"]*"[^>]*>(.*?)</div>""", part)))
    return _results(found)


def key_engine(k):
    return "Tavily" if k.startswith("tvly-") else "Brave Search API"


def keyed(k, q):
    """Search with the owner's key. -> [results]. Raises Refused when the key is rejected or used up."""
    if key_engine(k) == "Tavily":
        host, method, path = "api.tavily.com", "POST", "/search"
        body, headers = json.dumps({"query": q, "max_results": RESULTS}), {"Authorization": "Bearer " + k, "Content-Type": "application/json"}
    else:
        host, method, path = "api.search.brave.com", "GET", "/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": RESULTS})
        body, headers = None, {"X-Subscription-Token": k, "Accept": "application/json"}
    c = http.client.HTTPSConnection(host, timeout=TIMEOUT, context=_CTX)
    try:
        c.request(method, path, body, dict(headers, **{"User-Agent": "command-center"}))
        r = c.getresponse()
        raw = r.read(MAX_BYTES)
    finally:
        c.close()
    if r.status != 200:
        raise Refused("%s answered with error %d" % (key_engine(k), r.status))
    j = json.loads(raw)
    rows = j.get("results") if key_engine(k) == "Tavily" else (j.get("web") or {}).get("results")
    return _results((str(x.get("url") or ""), _plain(str(x.get("title") or "")), _plain(str(x.get("content") or x.get("description") or "")))
                    for x in rows or [] if isinstance(x, dict))


ENGINES = [("DuckDuckGo", "https://html.duckduckgo.com/html/?", parse_ddg),
           ("Brave", "https://search.brave.com/search?source=web&", parse_brave)]
_resting = {}            # engine name -> time until which it is left alone
_last = {}               # engine name -> when it was last asked
_turn = [0]              # which public engine goes first next time (they take turns, so neither gets a burst)
GAP = 3                  # seconds between two questions to the same public engine


def search(q):
    """Ask the engines in turn until one answers. -> (ok, [results] or error message, engine name)"""
    tried, empty = [], None
    k = cloud.key("search")
    if k and _resting.get(key_engine(k), 0) <= time.time():
        tried.append(key_engine(k))
        try:
            res = keyed(k, q)
            if res:
                return True, res[:RESULTS], key_engine(k)
            empty = key_engine(k)
        except Refused:   # key rejected or used up: fall back to the public pages
            _resting[key_engine(k)] = time.time() + COOL_DOWN
        except (OSError, ValueError, http.client.HTTPException):
            pass
    with _lock:
        _turn[0] += 1
        order = ENGINES[_turn[0] % len(ENGINES):] + ENGINES[:_turn[0] % len(ENGINES)]
    refused = 0
    for name, base, parse in order:
        if _resting.get(name, 0) > time.time():
            refused += 1
            continue
        tried.append(name)
        with _lock:
            wait = _last.get(name, 0) + GAP - time.time()
            _last[name] = time.time() + max(0, wait)
        if wait > 0:
            time.sleep(wait)
        try:
            st, ctype, raw, _ = _get(base + urllib.parse.urlencode({"q": q}))
        except (Refused, OSError, http.client.HTTPException):
            continue   # unreachable right now: the next one may work, and this one is tried again next time
        res = parse(_decode(raw, ctype)) if st == 200 else []
        if res:
            return True, res[:RESULTS], name
        if st != 200 or re.search(rb"captcha|anomaly|unusual traffic", raw, re.I):
            _resting[name] = time.time() + COOL_DOWN
            refused += 1
        else:
            empty = name
    if empty:
        return True, [], empty
    if refused:
        return False, ("the search engines are limiting searches from this connection right now. Don't search again in "
                       "this reply; it works again in a few minutes (a search key in 6:set avoids this)"), NAME
    return False, "no search engine could be reached (%s). Is the internet up?" % ", ".join(tried), NAME


def fetch(url):
    """-> (ok, title, text, final url) or (False, error message, "", url)"""
    try:
        st, ctype, raw, final = _get(url)
    except Refused as e:
        return False, str(e), "", url
    except ssl.SSLError:
        return False, "that site's security certificate couldn't be verified", "", url
    except (OSError, http.client.HTTPException) as e:
        return False, "couldn't open the page (%s)" % e.__class__.__name__, "", url
    if st != 200:
        return False, "the site answered with error %d" % st, "", final
    doc = _decode(raw, ctype)
    if "html" in ctype or (not ctype and "<html" in doc[:2000].lower()) or re.match(r"\s*<(!doctype|html)", doc[:200], re.I):
        title, text = page_text(doc)
    else:
        title, text = "", doc.strip()
    if not text:
        return False, "the page has no readable text (it probably needs a browser to show anything)", "", final
    return True, title or final, text, final


def norm_input(tool, args):
    v = str((args or {}).get(ARG.get(tool, "query")) or "")
    return norm_url(v) if tool == "web_fetch" else " ".join(v.lower().split())


def run(settings, tool, args, seen):
    """Execute one web tool call from the local model. `seen` is the set of links (norm_url) the model may open;
    links found in the result are added to it. -> {"ok", "text", "error", "sources", "provider", "cached"?}"""
    c = config(settings)

    def fail(msg):
        return {"ok": False, "text": "", "error": msg, "sources": [], "provider": NAME}

    if tool not in ARG or not c["enabled"] or (tool == "web_fetch" and not c["fetch"]):
        return fail("%s is turned off in 6:set" % str(tool)[:40])
    v = str((args or {}).get(ARG[tool]) or "").strip()[:2000 if tool == "web_fetch" else 400]
    if not v:
        return fail("no link given" if tool == "web_fetch" else "empty search query")
    if tool == "web_fetch":
        if not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        if norm_url(v) not in seen:
            return fail("that link wasn't in the conversation or in a search result, so it isn't opened. "
                        "Search for the page first, then open a link from the results")
    ck = (tool, norm_input(tool, {ARG[tool]: v}))
    with _lock:
        hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        res = dict(hit[1], cached=True)
    elif tool == "web_search":
        ok, got, engine = search(v)
        if not ok:
            return fail(got)
        lines = ["%d. %s\n   %s\n   %s" % (i + 1, r["title"], r["url"], r["snippet"]) for i, r in enumerate(got)]
        text = ("Web search results for \"%s\":\n\n%s" % (v, "\n".join(lines))) if got else "No results for \"%s\"." % v
        if got and c["fetch"]:
            text += "\n\nThese are short excerpts. If they don't answer the question, read a result with web_fetch."
        res = {"ok": True, "text": text, "error": None, "provider": engine,
               "sources": [{"title": r["title"], "url": r["url"]} for r in got]}
    else:
        ok, title, text, final = fetch(v)
        if not ok:
            return fail(title)
        more = len(text) - PAGE_CHARS
        if more > 0:
            text = text[:PAGE_CHARS] + "\n[the page continues for about %d more characters, not shown]" % more
        res = {"ok": True, "text": "Text of the page \"%s\" (%s):\n\n%s" % (title, final, text), "error": None,
               "provider": urllib.parse.urlsplit(final).hostname or NAME, "sources": [{"title": title[:150], "url": final[:2000]}]}
    if not res.get("cached"):
        with _lock:
            _cache[ck] = (time.time(), res)
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:-64]:   # keep the 64 newest
                del _cache[k]
    seen |= urls_in(res["text"])
    return res


def test():
    """For the "test" button in 6:set. -> (ok, message)"""
    ok, got, engine = search("wikipedia")
    if not ok:
        return False, got
    return (True, "search works · %d results from %s" % (len(got), engine)) if got else (False, "%s answered, but with no results" % engine)
