"""Auto-optimise: measure, once per model, how it runs fastest on this PC, and reuse the result on every later load.

The first time a model is turned on (with "optimise new models" on in the model list), a few trial copies of
llama-server are started one after another on a spare port, each with different settings. Each trial reads a long
prompt and writes a short text, its speed and peak VRAM are recorded, and the fastest one that leaves VRAM to spare
is kept in command-center\\tuning.json. Nothing is guessed from the model's name: it works for any GGUF.

What is tried (the settings that measurably matter; thread count, priority and polling do not):
  placement   how much of the model sits on the GPU: llama.cpp's own fit with different VRAM reserves, and for
              mixture-of-experts models an explicit number of expert layers kept in RAM (--n-cpu-moe)
  batch       the prompt batch size (-ub): larger reads long prompts faster but costs VRAM

A result belongs to one model file, one context size, one mode (fast text / images) and one GPU; change any of
them and it is measured again. A newer llama.cpp build does not discard results (the engine updates often): use
"optimise again" in the model list after a big engine change.

The measured search below runs only when asked ("optimise" in the model list). Without it, models learn while in
use (see "learning while in use"): no load ever waits for trial runs.

When the default batch doesn't fit at all (some architectures, e.g. Inkling-2-Mini, keep no fused attention, so
their compute buffer grows with context x batch), the size llama-server logs sets the batch that fits in one retry,
and the rest is measured on top of it. If saved settings later stop loading, server.py falls back to the defaults
at once (mark_stale) instead of measuring again.
"""
import http.client
import json
import os
import subprocess
import threading
import time

import fsutil
import models

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "tuning.json")
LOG = os.path.join(HERE, "tune.log")
PORT = 8191                 # trial servers; 8090 is the model, 8091/8192 embeddings, 8190 bench.py
NOWIN = 0x08000000
HEADROOM_GB = 0.6           # a trial leaving less VRAM free than this is never chosen: on a 16 GB card speed halves
                            # once use passes ~15.4 GB (Windows starts paging VRAM), so the edge is kept clear
WORTH = 1.03                # a change has to be at least 3% faster to replace the default (below that it is noise)

S = {"running": False, "model": None, "msg": "", "step": 0, "of": 0, "cancel": False, "proc": None}
_lock = threading.Lock()


# ------------------------------------------------------------------ saved results
def key(m, ctx, mode, hw):
    try:
        st = os.stat(m["_path"])
        sig = "%d.%d" % (st.st_size, int(st.st_mtime))
    except OSError:
        sig = "?"
    return "|".join([m["id"], sig, str(ctx), "fast" if models.mtp_on(m, mode) else "std", str(hw.get("gpu")), str(hw.get("vram_gb"))])


def _all():
    d = fsutil.read_json(PROFILES, {})
    return d if isinstance(d, dict) else {}


def profile(m, ctx, mode, hw=None):
    """The saved result for this model as it would be launched now, or None."""
    p = _all().get(key(m, ctx, mode, hw or models.hardware()))
    return p if isinstance(p, dict) and isinstance(p.get("best"), dict) else None


def best(m, ctx, mode, hw=None):
    p = profile(m, ctx, mode, hw)
    return p["best"] if p else None


def forget(m, ctx, mode, hw=None):
    with _lock:
        d = _all()
        if d.pop(key(m, ctx, mode, hw or models.hardware()), None) is not None:
            fsutil.write_json(PROFILES, d, indent=1)


def mark_stale(m, ctx, mode, hw=None):
    """The saved settings no longer load (engine or driver update, less free VRAM): fall back to the defaults from
    now on without measuring again, so the next load isn't any slower. "optimise again" measures afresh."""
    with _lock:
        d = _all()
        p = d.get(key(m, ctx, mode, hw or models.hardware()))
        if isinstance(p, dict) and p.get("best"):
            p.update(best={}, stale=int(time.time()))
            fsutil.write_json(PROFILES, d, indent=1)
            return True
    return False


