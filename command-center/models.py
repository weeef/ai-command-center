"""Model library for the command center: finds every .gguf in models\\, reads its metadata, sizes it for this PC,
builds the llama-server command line, and downloads new models from Hugging Face.

Anyone can drop a .gguf into models\\ (any subfolder) and it shows up in the picker. A vision projector
(mmproj*.gguf) in the same folder turns on image input. Split files (-00001-of-0000N.gguf) count as one model.

Tuning:
  * The Qwen3.6-35B files this repo ships with use the measured settings in llm-config.bat (written by setup for
    your GPU), exactly like start-llm.bat.
  * Any other model uses llama.cpp's own --fit: it measures the model and your free VRAM at load time and puts as
    much as fits on the GPU (all of it when it fits; for MoE models the expert weights spill to RAM first, which
    keeps them fast). Nothing to configure.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS_DIR = os.path.join(ROOT, "models")
LLAMA_EXE = os.path.join(ROOT, "llama.cpp", "llama-server.exe")
KEYFILE = os.path.join(ROOT, "api-key.txt")
LOG = os.path.join(HERE, "llama.log")
NOWIN = 0x08000000
GiB = 1024 ** 3

CTX_CHOICES = [16384, 32768, 65536, 131072, 262144, 524288, 1048576]
DEFAULT_CTX = 131072

# The two launcher keys keep working (slash commands, saved choices). Since 2026-11-14 they mean Qwen3.8 27B.
Q36_STD = "Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
Q36_UNC = "Qwen3.6-35B-A3B-uncensored-heretic-GGUF/Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf"
Q38_STD = "Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf"
Q38_UNC = "Qwen3.8-27B-Uncensored-MTP-GGUF/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-IQ3_XS.gguf"
ALIASES = {"standard": Q38_STD, "uncensored": Q38_UNC}
FALLBACK = {"standard": Q36_STD, "uncensored": Q36_UNC}  # installs that only have the Qwen3.6 files (setup.ps1)


def alias_target(a):
    for p in (ALIASES.get(a), FALLBACK.get(a)):
        if p and os.path.exists(os.path.join(MODELS_DIR, *p.split("/"))):
            return p
    return ALIASES.get(a)
# files with measured settings from llm-config.bat: id -> (config variable for --n-cpu-moe, its default)
TUNED = {
    Q36_STD: ("MOE_STD", 23),
    Q36_UNC: ("MOE_UNC", 25),
    "Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf": ("MOE_MTP", 25),
    "Qwen3.6-35B-A3B-uncensored-heretic-MTP-GGUF/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf": ("MOE_MTP_UNC", 27),
}
NICE = {Q36_STD: "Qwen3.6 35B · standard", Q36_UNC: "Qwen3.6 35B · uncensored",
        "Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf": "Qwen3.6 35B · standard · fast text (MTP)",
        "Qwen3.6-35B-A3B-uncensored-heretic-MTP-GGUF/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf": "Qwen3.6 35B · uncensored · fast text (MTP)",
        # Qwen3.8 27B (dense). On a 16 GB card only ~3-bit quants fit fully on the GPU; measured 2026-11-14 on a
        # 5080 at 32K ctx: IQ3_S + MTP 75-77 t/s, without MTP 46 t/s, IQ4_XS split with RAM 12 t/s.
        Q38_STD: "Qwen3.8 27B · standard",
        Q38_UNC: "Qwen3.8 27B · uncensored"}

# ------------------------------------------------------------------ GGUF metadata
_T = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def read_gguf(path):
    """The metadata keys we need (architecture, layers, experts, context, attention shape). Arrays are skipped."""
    out = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return None
        ver, = struct.unpack("<I", f.read(4))
        if ver < 2:
            return None
        _, nkv = struct.unpack("<QQ", f.read(16))

        def rstr():
            n, = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", "replace")

        def skip(t, count=1):
            if t in _T:
                f.seek(struct.calcsize(_T[t]) * count, 1)
            elif t == 8:
                for _ in range(count):
                    n, = struct.unpack("<Q", f.read(8)); f.seek(n, 1)
            elif t == 9:
                for _ in range(count):
                    at, n = struct.unpack("<IQ", f.read(12)); skip(at, n)
            else:
                raise ValueError("bad gguf type %d" % t)

        for _ in range(nkv):
            k = rstr()
            t, = struct.unpack("<I", f.read(4))
            if t in _T:
                out[k], = struct.unpack(_T[t], f.read(struct.calcsize(_T[t])))
            elif t == 8:
                out[k] = rstr()
            elif t == 9:
                at, n = struct.unpack("<IQ", f.read(12))
                if at in _T and n <= 512 and k.endswith(("head_count_kv", "head_count")):
                    vals = struct.unpack("<%d%s" % (n, _T[at][1]), f.read(struct.calcsize(_T[at][1]) * n))
                    out[k] = list(vals)
                else:
                    skip(at, n)
            else:
                raise ValueError("bad gguf type %d" % t)
    return out


_meta_cache = {}


def meta(path):
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_size, st.st_mtime)
    if key not in _meta_cache:
        try:
            _meta_cache[key] = read_gguf(path) or {}
        except Exception:
            _meta_cache[key] = {}
    return _meta_cache[key]


# ------------------------------------------------------------------ hardware
_hw = {"t": 0, "v": None}


def hardware():
    if _hw["v"] and time.time() - _hw["t"] < 300:
        return _hw["v"]
    h = {"gpu": None, "vram_gb": 0.0, "ram_gb": 0.0, "cores": os.cpu_count() or 4}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=NOWIN).stdout.strip().splitlines()
        if out:
            name, mem = [x.strip() for x in out[0].split(",")[:2]]
            h["gpu"], h["vram_gb"] = name, round(float(mem) / 1024, 1)
    except Exception:
        pass
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                        ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
        m = MS(); m.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        h["ram_gb"] = round(m.total / GiB, 1)
    except Exception:
        pass
    try:  # physical cores: llama.cpp is fastest with one thread per core
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"],
                             capture_output=True, text=True, timeout=15, creationflags=NOWIN).stdout.strip()
        h["cores"] = int(out) or h["cores"]
    except Exception:
        pass
    _hw.update(t=time.time(), v=h)
    return h


def config():
    """llm-config.bat (written by setup.ps1 for this GPU) as a dict, with start-llm.bat's defaults."""
    c = {"MOE_STD": "23", "MOE_UNC": "25", "MOE_MTP": "25", "MOE_MTP_UNC": "27", "UB": "2048", "NGL": "99",
         "SPEC": "--spec-type ngram-mod --spec-ngram-mod-n-min 16 --spec-ngram-mod-n-max 256 --spec-ngram-mod-n-match 16",
         "THREADS": "", "CACHE_RAM": "16384"}
    try:
        with open(os.path.join(ROOT, "llm-config.bat"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*set\s+"?(\w+)=([^"]*)"?\s*$', line, re.I)
                if m:
                    c[m.group(1).upper()] = m.group(2)
    except OSError:
        pass
    return c


