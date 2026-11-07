"""Token meter for both models: tokens in/out since each model was turned on, a lifetime total, and a session log.

Exact when the model server reports llama.cpp's prometheus counters (/metrics, needs --metrics):
  llamacpp:prompt_tokens_total   - prompt tokens processed (cached prompt tokens are not counted again)
  llamacpp:tokens_predicted_total - tokens generated
Those counters live in llama-server itself, so they are right even if the command center was closed for a while.
Otherwise it falls back to sampling /slots once a second and adding up each request's generated tokens. That only
counts output, only while the command center is open, and can miss the tail of a request, so the UI marks it "≈".

State lives in tokens.json (current sessions + lifetime) and finished sessions are appended to tokens-log.jsonl.
"""
import http.client
import json
import os
import threading
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "tokens.json")
LOG_FILE = os.path.join(HERE, "tokens-log.jsonl")
POLL = 1.0
_lock = threading.Lock()


def _get(addr, path, key=None, timeout=1.5):
    """(status, text) or (None, None) when nothing is listening."""
    c = http.client.HTTPConnection(*addr, timeout=timeout)
    try:
        c.request("GET", path, headers={"Authorization": "Bearer " + key} if key else {})
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    except OSError:
        return None, None
    finally:
        c.close()


def _parse_metrics(text):
    vals = {}
    for line in (text or "").splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total"):
            try:
                vals[parts[0]] = int(float(parts[1]))
            except ValueError:
                pass
    if len(vals) < 2:
        return None
    return vals["llamacpp:prompt_tokens_total"], vals["llamacpp:tokens_predicted_total"]


def _slot_counts(text):
    """{slot id: (task id, generated so far)} from a /slots answer."""
    try:
        slots = json.loads(text)
    except ValueError:
        return None
    if not isinstance(slots, list):
        return None
    out = {}
    for s in slots:
        if not isinstance(s, dict):
            continue
        nt = s.get("next_token")
        if isinstance(nt, list):
            nt = nt[0] if nt else {}
        n = (nt or {}).get("n_decoded", s.get("n_decoded", 0)) or 0
        out[s.get("id", 0)] = (s.get("id_task"), int(n))
    return out


def _blank():
    return {"on": False, "since": None, "model": None, "in": 0, "out": 0, "exact": True,
            "raw": None, "life_in": 0, "life_out": 0, "slots": {}}


class Meter:
    def __init__(self, name, probe):
        self.name, self.probe = name, probe   # probe() -> (state, model, metrics|None, slots|None)
        self.s = _blank()
        self.rate = {"t": 0, "out": 0, "tps": 0.0}

    # -------- session bookkeeping
    def _end(self):
        s = self.s
        if s["on"] and (s["in"] or s["out"]):
            entry = {"machine": self.name, "model": s["model"], "start": s["since"], "end": time.time(),
                     "in": s["in"], "out": s["out"], "exact": s["exact"]}
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        s.update(on=False, since=None, model=None, raw=None, slots={}, exact=True)
        s["in"] = s["out"] = 0

    def _add(self, d_in, d_out):
        s = self.s
        s["in"] += d_in
        s["out"] += d_out
        s["life_in"] += d_in
        s["life_out"] += d_out

    def tick(self):
        state, model, metrics, slots = self.probe()
        s = self.s
        if state == "OFF":
            if s["on"]:
                self._end()
            return
        if state not in ("ON", "ASLEEP", "LOADING"):
            return
        if not s["on"]:
            s.update(on=True, since=time.time(), model=model)
        if model:
            s["model"] = model
        if metrics:
            p, g = metrics
            if s["raw"] is None:
                self._add(p, g)            # a fresh server: all it has counted is this session's
            elif p < s["raw"][0] or g < s["raw"][1]:
                self._add(p, g)            # restarted or woke from sleep inside one "on" stretch
            else:
                self._add(p - s["raw"][0], g - s["raw"][1])
            s["raw"] = [p, g]
        elif slots is not None:
            s["exact"] = False
            seen = s["slots"]
            for sid, (task, n) in slots.items():
                k = str(sid)
                last = seen.get(k)
                if last and last[0] == task:
                    self._add(0, max(0, n - last[1]))
                elif last is not None:
                    self._add(0, n)        # a new request started since the last sample
                seen[k] = [task, n]
        # generation speed over the last few seconds, for the "burning" readout
        now = time.time()
        if now - self.rate["t"] >= 3:
            if self.rate["t"]:
                self.rate["tps"] = (s["out"] - self.rate["out"]) / (now - self.rate["t"]) if s["out"] >= self.rate["out"] else 0.0
            self.rate.update(t=now, out=s["out"])

    def view(self):
        s = self.s
        return {"on": s["on"], "since": s["since"], "model": s["model"], "in": s["in"], "out": s["out"],
                "exact": s["exact"], "life_in": s["life_in"], "life_out": s["life_out"],
                "tps": round(self.rate["tps"], 1)}


