"""Optional cloud help for the local model: web search and "ask a stronger model".

Off by default. The local model decides when to use them (tool calls); each call sends only the query or question
the local model wrote, never the conversation or the memories. Keys live in command-center/secrets.json, which is
never committed and never sent to the browser.

Providers (any one is enough; each has its own built-in web search):
  anthropic  Claude      via the official `anthropic` SDK (pip install anthropic), web_search server tool
  openai     ChatGPT     Responses API with the web_search tool (plain HTTPS, no SDK needed)
  gemini     Gemini      generateContent with Google Search grounding (plain HTTPS, no SDK needed)
"""
import http.client
import json
import os
import re
import threading
import time
import urllib.parse

import fsutil

HERE = os.environ.get("CC_STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "secrets.json")
MAX_CALLS = 3          # tool calls the local model gets per reply (each one is billed to the owner's key)
CACHE_SECONDS = 900    # an identical search within 15 minutes is answered from memory instead of paid for again
_cache = {}            # (provider, model, tool, normalised input) -> (time, result)
PROVIDERS = ("anthropic", "openai", "gemini")
NAMES = {"anthropic": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}
# Defaults; the model box in 6:set changes them (the "test" button lists what your key can use).
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5", "gemini": "gemini-2.5-flash"}
_lock = threading.Lock()

SEARCH_PROMPT = ("Search the web and answer with current, accurate information.\n\nQuery: {q}\n\n"
                 "Reply with a concise factual summary (the key facts, numbers and dates), then nothing else. "
                 "Sources are collected separately, so don't list links.")

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("Search the web. Use it for things that are current or that you don't reliably know: news, "
                        "prices, recent releases, schedules, documentation. Don't use it for things you already know. "
                        "Returns a summary with sources. Put everything you need into ONE specific query: a single "
                        "search usually answers the question, so search again only if the first result left out "
                        "something essential."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "what to look up"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "ask_cloud",
        "description": ("Ask a much stronger cloud AI model. Only for hard questions you can't answer well yourself "
                        "(difficult reasoning, maths, tricky code). It cannot see this conversation: write a complete, "
                        "self-contained question and leave out private details the user didn't ask you to share."),
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
]


# ---------------------------------------------------------------------------------------------- keys + settings
def _load():
    try:
        with open(SECRETS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    with _lock:
        fsutil.write_json(SECRETS, d, indent=1)


def key(p):
    return (_load().get("keys") or {}).get(p) or ""


def set_key(p, value):
    if p not in PROVIDERS:
        return False
    d = _load()
    d.setdefault("keys", {})[p] = "".join(str(value or "").split())[:400]
    if not d["keys"][p]:
        del d["keys"][p]
    _save(d)
    return True


def config(settings):
    c = settings.get("cloud") or {}
    models = dict(DEFAULT_MODELS, **(c.get("models") or {}))
    provider = c.get("provider") if c.get("provider") in PROVIDERS else next((p for p in PROVIDERS if key(p)), "anthropic")
    return {"enabled": bool(c.get("enabled")), "provider": provider, "models": models,
            "web_search": c.get("web_search", True), "ask_cloud": c.get("ask_cloud", True)}


def status(settings):
    c = config(settings)
    keys = {p: ("…" + key(p)[-4:]) if key(p) else "" for p in PROVIDERS}
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    return dict(c, keys=keys, names=NAMES, anthropic_sdk=sdk,
                ready=c["enabled"] and bool(key(c["provider"])) and (c["provider"] != "anthropic" or sdk))


def tools_for(settings):
    """The tool list to offer the local model, or None when cloud help is off / not set up."""
    c = config(settings)
    if not status(settings)["ready"]:
        return None
    t = [x for x in TOOLS if (x["function"]["name"] == "web_search" and c["web_search"]) or
         (x["function"]["name"] == "ask_cloud" and c["ask_cloud"])]
    return t or None


# ---------------------------------------------------------------------------------------------- HTTP helper
def _https(host, method, path, body=None, headers=None, timeout=120):
    c = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        c.request(method, path, json.dumps(body) if body is not None else None,
                  dict({"Content-Type": "application/json", "User-Agent": "command-center"}, **(headers or {})))
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, {"error": raw[:300].decode("utf-8", "replace")}
    finally:
        c.close()


def _err(j):
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        return e.get("message") or json.dumps(e)[:300]
    return str(e or j)[:300]


# ---------------------------------------------------------------------------------------------- providers
def _anthropic(model, prompt, search):
    import anthropic
    client = anthropic.Anthropic(api_key=key("anthropic"), max_retries=2, timeout=180)
    # Opus 5.5 / Sonnet 5.5 and the 4.6+ family get the dynamic-filtering search tool; older ones the basic one.
    newer = any(t in model for t in ("opus-5", "sonnet-5", "fable", "opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6"))
    tools = [{"type": "web_search_20260209" if newer else "web_search_20250305", "name": "web_search", "max_uses": 5}] if search else []
    messages = [{"role": "user", "content": prompt}]
    kw = dict(model=model, max_tokens=16000, messages=messages, tools=tools)
    if newer:
        kw["output_config"] = {"effort": "medium"}
    # Safety classifiers can decline a request; "default" re-runs a declined one on the recommended fallback model.
    fallback = any(t in model for t in ("opus-5", "fable", "sonnet-5-5"))
    sources, text = [], []
    for _ in range(5):  # pause_turn: the server-side search loop hit its limit; resend to let it continue
        try:
            if fallback:
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            else:
                resp = client.messages.create(**kw)
        except anthropic.AuthenticationError:
            return False, "Claude rejected the API key", []
        except anthropic.NotFoundError:
            return False, "Claude doesn't know the model %r (pick another in 6:set)" % model, []
        except anthropic.RateLimitError:
            return False, "Claude rate limit reached; try again shortly", []
        except anthropic.APIStatusError as e:
            return False, "Claude error %s: %s" % (e.status_code, str(e.message)[:200]), []
        except anthropic.APIConnectionError:
            return False, "couldn't reach the Claude API (internet down?)", []
        if resp.stop_reason == "refusal":
            return False, "Claude declined this request", []
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "web_search_tool_result" and isinstance(b.content, list):  # an error is an object, not a list
                sources += [{"title": r.title, "url": r.url} for r in b.content if getattr(r, "url", None)]
        if resp.stop_reason != "pause_turn":
            break
        # resume: the same user message plus everything the paused turn produced so far, as one assistant message
        paused = (messages[1]["content"] if len(messages) > 1 else []) + list(resp.content)
        messages = [messages[0], {"role": "assistant", "content": paused}]
        kw["messages"] = messages
    return True, "".join(text).strip(), sources


def _openai(model, prompt, search):
    body = {"model": model, "input": prompt}
    if search:
        body["tools"] = [{"type": "web_search"}]
    st, j = _https("api.openai.com", "POST", "/v1/responses", body, {"Authorization": "Bearer " + key("openai")}, 180)
    if st != 200:
        return False, "ChatGPT error %s: %s" % (st, _err(j)), []
    text, sources = [], []
    for item in j.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    text.append(c.get("text", ""))
                    sources += [{"title": a.get("title") or a.get("url"), "url": a.get("url")}
                                for a in c.get("annotations") or [] if a.get("type") == "url_citation" and a.get("url")]
    return True, "".join(text).strip(), sources


def _gemini(model, prompt, search):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    path = "/v1beta/models/%s:generateContent" % urllib.parse.quote(model, safe="-._")
    st, j = _https("generativelanguage.googleapis.com", "POST", path, body, {"x-goog-api-key": key("gemini")}, 180)
    if st != 200:
        return False, "Gemini error %s: %s" % (st, _err(j)), []
    cand = (j.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
    sources = [{"title": (c.get("web") or {}).get("title"), "url": (c.get("web") or {}).get("uri")} for c in chunks if c.get("web")]
    return True, text.strip(), sources


CALL = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini}


ARG = {"web_search": "query", "ask_cloud": "question"}


def parse_args(tool, raw):
    """The arguments of a tool call as a dict. Local models sometimes emit broken JSON (cut off, single quotes,
    a bare string); the one string argument is recovered where possible instead of wasting the call."""
    if isinstance(raw, dict):
        return raw
    raw = str(raw or "").strip()
    try:
        j = json.loads(raw or "{}")
        if isinstance(j, dict):
            return j
        if isinstance(j, str):
            return {ARG.get(tool, "query"): j}
    except ValueError:
        pass
    name = ARG.get(tool)
    if not name:
        return {}
    m = re.search(r"""["']%s["']\s*:\s*["'](.*)""" % name, raw, re.S)
    text = re.sub(r"""["']?\s*\}?\s*$""", "", m.group(1)) if m else ("" if raw[:1] in ("{", "[") else raw)
    return {name: text.strip()} if text.strip() else {}


def norm_input(tool, args):
    """What makes two calls 'the same': the tool's one string, lower-cased, with whitespace collapsed."""
    return " ".join(str((args or {}).get(ARG.get(tool, "query")) or "").lower().split())


def run(settings, tool, args):
    """Execute one tool call from the local model. -> {"ok", "text", "error", "sources", "provider", "model"}"""
    c = config(settings)
    p, model = c["provider"], c["models"][c["provider"]]

    def fail(msg):
        return {"ok": False, "text": "", "error": msg, "sources": [], "provider": NAMES[p], "model": model}

    if not key(p):
        return fail("no %s API key set (6:set → cloud)" % NAMES[p])
    if tool not in ARG:
        return fail("unknown tool %s" % str(tool)[:40])
    if not (c["web_search"] if tool == "web_search" else c["ask_cloud"]):
        return fail("%s is turned off in 6:set" % tool)
    q = str((args or {}).get(ARG[tool]) or "").strip()[:500 if tool == "web_search" else 8000]
    if not q:
        return fail("empty search query" if tool == "web_search" else "empty question")
    ck = (p, model, tool, norm_input(tool, {ARG[tool]: q}))
    with _lock:
        hit = _cache.get(ck)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return dict(hit[1], cached=True)
    ok, text, sources = CALL[p](model, SEARCH_PROMPT.format(q=q) if tool == "web_search" else q, True)
    seen, uniq = set(), []
    for s in sources:
        url = s.get("url") if isinstance(s, dict) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            uniq.append({"title": str(s.get("title") or url)[:150], "url": url[:2000]})
    res = {"ok": ok, "text": text if ok else "", "error": None if ok else text, "sources": uniq[:10],
           "provider": NAMES[p], "model": model}
    if ok and text:
        with _lock:
            _cache[ck] = (time.time(), res)
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:-64]:   # keep the 64 newest
                del _cache[k]
    return res


def list_models(p):
    """Check a key and list the models it can use. -> (ok, message, [model ids])"""
    k = key(p)
    if not k:
        return False, "no key saved", []
    try:
        if p == "anthropic":
            try:
                import anthropic
            except ImportError:
                return False, "the anthropic package isn't installed: run  python -m pip install --user anthropic", []
            try:
                ids = [m.id for m in anthropic.Anthropic(api_key=k, timeout=20).models.list()]
            except anthropic.AuthenticationError:
                return False, "key rejected", []
            except anthropic.APIError as e:
                return False, "error: %s" % str(e)[:200], []
        elif p == "openai":
            st, j = _https("api.openai.com", "GET", "/v1/models", None, {"Authorization": "Bearer " + k}, 20)
            if st != 200:
                return False, "error %s: %s" % (st, _err(j)), []
            ids = sorted((m.get("id") for m in j.get("data") or [] if m.get("id", "").startswith(("gpt", "o"))), reverse=True)
        else:
            st, j = _https("generativelanguage.googleapis.com", "GET", "/v1beta/models?pageSize=200", None, {"x-goog-api-key": k}, 20)
            if st != 200:
                return False, "error %s: %s" % (st, _err(j)), []
            ids = [m.get("name", "").split("/")[-1] for m in j.get("models") or []
                   if "generateContent" in (m.get("supportedGenerationMethods") or []) and "gemini" in m.get("name", "")]
    except OSError as e:
        return False, "couldn't connect (%s)" % e.__class__.__name__, []
    return True, "key works · %d models" % len(ids), ids[:80]