# ------------------------------------------------------------------ the library
def _rel(p):
    return os.path.relpath(p, MODELS_DIR).replace("\\", "/")


def _pretty(fname):
    n = re.sub(r"\.gguf$", "", fname, flags=re.I)
    n = re.sub(r"-0*1-of-\d+$", "", n)
    return n.replace("_", " ")


def _quant(fname):
    m = re.search(r"(IQ\d_[A-Z]+|Q\d_K_[SMLX]L?|Q\d_K|Q\d_\d|Q\d_K_XL|BF16|F16|F32|MXFP4)", fname, re.I)
    return m.group(1).upper() if m else ""


def scan():
    """Every runnable .gguf under models\\ as a dict, sorted by name."""
    out = []
    if not os.path.isdir(MODELS_DIR):
        return out
    for d, _, files in os.walk(MODELS_DIR):
        ggufs = [f for f in files if f.lower().endswith(".gguf")]
        projs = sorted(f for f in ggufs if f.lower().startswith("mmproj"))
        for f in ggufs:
            low = f.lower()
            if low.startswith("mmproj") or ".part" in low:
                continue
            split = re.search(r"-(\d+)-of-(\d+)\.gguf$", low)
            if split and int(split.group(1)) != 1:
                continue
            path = os.path.join(d, f)
            md = meta(path)
            arch = md.get("general.architecture", "")
            if not arch or md.get(arch + ".pooling_type") is not None or "embed" in low:
                continue  # embedding / reranker models are not chat models
            size = os.path.getsize(path)
            if split:
                for g in ggufs:
                    if g != f and g.lower().startswith(low[:split.start()]) and re.search(r"-\d+-of-\d+\.gguf$", g.lower()):
                        size += os.path.getsize(os.path.join(d, g))
            mid = _rel(path)
            alias = next((a for a in ALIASES if alias_target(a) == mid), None)
            out.append({
                "id": mid, "alias": alias, "file": f, "folder": _rel(d) if d != MODELS_DIR else "",
                "name": NICE.get(mid) or md.get("general.name") or _pretty(f),
                "variant": _pretty(f), "quant": _quant(f), "arch": arch,
                "size_gb": round(size / GiB, 1),
                "params": md.get("general.size_label", ""),
                "layers": md.get(arch + ".block_count") or 0,
                "experts": md.get(arch + ".expert_count") or 0,
                "experts_used": md.get(arch + ".expert_used_count") or 0,
                "ctx_train": md.get(arch + ".context_length") or 0,
                "mtp": "mtp" in low or bool(md.get(arch + ".nextn_predict_layers")),
                "vision": bool(projs), "mmproj": os.path.join(d, projs[0]) if projs else None,
                "tuned": mid in TUNED,
                "_path": path, "_meta": md,
            })
    # fine-tunes often ship without a vision projector: borrow one from a folder whose model has the same shape
    projs = {(m["arch"], m["_meta"].get(m["arch"] + ".embedding_length"), m["layers"]): m["mmproj"] for m in out if m["mmproj"]}
    for m in out:
        if not m["mmproj"]:
            m["mmproj"] = projs.get((m["arch"], m["_meta"].get(m["arch"] + ".embedding_length"), m["layers"]))
            m["vision"] = bool(m["mmproj"])
    out.sort(key=lambda m: (m["alias"] is None, m["mtp"], m["name"].lower(), m["variant"].lower()))
    return out


