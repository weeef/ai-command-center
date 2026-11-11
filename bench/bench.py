"""Benchmark harness for the llama-server settings in start-llm.bat (Python stdlib only).

Starts its own llama-server on a spare port (8190, so the command center never sees it), runs three fixed tasks,
samples peak VRAM with nvidia-smi, appends one JSON line to benchmarks/results-v2.jsonl and prints a table row.

  read     a ~12k-token prompt (a frozen copy of this repo's server.py from git) + a 300-token summary
           -> prompt tok/s ("pp", how fast long prompts are read) and writing tok/s with a long context
  rewrite  Cline-style "rewrite this whole file with one change" on a ~3k-token file
           -> tok/s when most of the output repeats the input (what ngram speculation speeds up)
  chat     a short question, 400 tokens of free writing -> plain writing tok/s

The prompts come from a fixed commit, so numbers stay comparable while the code changes.

Examples (run from the repo root, with the model in 1:llm turned OFF: only one fits in VRAM):
  python bench/bench.py --name default
  python bench/bench.py --name moe22 --moe 22
  python bench/bench.py --name vcache --extra "-t 8 --cpu-range 0-7 --cpu-strict 1"
  python bench/bench.py --name mtp --model mtp --moe 25 --spec "--spec-type draft-mtp,ngram-mod --spec-draft-n-max 2" --no-vision
  python bench/bench.py --name unc --model unc --moe 25
"""
import argparse
import http.client
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8190
FROZEN = "53b6f77"  # the commit the prompt texts are read from
MODELS = {
    "std": r"models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
    "unc": r"models\Qwen3.6-35B-A3B-uncensored-heretic-GGUF\Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf",
    "mtp": r"models\Qwen3.6-35B-A3B-MTP-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
    "mtpu": r"models\Qwen3.6-35B-A3B-uncensored-heretic-MTP-GGUF"
            r"\Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf",
}
MMPROJ = r"models\Qwen3.6-35B-A3B-GGUF\mmproj-F32.gguf"


def frozen(path):
    return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (FROZEN, path)], capture_output=True, check=True,
                          encoding="utf-8").stdout


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def vram_used():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 0