def summary(m, ctx, mode, hw=None):
    """One line for the model list, or None when this model hasn't been measured."""
    p = profile(m, ctx, mode, hw)
    if not p:
        return None
    lp = learning(m, ctx, mode, hw)
    if lp:
        s = "learning the fastest settings while you use it: %d of about %d judged" % lp
        r = p.get("result") or {}
        return s + (" (best so far %.0f tok/s writing)" % r["tg"] if r.get("tg") else "")
    r, b = p.get("result") or {}, p.get("base") or {}
    s = "optimised for this PC: %.0f tok/s writing, %s tok/s reading" % (r.get("tg") or 0, format(int(r.get("pp") or 0), ","))
    gain = (r.get("tg") or 0) / b["tg"] - 1 if b.get("tg") else 0
    gain_pp = (r.get("pp") or 0) / b["pp"] - 1 if b.get("pp") else 0
    if gain >= 0.03 or gain_pp >= 0.05:
        s += " (%s vs the default)" % ", ".join(x for x in ("+%.0f%% writing" % (gain * 100) if gain >= 0.03 else "",
                                                          "+%.0f%% reading" % (gain_pp * 100) if gain_pp >= 0.05 else "") if x)
    elif p["best"] and r.get("vram_gb") and b.get("vram_gb") and r["vram_gb"] <= b["vram_gb"] - 0.3:
        s += " (as fast as the default with %.1f GB more VRAM free)" % (b["vram_gb"] - r["vram_gb"])
    else:
        s += " (the default was already the fastest)"
    return s


# ------------------------------------------------------------------ what to try
def describe(t):
    if not t:
        return "default"
    bits = []
    if "moe" in t:
        bits.append("%d expert layers in RAM" % t["moe"])
    if "fit_target" in t:
        bits.append("%d MB VRAM reserve" % t["fit_target"])
    if "ub" in t:
        bits.append("batch %d" % t["ub"])
    return ", ".join(bits) or "default"


def placements(m, ctx, hw):
    """Placement candidates after the default, most promising first."""
    if not hw.get("vram_gb"):
        return []
    plan = models.plan(m, ctx, hw)
    if m.get("tuned"):   # launched with an explicit --n-cpu-moe from llm-config.bat: look one layer to each side
        var, dflt = models.TUNED[m["id"]]
        n = int(models.config().get(var) or dflt) + (3 if ctx > models.DEFAULT_CTX else 0)
        return [{"moe": n - 1}, {"moe": n - 2}, {"moe": n + 1}]
    if plan["fit"] == "gpu":
        return [{"fit_target": 256}, {"fit_target": 1024}]
    if plan["fit"] == "split":
        n = plan.get("moe_ram_layers") or 0
        out = [{"fit_target": 1024}, {"fit_target": 512}]
        if n:
            out += [{"moe": n}, {"moe": max(0, n - 2)}, {"moe": n + 2}]
        return out
    if plan["fit"] == "ram":
        return [{"fit_target": 1024}, {"fit_target": 512}]
    return []


def batches(hw, current):
    if (hw.get("vram_gb") or 0) < 8:
        return []
    return [{"ub": u} for u in (4096, 1024) if u != current]


# ------------------------------------------------------------------ one trial
_WORDS = ("signal river copper window lantern orbit meadow circuit harbor granite velvet ember compass thistle "
          "anchor prism willow engine marble falcon canyon ribbon quartz timber beacon cobalt saddle mirror "
          "garden silver thunder paper bridge candle forest violet ocean ladder button rocket").split()


def _prompt(tokens=5000):
    """A fixed text of roughly `tokens` tokens that no model has cached: numbered sentences from a word list."""
    seed, out = 12345, []
    for i in range(tokens // 14):
        ws = []
        for _ in range(9):
            seed = (seed * 1103515245 + 12345) % 2147483648
            ws.append(_WORDS[seed % len(_WORDS)])
        out.append("%d. The %s %s near the %s while the %s %s the %s %s beside a %s %s." % (i + 1, *ws))
    return "\n".join(out)


def _req(method, path, body=None, timeout=600):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        hd = {"Content-Type": "application/json"}
        try:
            with open(models.KEYFILE, encoding="utf-8-sig") as f:
                hd["Authorization"] = "Bearer " + f.readline().strip()
        except OSError:
            pass
        c.request(method, path, json.dumps(body) if body is not None else None, hd)
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, None
    finally:
        c.close()


def _vram_used():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=NOWIN).stdout.strip().splitlines()
        return float(out[0]) / 1024
    except Exception:
        return None