def find(key):
    """Model by id, alias, file name, or a unique part of the name."""
    if not key:
        return None
    lib = scan()
    key = alias_target(key) if key in ALIASES else key
    for m in lib:
        if key in (m["id"], m["file"]):
            return m
    k = key.lower()
    hits = [m for m in lib if k in m["id"].lower() or k in m["name"].lower()]
    return hits[0] if len(hits) == 1 else None


def from_path(path):
    """The library id of a running model's file path (from llama-server /props)."""
    if not path:
        return None
    try:
        rel = _rel(os.path.abspath(path))
    except ValueError:
        return None
    return None if rel.startswith("..") else rel


# ------------------------------------------------------------------ sizing
def kv_gb(m, ctx):
    """KV cache size at q8_0 (what we run). Hybrid models (only every Nth layer has attention) cost less."""
    md, a = m["_meta"], m["arch"]
    layers = m["layers"] or 32
    heads = md.get(a + ".attention.head_count") or 32
    kvh = md.get(a + ".attention.head_count_kv") or heads
    if isinstance(heads, list):
        heads = max(heads) or 32
    if isinstance(kvh, list):
        attn = sum(1 for x in kvh if x) or layers
        kvh = max(kvh) or 8
    else:
        every = md.get(a + ".full_attention_interval")
        attn = layers // every if every else layers
    emb = md.get(a + ".embedding_length") or 4096
    kl = md.get(a + ".attention.key_length") or emb // heads
    vl = md.get(a + ".attention.value_length") or kl
    return attn * kvh * (kl + vl) * ctx * 1.0625 / GiB