def post(path, body, timeout=900):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        if r.status != 200:
            raise RuntimeError("HTTP %d: %s" % (r.status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw)
    finally:
        c.close()


def chat(system, user, max_tokens):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    j = post("/v1/chat/completions", {
        "messages": msgs, "max_tokens": max_tokens, "temperature": 0, "seed": 42, "stream": False,
        "cache_prompt": False, "chat_template_kwargs": {"enable_thinking": False}})
    return j.get("timings") or {}, (j["choices"][0]["message"].get("content") or "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="std", help="std | unc | mtp | mtpu | a path to a .gguf")
    ap.add_argument("--llama", default="llama.cpp", help="llama.cpp folder (e.g. llama.cpp-b11798)")
    ap.add_argument("--moe", type=int, default=23)
    ap.add_argument("--ctx", type=int, default=131072)
    ap.add_argument("--ub", type=int, default=2048)
    ap.add_argument("--b", type=int, default=2048)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--ctk", default="q8_0", help="KV cache type for both K and V")
    ap.add_argument("--spec", default="--spec-type ngram-mod")
    ap.add_argument("--no-vision", action="store_true", help="don't load the mmproj (MTP can't be combined with it)")
    ap.add_argument("--extra", default="", help="more llama-server flags, appended as-is")
    ap.add_argument("--tasks", default="read,rewrite,chat")
    ap.add_argument("--force", action="store_true", help="run even if a model is already up on :8090")
    a = ap.parse_args()

    if port_open(8090) and not a.force:
        sys.exit("a model is running on :8090; turn it off first (only one fits in VRAM) or pass --force")
    if port_open(PORT):
        sys.exit("port %d is busy (an earlier benchmark server?)" % PORT)
    model = MODELS.get(a.model, a.model)
    model = model if os.path.isabs(model) else os.path.join(ROOT, model)
    exe = os.path.join(ROOT, a.llama, "llama-server.exe")
    for p in (model, exe):
        if not os.path.exists(p):
            sys.exit("not found: " + p)
    out_dir = os.path.join(ROOT, "benchmarks")
    os.makedirs(out_dir, exist_ok=True)

    args = [exe, "-m", model, "--host", "127.0.0.1", "--port", str(PORT), "--fit", "off", "-ngl", "99",
            "--n-cpu-moe", str(a.moe), "-c", str(a.ctx), "-np", "1", "-fa", "on", "-ctk", a.ctk, "-ctv", a.ctk,
            "--load-mode", "none", "-t", str(a.threads), "-b", str(a.b), "-ub", str(a.ub), "--no-ui"]
    mm = os.path.join(ROOT, MMPROJ)
    if not a.no_vision and os.path.exists(mm):
        args += ["--mmproj", mm, "--no-mmproj-offload"]
    args += shlex.split(a.spec) + shlex.split(a.extra)
    flags = " ".join(args[3:])

    rec = {"name": a.name, "when": time.strftime("%Y-%m-%d %H:%M"), "llama": a.llama, "model": os.path.basename(model),
           "flags": flags, "vram_idle_mib": vram_used(), "status": "?"}
    peak = {"v": 0, "stop": False}

    def sample():
        while not peak["stop"]:
            peak["v"] = max(peak["v"], vram_used())
            time.sleep(0.4)

    log = open(os.path.join(out_dir, "v2-%s.log" % a.name), "wb")
    t0 = time.time()
    srv = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    threading.Thread(target=sample, daemon=True).start()
    try:
        while time.time() - t0 < 600:
            if srv.poll() is not None:
                raise RuntimeError("llama-server exited with %s (see benchmarks/v2-%s.log)" % (srv.returncode, a.name))
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
                c.request("GET", "/health")
                ok = c.getresponse().status == 200
                c.close()
                if ok:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("load timeout")
        rec["load_s"] = round(time.time() - t0, 1)
        chat(None, "Say hi.", 8)  # warm-up: first request pays one-off allocation costs
        tasks = a.tasks.split(",")
        if "read" in tasks:
            src = frozen("command-center/server.py")[:46000]
            t, _ = chat("You are a senior engineer reviewing code.",
                        "Here is a Python file.\n\n```python\n%s\n```\n\nSummarise what it does in about 200 words." % src, 300)
            rec.update(read_tokens=t.get("prompt_n"), read_pp=round(t.get("prompt_per_second", 0), 1),
                       read_tg=round(t.get("predicted_per_second", 0), 1))
        if "rewrite" in tasks:
            src = frozen("command-center/tokens.py")
            t, text = chat("You are a coding agent. When asked to change a file, reply with the complete new file in "
                           "one ```python code block and nothing else.",
                           "Rename the constant POLL to POLL_SECONDS everywhere in this file. Change nothing else.\n\n"
                           "```python\n%s\n```" % src, 4500)
            rec.update(rw_tokens=t.get("predicted_n"), rw_tg=round(t.get("predicted_per_second", 0), 1),
                       rw_ok="POLL_SECONDS" in text and "def recent_log" in text,
                       rw_draft=t.get("draft_n"), rw_draft_ok=t.get("draft_n_accepted"))
        if "chat" in tasks:
            t, _ = chat(None, "Explain how a heat pump works and when it beats a gas boiler, in about 300 words.", 400)
            rec.update(chat_tg=round(t.get("predicted_per_second", 0), 1), chat_draft=t.get("draft_n"),
                       chat_draft_ok=t.get("draft_n_accepted"))
        rec["status"] = "ok"
    except Exception as e:  # a failed config (e.g. out of VRAM) is a result too
        rec["status"] = "error: %s" % e
    finally:
        peak["stop"] = True
        srv.terminate()
        try:
            srv.wait(30)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
    rec["vram_peak_mib"] = peak["v"]
    with open(os.path.join(out_dir, "results-v2.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k not in ("flags", "model", "when", "llama")}))
    time.sleep(3)  # let the driver release VRAM before the next run


if __name__ == "__main__":
    main()