def _kill(p):
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=20)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    for _ in range(40):   # the port (and the VRAM) must be free before the next trial
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.3)
            c.request("GET", "/health")
            c.getresponse().read()
            c.close()
            time.sleep(0.25)
        except OSError:
            break
    time.sleep(1.0)


def trial(m, ctx, mode, tune, quick=False):
    """Load the model with these settings on the trial port and measure it.
    -> {"ok", "tg", "pp", "vram_gb", "load_s", "why"}"""
    args = models.command(m, ctx, mode, tune)
    args[args.index("--port") + 1] = str(PORT)
    res = {"ok": False, "tune": tune or {}, "tg": 0.0, "pp": 0.0, "vram_gb": None, "load_s": None, "why": ""}
    peak = [_vram_used() or 0.0]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.5):
            v = _vram_used()
            if v:
                peak[0] = max(peak[0], v)

    log = open(LOG, "a", encoding="utf-8", errors="replace")
    log.write("\n# %s  %s\n# %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), describe(tune), " ".join('"%s"' % a if " " in a else a for a in args)))
    log.flush()
    start = log.tell()
    t0 = time.time()
    p = subprocess.Popen(args, cwd=models.ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    S["proc"] = p
    threading.Thread(target=sample, daemon=True).start()
    try:
        while True:   # wait for the model to load
            if S["cancel"]:
                res["why"] = "cancelled"
                return res
            if p.poll() is not None:
                res["why"] = "didn't load (most likely out of memory)"
                try:
                    with open(LOG, encoding="utf-8", errors="replace") as f:
                        f.seek(start)
                        text = f.read()
                        if models.compute_oom(text):
                            res["compute_oom"] = True
                            res["fit_ub"] = models.fit_ub(text, args[args.index("-ub") + 1], m, ctx)
                            res["why"] = "didn't load: batch %s needs more VRAM than there is" % args[args.index("-ub") + 1]
                except (OSError, ValueError):
                    pass
                return res
            if time.time() - t0 > 600:
                res["why"] = "took more than 10 minutes to load"
                return res
            try:
                if _req("GET", "/health", timeout=2)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        res["load_s"] = round(time.time() - t0, 1)
        base = {"model": "x", "temperature": 0, "cache_prompt": False, "stream": False}
        _req("POST", "/v1/chat/completions", dict(base, messages=[{"role": "user", "content": "Say hi."}], max_tokens=4))   # warm-up
        if S["cancel"]:
            res["why"] = "cancelled"
            return res
        # reading: a long prompt nobody has seen, one token out
        st, j = _req("POST", "/v1/chat/completions", dict(base, max_tokens=1, messages=[
            {"role": "user", "content": _prompt(1500 if quick else 5000) + "\n\nHow many sentences are above? Answer with a number."}]))
        if st != 200 or not j:
            res["why"] = "the reading test failed (HTTP %s)" % st
            return res
        res["pp"] = round(float((j.get("timings") or {}).get("prompt_per_second") or 0), 1)
        if S["cancel"]:
            res["why"] = "cancelled"
            return res
        # writing: free text
        st, j = _req("POST", "/v1/chat/completions", dict(base, max_tokens=48 if quick else 160, messages=[
            {"role": "user", "content": "Explain in detail how a bicycle stays upright while moving."}]))
        if st != 200 or not j:
            res["why"] = "the writing test failed (HTTP %s)" % st
            return res
        res["tg"] = round(float((j.get("timings") or {}).get("predicted_per_second") or 0), 1)
        res["ok"] = res["tg"] > 0
        if not res["ok"]:
            res["why"] = "the server reported no speed"
        return res
    except (OSError, http.client.HTTPException, ValueError) as e:
        res["why"] = "trial failed: %s" % e.__class__.__name__
        return res
    finally:
        stop.set()
        v = _vram_used()
        if v:
            peak[0] = max(peak[0], v)
        res["vram_gb"] = round(peak[0], 2) if peak[0] else None
        _kill(p)
        S["proc"] = None
        log.close()


# ------------------------------------------------------------------ learning while in use
# No trial runs before a model can be used: every load starts at once. While you use the model, llama-server's own
# counters (tokens and seconds spent reading and writing) and the GPU's peak VRAM are recorded for the settings it
# was started with. Once those settings have done enough real work they are judged, and the next load tries the
# next candidate (one per load). The same rules as the measured search (choose) pick the winner; when every
# candidate is judged, the winner is kept and nothing more is tried. "optimise" in the model list still runs the
# measured search on request.
LEARN_OUT = 1500      # tokens written in real use before a setting is judged
LEARN_IN = 2000       # tokens read (for the batch-size judgement); judged on writing alone after 3 x LEARN_OUT
LEARN_EARLY = 200     # ...but a candidate writing below LEARN_SLOW x the best known speed is judged after this many
LEARN_SLOW = 0.8
COUNTERS = ("llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total",
            "llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total")


def parse_counters(text):
    """llama-server's /metrics -> {name: value} for COUNTERS, or None when they aren't all there."""
    vals = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in COUNTERS:
            try:
                vals[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return vals if len(vals) == len(COUNTERS) else None


def _same(a, b):
    return json.dumps(a or {}, sort_keys=True) == json.dumps(b or {}, sort_keys=True)


def _candidates(m, ctx, mode, hw, base):
    """Placements to learn, on top of the base batch. Batch sizes come after, on the winning placement."""
    return [dict(t, **({"ub": base["ub"]} if "ub" in base else {})) for t in placements(m, ctx, hw)]


def _batches_for(m, ctx, mode, hw, best, base):
    if "ub" in base:   # the default batch didn't fit: bigger ones can't, smaller ones were the fallback
        return []
    cmd = models.command(m, ctx, mode)
    ub_now = int(cmd[cmd.index("-ub") + 1]) if "-ub" in cmd else 2048
    return [dict(best, **u) for u in batches(hw, ub_now)]


def launch_tune(m, ctx, mode, base=None, settle=False, hw=None):
    """The settings to start this model with now, and whether they are being learned (-> (tune, learning)).
    `base` is what the model starts with before anything is known ({} or a fallback batch). `settle` starts the
    best known settings without trying anything new (after a candidate failed to load)."""
    hw = hw or models.hardware()
    base = dict(base or {})
    k = key(m, ctx, mode, hw)
    with _lock:
        d = _all()
        p = d.get(k)
        if not isinstance(p, dict) or not isinstance(p.get("best"), dict):
            p = {"best": base, "when": int(time.time()), "gpu": hw.get("gpu"),
                 "learn": {"base": base, "trying": base, "queue": _candidates(m, ctx, mode, hw, base),
                           "batches": False, "done": []}}
            d[k] = p
            fsutil.write_json(PROFILES, d, indent=1)
            return base, True
        L = p.get("learn")
        if not isinstance(L, dict) or settle:
            return p["best"], False
        if L.get("trying") is None:
            if not L["queue"] and not L["batches"]:
                L["batches"] = True
                L["queue"] = _batches_for(m, ctx, mode, hw, p["best"], L["base"])
            if not L["queue"]:
                p.pop("learn")   # everything judged: keep the winner, try nothing more
                fsutil.write_json(PROFILES, d, indent=1)
                return p["best"], False
            L["trying"] = L["queue"].pop(0)
            fsutil.write_json(PROFILES, d, indent=1)
        return L["trying"], True


def _rec(tune, ok, tg=0.0, pp=0.0, vram=None, why="", n_out=0, n_in=0):
    return {"tune": tune, "ok": ok, "tg": round(tg, 1), "pp": round(pp, 1), "vram_gb": vram, "why": why,
            "n_out": int(n_out), "n_in": int(n_in)}


def _judge(p, hw):
    """Re-pick the best from everything judged so far (the base is always judged first)."""
    L = p["learn"]
    done = [r for r in L["done"] if r.get("ok")]
    base = next((r for r in done if _same(r["tune"], L["base"])), None)
    if not base:
        return
    winner = choose(base, [r for r in L["done"] if not _same(r["tune"], L["base"])], hw)
    p.update(best=winner["tune"], result={k: winner[k] for k in ("tg", "pp", "vram_gb")},
             base={k: base[k] for k in ("tg", "pp", "vram_gb")}, when=int(time.time()))


def observe(m, ctx, mode, tune, sums, hw=None):
    """Real use of `tune` so far this load: sums = {"n_out", "s_out", "n_in", "s_in", "vram"}. Judges the setting
    once it has done enough work and returns True then (stop observing this load)."""
    if sums["s_out"] <= 0 or sums["n_out"] < LEARN_EARLY:
        return False
    enough = sums["n_out"] >= LEARN_OUT and (sums["n_in"] >= LEARN_IN or sums["n_out"] >= 3 * LEARN_OUT)
    hw = hw or models.hardware()
    with _lock:
        d = _all()
        p = d.get(key(m, ctx, mode, hw))
        L = isinstance(p, dict) and p.get("learn")
        if not L or not _same(L.get("trying"), tune):
            return True
        # a candidate already clearly slower than the best known one is judged early, so it isn't tried again
        best_tg = (p.get("result") or {}).get("tg") or 0
        if not enough and not (best_tg and sums["n_out"] / sums["s_out"] < best_tg * LEARN_SLOW):
            return False
        pp = sums["n_in"] / sums["s_in"] if sums["s_in"] > 0 else 0.0
        L["done"].append(_rec(tune, True, sums["n_out"] / sums["s_out"], pp, sums.get("vram"),
                              n_out=sums["n_out"], n_in=sums["n_in"]))
        L["trying"] = None
        _judge(p, hw)
        fsutil.write_json(PROFILES, d, indent=1)
    return True


def failed(m, ctx, mode, tune, why, hw=None):
    """A candidate being learned didn't load: never try it again. True if it was a candidate (not the base)."""
    hw = hw or models.hardware()
    with _lock:
        d = _all()
        p = d.get(key(m, ctx, mode, hw))
        L = isinstance(p, dict) and p.get("learn")
        if not L or not _same(L.get("trying"), tune) or _same(tune, L["base"]):
            return False
        L["done"].append(_rec(tune, False, why=why))
        L["trying"] = None
        fsutil.write_json(PROFILES, d, indent=1)
    return True


def learning(m, ctx, mode, hw=None):
    """Progress for the model list: (judged, total) while learning, else None."""
    p = _all().get(key(m, ctx, mode, hw or models.hardware()))
    L = isinstance(p, dict) and p.get("learn")
    if not L:
        return None
    n = len(L["done"])
    return n, n + len(L["queue"]) + (1 if L.get("trying") is not None else 0) + (0 if L["batches"] else 2)


# ------------------------------------------------------------------ the search
def _safe(r, hw):
    return r["ok"] and (not hw.get("vram_gb") or r["vram_gb"] is None or r["vram_gb"] <= hw["vram_gb"] - HEADROOM_GB)


def choose(base, trials, hw):
    """The winner among the default and the trials. Writing speed decides placement; reading speed decides the
    batch size, as long as writing doesn't suffer. Anything not clearly better than the default loses to it."""
    # placement: the fastest writer and, among those within 3% of it, the one that uses the least VRAM (room to
    # spare matters more than a difference that small: a model that touches the VRAM limit slows to a crawl)
    best = base
    bub = base["tune"].get("ub")
    cands = [r for r in trials if r["tune"].get("ub") == bub and _safe(r, hw)]
    if cands:
        top = max(r["tg"] for r in cands)
        if top > base["tg"] * WORTH:
            # ...but whatever is picked must itself beat the default by 3%, or the default stays
            near = [r for r in cands if r["tg"] >= top / WORTH and r["tg"] > base["tg"] * WORTH]
            best = min(near, key=lambda r: (r["vram_gb"] if r["vram_gb"] is not None else 99.0, -r["tg"]))
    place = best
    for r in trials:   # batch size, measured on top of the chosen placement
        if r["tune"].get("ub") == bub or not _safe(r, hw):
            continue
        if r["tg"] < place["tg"] * 0.97:
            continue
        faster = r["pp"] > best["pp"] * 1.05
        # the same speed for clearly less VRAM is a win too: it is room that keeps the model off the VRAM limit
        leaner = (r["pp"] >= best["pp"] * 0.97 and r["vram_gb"] is not None and best["vram_gb"] is not None
                  and r["vram_gb"] <= best["vram_gb"] - 0.4)
        if faster or leaner:
            best = r
    return best


def optimise(m, ctx, mode, quick=False):
    """Run the whole search for one model (blocking, a few minutes). Returns the saved profile, or None if it was
    cancelled or the model doesn't load even with the default settings."""
    hw = models.hardware()
    S.update(running=True, model=m["id"], cancel=False, step=0, of=1, msg="optimising %s for this PC" % m["name"])
    try:
        try:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 2_000_000:
                os.replace(LOG, LOG + ".old")
        except OSError:
            pass
        place = placements(m, ctx, hw)
        cmd = models.command(m, ctx, mode)
        ub_now = int(cmd[cmd.index("-ub") + 1]) if "-ub" in cmd else 2048
        ubs = batches(hw, ub_now)
        S["of"] = 2 + len(place) + len(ubs)   # the default is measured first and once more at the end

        def run(tune):
            S["step"] += 1
            S["msg"] = "optimising %s: test %d of %d (%s)" % (m["name"], S["step"], S["of"], describe(tune))
            return trial(m, ctx, mode, tune, quick)

        base, floor = run(None), {}
        if S["cancel"]:
            return None
        # The default batch doesn't fit at all (compute buffer out of memory): jump to the batch the reported size
        # says fits (usually one retry), and measure everything else on top of it. Bigger batches can't fit then.
        u = base.get("fit_ub")
        while u and not base["ok"]:
            S["of"] += 1
            floor = {"ub": u}
            base = run(floor)
            if S["cancel"]:
                return None
            u = base.get("fit_ub")
        if not base["ok"]:
            S["msg"] = "couldn't optimise %s: %s" % (m["name"], base["why"])
            return None
        if floor:
            place, ubs = [dict(t, **floor) for t in place], []
            S["of"] -= len(batches(hw, ub_now))
        trials, too_low = [], None
        for t in place:
            if S["cancel"]:
                return None
            if "moe" in t and too_low is not None and t["moe"] <= too_low:
                S["step"] += 1   # even fewer layers in RAM than a setting that already ran out of VRAM: can't work
                continue
            r = run(t)
            trials.append(r)
            if "moe" in t and not _safe(r, hw):
                too_low = t["moe"] if too_low is None else max(too_low, t["moe"])
        winner = choose(base, trials, hw)
        for u in ubs:
            if S["cancel"]:
                return None
            r = run(dict(winner["tune"], **u))
            trials.append(r)
        if S["cancel"]:
            return None
        # The default ran first, possibly with the model file not yet in Windows' file cache or the GPU still
        # settling, so it is measured once more now and the better run counts. Without this, a slow first run
        # would make every other setting look like a huge gain.
        again = run(floor or None)
        if S["cancel"]:
            return None
        if again["ok"] and again["tg"] > base["tg"]:
            base = again
        winner = choose(base, trials, hw)
        prof = {"best": winner["tune"], "result": {k: winner[k] for k in ("tg", "pp", "vram_gb", "load_s")},
                "base": {k: base[k] for k in ("tg", "pp", "vram_gb", "load_s")},
                "trials": [dict(tune=r["tune"], ok=r["ok"], tg=r["tg"], pp=r["pp"], vram_gb=r["vram_gb"], why=r["why"]) for r in [base] + trials],
                "when": int(time.time()), "gpu": hw.get("gpu"), "quick": bool(quick)}
        with _lock:
            d = _all()
            d[key(m, ctx, mode, hw)] = prof
            fsutil.write_json(PROFILES, d, indent=1)
        S["msg"] = "optimised %s: %s" % (m["name"], describe(winner["tune"]))
        return prof
    finally:
        S.update(running=False, proc=None)


def cancel():
    """Stop a running optimisation (the model is being turned off, or another one was picked)."""
    if not S["running"]:
        return
    S["cancel"] = True
    _kill(S.get("proc"))
    for _ in range(120):
        if not S["running"]:
            break
        time.sleep(0.25)