def plan(m, ctx, hw=None):
    """How this model will run here, for the picker: fit = gpu | split | ram | cpu | too-big."""
    hw = hw or hardware()
    vram, ram = hw["vram_gb"], hw["ram_gb"]
    w, kv = m["size_gb"], kv_gb(m, ctx)
    compute = 1.6 if vram >= 12 else 0.6
    need = w + kv + compute
    budget = max(0.0, vram - 1.5)  # Windows, the browser and the desktop keep ~1.5 GB
    p = {"weights_gb": round(w, 1), "kv_gb": round(kv, 1), "need_gb": round(need, 1), "budget_gb": round(budget, 1)}
    if not vram:
        p.update(fit="cpu", note="no NVIDIA GPU: runs on the CPU, slowly")
    elif need <= budget:
        p.update(fit="gpu", note="fits entirely on the GPU: fastest")
    elif need > budget + ram - 6:
        p.update(fit="too-big", note=f"needs ~{need:.0f} GB but this PC has {vram:.0f} GB VRAM + {ram:.0f} GB RAM")
    elif m["experts"]:
        per = w * 0.92 / max(1, m["layers"])
        n = min(m["layers"], int((need - budget) / per) + 1)
        p.update(fit="split", moe_ram_layers=n,
                 note=f"MoE: experts of ~{n} of {m['layers']} layers live in RAM; still fast, since each token only uses {m['experts_used'] or 'a few'} of {m['experts']} experts")
    else:
        on = max(0.0, min(1.0, (budget - kv - compute) / w))
        p.update(fit="ram", gpu_share=round(on, 2),
                 note=f"dense model, ~{on:.0%} on the GPU and the rest in RAM: works, but much slower. A smaller quant fits better")
    return p


def ctx_choices(m):
    top = m["ctx_train"] or DEFAULT_CTX
    return [c for c in CTX_CHOICES if c <= max(top, CTX_CHOICES[0])]


def default_ctx(m, hw=None):
    """The biggest context up to 128K that still fits on the GPU; never more than the model was trained for."""
    ch = ctx_choices(m)
    best = ch[0]
    for c in ch:
        if c > DEFAULT_CTX:
            break
        if plan(m, c, hw)["fit"] == "gpu" or c <= 32768:
            best = c
    return best if plan(m, best, hw)["fit"] == "gpu" else min(DEFAULT_CTX, ch[-1])


def mtp_on(m, mode=None):
    """MTP models with a vision projector can run either way: "fast" (MTP, text only) or "images" (no MTP)."""
    return bool(m["mtp"]) and not (mode == "images" and m["mmproj"])


def describe(m, ctx=None, hw=None, mode=None):
    ctx = ctx or default_ctx(m, hw)
    d = {k: v for k, v in m.items() if not k.startswith("_") and k != "mmproj"}
    # what it will actually run with: MTP and images can't be on together (llama.cpp)
    d.update(both=bool(m["mtp"] and m["mmproj"]), mode="fast" if mtp_on(m, mode) else "images" if m["vision"] else "text",
             vision=bool(m["vision"]) and not mtp_on(m, mode), mtp=mtp_on(m, mode), mtp_capable=bool(m["mtp"]))
    d.update(ctx=ctx, ctx_choices=ctx_choices(m), plan=plan(m, ctx, hw),
             tuning="measured on this setup (llm-config.bat)" if m["tuned"] else "auto-fit to this PC by llama.cpp")
    return d


