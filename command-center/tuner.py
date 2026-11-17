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
HEADROOM_GB = 0.4           # a trial that leaves less VRAM free than this is not chosen: the desktop needs room to breathe
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


def summary(m, ctx, mode, hw=None):
    """One line for the model list, or None when this model hasn't been measured."""
    p = profile(m, ctx, mode, hw)
    if not p:
        return None
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


# ------------------------------------------------------------------ the search
def _safe(r, hw):
    return r["ok"] and (not hw.get("vram_gb") or r["vram_gb"] is None or r["vram_gb"] <= hw["vram_gb"] - HEADROOM_GB)


def choose(base, trials, hw):
    """The winner among the default and the trials. Writing speed decides placement; reading speed decides the
    batch size, as long as writing doesn't suffer. Anything not clearly better than the default loses to it."""
    # placement: the fastest writer and, among those within 3% of it, the one that uses the least VRAM (room to
    # spare matters more than a difference that small: a model that touches the VRAM limit slows to a crawl)
    best = base
    cands = [r for r in trials if "ub" not in r["tune"] and _safe(r, hw)]
    if cands:
        top = max(r["tg"] for r in cands)
        if top > base["tg"] * WORTH:
            near = [r for r in cands if r["tg"] >= top / WORTH]
            best = min(near, key=lambda r: (r["vram_gb"] if r["vram_gb"] is not None else 99.0, -r["tg"]))
    place = best
    for r in trials:   # batch size, measured on top of the chosen placement
        if "ub" not in r["tune"] or not _safe(r, hw):
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

        base = run(None)
        if S["cancel"]:
            return None
        if not base["ok"]:
            S["msg"] = "couldn't optimise %s: %s" % (m["name"], base["why"])
            return None
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
        again = run(None)
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