class TokenWatch:
    def __init__(self, pc_addr, pair_addr, key_fn):
        self.pc_addr, self.pair_addr, self.key_fn = pc_addr, pair_addr, key_fn
        self.meters = {"pc": Meter("pc", self._probe_pc), "laptop": Meter("laptop", self._probe_laptop)}
        self._load()

    # -------- probes (cheap: one or two local requests each)
    def _probe_pc(self):
        key = self.key_fn()
        code, text = _get(self.pc_addr, "/metrics", key)
        if code is None:
            return "OFF", None, None, None
        if code == 503:
            return "LOADING", None, None, None
        if code == 200:
            return "ON", self._pc_model(key), _parse_metrics(text), None
        # metrics disabled (started without --metrics): fall back to slots
        code, text = _get(self.pc_addr, "/slots", key)
        if code == 503:
            return "LOADING", None, None, None
        return "ON", self._pc_model(key), None, _slot_counts(text) if code == 200 else None

    _pc_model_cache = {"t": 0, "v": None}

    def _pc_model(self, key):
        c = self._pc_model_cache
        if time.time() - c["t"] > 15:
            code, text = _get(self.pc_addr, "/props", key)
            try:
                p = os.path.basename(json.loads(text).get("model_path", "")) if code == 200 else None
            except (ValueError, AttributeError):
                p = None
            c.update(t=time.time(), v=p)
        return c["v"]

    def _probe_laptop(self):
        code, text = _get(self.pair_addr, "/models")
        if code != 200:
            return "OFF", None, None, None
        try:
            data = json.loads(text).get("data") or []
        except (ValueError, AttributeError):
            data = []
        m = next((x for x in data if (x.get("status") or {}).get("value") in ("loaded", "sleeping", "loading")), None)
        if not m:
            return "OFF", None, None, None
        st = (m.get("status") or {}).get("value")
        mid = m.get("id")
        if st != "loaded":   # never poke a sleeping/loading model: it would wake it
            return ("ASLEEP" if st == "sleeping" else "LOADING"), mid, None, None
        q = "?model=" + quote(mid, safe="")
        code, text = _get(self.pair_addr, "/metrics" + q)
        if code == 200 and _parse_metrics(text):
            return "ON", mid, _parse_metrics(text), None
        code, text = _get(self.pair_addr, "/slots" + q)
        # PAIR answers /slots with 400 while the laptop is generating; skip that sample
        return "ON", mid, None, _slot_counts(text) if code == 200 else None

    # -------- persistence
    def _load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for k, m in self.meters.items():
            d = saved.get(k) or {}
            for f_ in ("on", "since", "model", "in", "out", "exact", "raw", "life_in", "life_out"):
                if f_ in d:
                    m.s[f_] = d[f_]
            # "raw" is the server's counters at the last save: if the server kept running while the command
            # center was closed, the next reading adds exactly what it did in the meantime.

    def _save(self):
        out = {k: {f: m.s[f] for f in ("on", "since", "model", "in", "out", "exact", "raw", "life_in", "life_out")}
               for k, m in self.meters.items()}
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def run(self):
        last_save, last = 0, None
        while True:
            for m in self.meters.values():
                try:
                    with _lock:
                        m.tick()
                except Exception:
                    pass
            snap = json.dumps({k: m.view() for k, m in self.meters.items()}, sort_keys=True)
            if snap != last and time.time() - last_save > 5:
                with _lock:
                    self._save()
                last_save, last = time.time(), snap
            time.sleep(POLL)

    def view(self):
        with _lock:
            return {k: m.view() for k, m in self.meters.items()}

    def recent_log(self, n=20):
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for l in reversed(lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out