def command(m, ctx, mode=None):
    """llama-server command line for this model at this context, sized for this PC."""
    c, hw = config(), hardware()
    threads = c.get("THREADS") or str(hw["cores"])
    args = [LLAMA_EXE, "-m", m["_path"]]
    spec = c["SPEC"].split()
    vision = m["vision"]
    if mtp_on(m, mode):
        # multi-token prediction drafts 2 tokens per pass (+~15%); llama.cpp can't combine it with --mmproj yet
        spec, vision = ["--spec-type", "draft-mtp,ngram-mod", "--spec-draft-n-max", "2"], False
    if vision:
        args += ["--mmproj", m["mmproj"], "--no-mmproj-offload"]
    args += ["--host", "127.0.0.1", "--port", "8090", "--api-key-file", KEYFILE]
    if m["tuned"]:
        var, dflt = TUNED[m["id"]]
        moe = int(c.get(var) or dflt) + (3 if ctx > DEFAULT_CTX else 0)  # bigger KV cache: 3 more layers to RAM
        args += ["--fit", "off", "-ngl", c.get("NGL") or "99", "--n-cpu-moe", str(min(moe, m["layers"] or moe))]
    elif hw["vram_gb"]:
        # A model that fits entirely on the GPU only gets there with a small reserve: with 1536 MiB, llama.cpp's
        # fit moved a few layers of Qwen3.8 27B to RAM next to the desktop's ~3-4 GB and generation fell 77 -> 49 t/s.
        target = "512" if plan(m, ctx, hw)["fit"] == "gpu" else "1536"
        args += ["--fit", "on", "--fit-target", target]
    else:
        args += ["--fit", "off", "-ngl", "0"]
    ub = c.get("UB") or ("2048" if hw["vram_gb"] >= 12 else "512")
    args += ["-c", str(ctx), "-np", "1", "--cache-ram", c.get("CACHE_RAM") or "8192",
             "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
             "-t", threads, "-b", "2048", "-ub", ub] + spec + ["--metrics"]
    if plan(m, ctx, hw)["fit"] != "gpu":
        args += ["--load-mode", "none"]  # weights that stay on the CPU: read into RAM up front, faster prompts than mmap
    return args


def launch(m, ctx, mode=None):
    """Start llama-server in the background (no console window); its output goes to command-center\\llama.log."""
    if not os.path.exists(LLAMA_EXE):
        raise RuntimeError("llama.cpp isn't installed: run setup.bat")
    if not os.path.exists(KEYFILE):
        raise RuntimeError("api-key.txt is missing: run setup.bat")
    log = open(LOG, "w", encoding="utf-8", errors="replace")
    log.write("# " + " ".join('"%s"' % a if " " in a else a for a in command(m, ctx, mode)) + "\n"); log.flush()
    p = subprocess.Popen(command(m, ctx, mode), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=NOWIN | 0x00000200)  # CREATE_NEW_PROCESS_GROUP: outlives the command center
    log.close()
    return p


def log_tail(n=12):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f.readlines()[1:] if l.strip()]
    except OSError:
        return ""
    err = [l for l in lines if re.search(r"error|failed|out of memory|unable|cannot", l, re.I)]
    return "\n".join((err or lines)[-n:])


# ------------------------------------------------------------------ Hugging Face
HF = "https://huggingface.co"
downloads = {}  # id -> {repo, file, done, total, state, error}
_dl_lock = threading.Lock()


def hf_repo(text):
    """'owner/repo' from a Hugging Face link or a bare owner/repo, plus a file if the link points at one."""
    t = (text or "").strip()
    m = re.match(r"^(?:https?://(?:www\.)?huggingface\.co/)?([\w.-]+)/([\w.-]+)(?:/(?:blob|resolve|tree)/[^/]+/(.+?))?(?:\?.*)?/?$", t)
    if not m or m.group(1) in ("api", "datasets", "spaces"):
        return None, None
    return m.group(1) + "/" + m.group(2), (urllib.parse.unquote(m.group(3)) if m.group(3) else None)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "command-center"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def hf_files(text):
    """The .gguf files in a Hugging Face repo, grouped into downloadable choices with a fit estimate for this PC."""
    repo, file = hf_repo(text)
    if not repo:
        raise ValueError("paste a Hugging Face link like huggingface.co/unsloth/Qwen3-8B-GGUF")
    items = _get_json(f"{HF}/api/models/{repo}/tree/main?recursive=1")
    files = [i for i in items if i.get("type") == "file" and i["path"].lower().endswith(".gguf")]
    if not files:
        raise ValueError(f"{repo} has no .gguf files (llama.cpp needs GGUF; look for a repo ending in -GGUF)")
    hw = hardware()
    projs = [f["path"] for f in files if os.path.basename(f["path"]).lower().startswith("mmproj")]
    groups = {}
    for f in files:
        p = f["path"]; b = os.path.basename(p)
        if b.lower().startswith("mmproj"):
            continue
        s = re.search(r"-(\d+)-of-(\d+)\.gguf$", b, re.I)
        key = p[:s.start() + len(p) - len(b)] if s else p
        g = groups.setdefault(key, {"name": _pretty(b), "quant": _quant(b), "files": [], "size": 0})
        g["files"].append(p); g["size"] += (f.get("lfs") or {}).get("size") or f.get("size") or 0
    have = {m["id"] for m in scan()}
    out = []
    for key, g in groups.items():
        gb = g["size"] / GiB
        need = gb + 2.5  # weights + a typical 32-64K context and compute buffers
        fit = ("cpu" if not hw["vram_gb"] else "gpu" if need <= hw["vram_gb"] - 1.5
               else "too-big" if need > hw["vram_gb"] + hw["ram_gb"] - 8 else "split")
        dest = repo.split("/")[1] + "/" + os.path.basename(sorted(g["files"])[0])
        out.append({"key": key, "name": g["name"], "quant": g["quant"], "files": sorted(g["files"]), "size_gb": round(gb, 1),
                    "fit": fit, "have": dest in have, "picked": bool(file and file in g["files"])})
    order = {"gpu": 0, "split": 1, "cpu": 2, "too-big": 3}
    out.sort(key=lambda x: (order[x["fit"]], -x["size_gb"]))
    # best pick: the biggest quant that fits on the GPU, else the biggest that runs at all
    best = next((x for x in out if x["fit"] == "gpu"), None) or next((x for x in out if x["fit"] != "too-big"), None)
    if best:
        best["best"] = True
    return {"repo": repo, "files": out, "mmproj": sorted(projs, key=lambda p: ("f16" not in p.lower(), p))[:1],
            "free_gb": round(shutil.disk_usage(ROOT).free / GiB)}


def _safe_dest(repo, path):
    folder = re.sub(r"[^\w.-]", "_", repo.split("/")[1])
    name = re.sub(r"[^\w.-]", "_", os.path.basename(path))
    if not name.lower().endswith(".gguf"):
        raise ValueError("not a .gguf file")
    d = os.path.join(MODELS_DIR, folder)
    return d, os.path.join(d, name)


def download(repo, files):
    """Download these repo files into models\\<repo name>\\ in the background (resumes a partial .part file)."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise ValueError("bad repo")
    jobs = []
    for p in files:
        if not isinstance(p, str) or ".." in p or p.startswith("/"):
            raise ValueError("bad file name")
        d, dest = _safe_dest(repo, p)
        jobs.append((p, d, dest))
    did = str(int(time.time() * 1000))
    downloads[did] = {"repo": repo, "file": os.path.basename(files[0]), "done": 0, "total": 0, "state": "starting", "error": ""}

    def run():
        st = downloads[did]
        try:
            for p, d, dest in jobs:
                if os.path.exists(dest):
                    continue
                os.makedirs(d, exist_ok=True)
                url = f"{HF}/{repo}/resolve/main/" + urllib.parse.quote(p)
                part = dest + ".part"
                have = os.path.getsize(part) if os.path.exists(part) else 0
                req = urllib.request.Request(url, headers={"User-Agent": "command-center", **({"Range": f"bytes={have}-"} if have else {})})
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status != 206:
                        have = 0
                    total = have + int(r.headers.get("Content-Length") or 0)
                    st.update(state="downloading", file=os.path.basename(p))
                    st["total"] += total; st["done"] += have
                    with open(part, "ab" if have else "wb") as f:
                        while True:
                            if st["state"] == "cancelled":
                                return
                            b = r.read(1 << 20)
                            if not b:
                                break
                            f.write(b); st["done"] += len(b)
                os.replace(part, dest)
            st["state"] = "done"
        except Exception as e:
            if st["state"] != "cancelled":
                st.update(state="error", error=str(e)[:300])

    threading.Thread(target=run, daemon=True).start()
    return did


def open_folder():
    os.makedirs(MODELS_DIR, exist_ok=True)
    subprocess.Popen(["explorer", MODELS_DIR], creationflags=NOWIN)
