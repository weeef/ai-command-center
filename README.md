# AI command center

Run big open AI models **on your own Windows PC**, privately, with a polished control panel on top:
chat with saved history, a long-term **memory** shared by every model, a coding agent (VS Code + Cline) that uses the
same memory, and access from your **phone**. Nothing leaves your machine unless you turn on phone access, and even
then only your own devices can reach it.

It's built on [llama.cpp](https://github.com/ggml-org/llama.cpp) and a small Python server with no required
dependencies. The default models are Qwen3.6-35B-A3B (a fast "mixture of experts" model that runs well on a 12-16 GB
graphics card plus 32-64 GB of RAM).

---

## Quick start

**You need:** Windows 10/11, an NVIDIA graphics card (8 GB+, 12-16 GB recommended), 32 GB+ RAM (64 GB recommended),
~50 GB free disk, and [Python 3.10+](https://www.python.org/downloads/) (`winget install Python.Python.3.13`).

1. **Get the code**: `git clone https://github.com/weeef/ai-command-center` (or download the ZIP from GitHub and
   unzip it). Use a folder on a fast drive with plenty of space.
2. **Run `setup.bat`** in that folder. It:
   - checks your GPU, RAM and Python, installs `numpy`
   - downloads llama.cpp (the official CUDA build) into `llama.cpp\`
   - creates `api-key.txt` (a private key for the local model server)
   - asks which model(s) to download (22-25 GB each) and fetches the 0.6 GB memory-search model
   - writes `llm-config.bat` with settings sized for **your** graphics card
   - puts a **Command Center** shortcut on your desktop
3. **Double-click Command Center.** In **1:llm** press **⏻ on** (the first start takes ~30 s), then type in the chat.

That's it. Everything below is optional.

---

## The tabs

| tab | what it's for |
|---|---|
| **1:llm** | Turn the model on/off, pick *standard* or *uncensored*, choose **run on** *this pc* or *laptop*, and chat. Paste or drop images (vision). Live GPU/RAM/CPU usage, speed of the last reply (pp = reading, tg = writing) and tokens used. The **brain** shows the state: grey = off, nodes lighting up = loading, pulses = thinking. In the *corpus* theme its glowing nodes are your memories. |
| **2:code** | Open projects in VS Code with the Cline coding agent wired to your local model. Optional second "laptop" VS Code (see *Two machines*). |
| **3:web** | Phone access over Tailscale: one button to turn it on, the address to open on your phone, your devices. |
| **4:mem** | Your memories as a **brain**: every memory is a neuron in its topic's region (about you = frontal lobe, preferences = prefrontal, how-tos = motor strip, projects = parietal, people + events = temporal, facts = occipital, other = cerebellum). Bigger = recalled more often, ring = pinned. Synapses join memories with related meaning; signals travel along them and a neuron fires when the model recalls it. Click a neuron to read/edit/pin/forget it; switch to **tree** for a radial tree by topic. Scroll to zoom, drag to move. |
| **5:cline** | Route Cline through the command center so it gets memory too, plus a log of every Cline task (steps, memories used, tokens, cache hit rate). |
| **6:set** | Memory on/off and auto-learning, the memory folder (move it to another drive), import from ChatGPT/Claude, export, the searchable memory list, the theme, and **cloud & web search** (API keys for Claude / ChatGPT / Gemini). |

Keyboard: **Alt+1 … Alt+6** switch tabs. In chat: **Enter** sends, **Shift+Enter** new line, `/help` lists commands
(`/new`, `/chats`, `/remember …`, `/on`, `/off`, `/theme`, …). **history** (top right of the chat) reopens old chats.

---

## Memory

Every model you run (standard, uncensored, the laptop, any model you add later) shares **one** memory.

- **It learns by itself.** After each reply, when the model is idle, it notes durable facts ("prefers short answers",
  "is building X", "sister lives in Denver") as short sentences in 8 topics. You'll see `◉ remembered: …` in the chat.
- **It recalls by itself.** Each message pulls in the relevant memories and snippets of older chats. The reply's
  stats line shows `◉ 3 memories`; click it to see which. Unrelated questions recall nothing.
- **You stay in control.** `/remember <fact>` in chat, or edit / pin (★ = always included) / forget in **4:mem** or
  **6:set**. Turn learning or memory off in **6:set**.
- **It's cheap.** At most ~1.5k tokens per message (usually ~150), placed so the model's prompt cache keeps working:
  a new chat turn only processes the new text.
- **It's portable.** Everything lives in one folder (`memory\` by default): `memory.db` + `chats\`. To use another
  drive: **6:set → location**, type e.g. `E:\ai-memory`, **move here**. It copies, verifies, switches, and keeps the
  old folder as a backup. If the drive is unplugged, memory pauses (chat still works) instead of starting over.
- **Bring your history.** **6:set → import**: pick the `.zip` from ChatGPT (*Settings → Data controls → Export data*)
  or Claude (*Settings → Privacy → Export data*). Old conversations become searchable chats right away, and memories
  are learned from them in the background. Saved memories can be pasted one per line.

How it works: `command-center\memory.py`. SQLite with full-text search plus embedding vectors from a small model
(Qwen3-Embedding-0.6B) that runs in its own llama-server on the **CPU** (port 8091), so it never takes VRAM.

---

## Speed and settings for your GPU

`start-llm.bat` reads `llm-config.bat` (written by setup, never committed). The knobs:

| setting | what it does |
|---|---|
| `MOE_STD`, `MOE_UNC` | how many of the model's expert layers stay in system RAM. **Lower = faster but more VRAM.** Each layer moved to the GPU costs ~0.46 GB (Q4) / ~0.56 GB (Q5). 40 = all in RAM (8 GB cards). |
| `UB` | prompt batch size. 2048 reads long prompts (Cline, long chats) ~2.3x faster than 512 for ~1.5 GB VRAM. |
| `SPEC` | `--spec-type ngram-mod` drafts repeated text, making code rewrites (what Cline does all day) ~2.4x faster. |
| `THREADS`, `CACHE_RAM`, `NGL` | CPU threads, RAM for llama.cpp's prompt cache (MB), GPU layers (0 = CPU only). |

If a model fails to start with an out-of-memory error, raise `MOE_*` by 2. If VRAM has room to spare (watch **usage**
in 1:llm while chatting), lower it by 1-2.

**Measured on the reference PC** (RTX 5080 16 GB, Ryzen 9 9950X3D, 64 GB DDR5-6000, 12k-token prompt; llama.cpp b12290, which was ~2% faster than b11798 on the same settings):

| model | settings | reading a prompt | writing | rewriting code (Cline) | peak VRAM |
|---|---|---|---|---|---|
| standard Q4 | old: `MOE 24, UB 512` | 1,818 tok/s | 77 tok/s | 79 tok/s | 12.6 GB |
| standard Q4 | **new default: `MOE 23, UB 2048, ngram-mod`** | **4,240 tok/s** | ~78 tok/s | **200 tok/s** | 14.2 GB |
| uncensored Q5 | old: `MOE 26, UB 512` | 1,460 tok/s | 69 tok/s | 71 tok/s | 12.9 GB |
| uncensored Q5 | **new default: `MOE 25, UB 2048, ngram-mod`** | **~3,600 tok/s** | ~70 tok/s | **~165 tok/s** | ~14 GB |

**Fast text-only mode** (`start-llm.bat` options 4/5): MTP versions of the models draft 2 tokens ahead. Measured: +15% writing speed at `MOE 23` (but ~15.7 GB peak VRAM) and ~+4% at the safer default `MOE_MTP=25` (14.1 GB). No image input (llama.cpp can't combine MTP with vision yet). Needs the MTP model files (links in `start-llm.bat`).

Also measured: CPU threads (8/12/16) make no difference; every expert layer moved to the GPU adds ~2.5% writing
speed; `UB 2048` costs ~1.5 GB VRAM. Writing speed is limited by system-RAM bandwidth for the experts kept in RAM.

---

## Web search and a stronger cloud model (optional)

Give your local model two tools it can use when it decides they help:

- **web_search**: looks things up on the live web (news, prices, releases, docs) and returns a summary with sources.
- **ask_cloud**: hands a hard question to a much stronger model.

Any one API key is enough: **Claude** (console.anthropic.com), **ChatGPT** (platform.openai.com) or **Gemini**
(aistudio.google.com). Each provider's own built-in web search is used, so there's no separate search account.

1. **6:set → cloud & web search**: pick the provider, paste the key, **save key** (it tests the key and lists the
   models it can use), optionally set the model, then **turn on**.
2. Chat as usual. When the model searches you'll see `🔎 searching the web: "…"` in its reply, then the sources
   (click to expand). `/search <query>` asks it to search explicitly.

Privacy: only the query or question the local model writes is sent; never your conversation or memories. Keys stay in
`command-center\secrets.json` on your PC (never committed, never shown in full in the app). Usage is billed by the
provider. Claude uses the official `anthropic` Python package (setup installs it) with `claude-opus-5-5` by default
and server-side refusal fallbacks enabled; ChatGPT uses the Responses API web search tool; Gemini uses Google Search
grounding. Defaults can be changed per provider in 6:set. Not used by Cline (it has its own tools).

---

## Phone access (optional)

1. Install [Tailscale](https://tailscale.com/download) on the PC and your phone, signed in to the same account.
2. In **3:web**, press **turn on**. Open the address it shows on your phone (Safari: Share → *Add to Home Screen*).

The phone gets the same app in a phone layout: chat, saved chats, memory, model on/off. It can't open VS Code, see the
API key, move the memory or switch phone access off. It works while the command center is open on the PC. Only
devices signed in to your Tailscale account can reach it; it is not on the public internet.

## VS Code + Cline (optional)

Install [VS Code](https://code.visualstudio.com/) and the **Cline** extension. In Cline's settings choose
*OpenAI Compatible*, base URL `http://127.0.0.1:8099/cline/pc/v1` (through the command center, with memory; or
`http://127.0.0.1:8090/v1` directly), any model id, and the key from `api-key.txt`. Or let **5:cline** set it for you
(close VS Code first). Routed Cline needs the command center to be open.

## Two machines (optional)

If a second PC runs NVIDIA's Personal AI Router (PAIR), the command center can use its model: **run on → laptop** in
1:llm. `start-vscode-laptop.bat` opens a separate VS Code whose Cline uses the laptop, and **2:code → open · both**
puts the two agents on separate git worktrees of the same repo so they can work side by side.

## Updates

The command center checks this GitHub repo in the background (shortly after it starts, then every 6 hours). When
there's a newer version, an **↑ update** button appears in the top bar: it lists what changed, and **update now**
installs it and restarts the server (a few seconds; the window reloads itself, your chat comes back, a loaded model
keeps running). **Later** hides it for now, and **skip this version** until something newer is out. **6:set → updates**
has *check now* and an auto-check switch. Nothing installs without you clicking.

- **git clone**: a fast-forward `git pull`. If you've changed files or have commits that aren't on GitHub, it refuses
  and changes nothing.
- **ZIP download**: only the files that differ from GitHub are replaced; the old copies are kept as `*.bak`. Your
  settings, keys, chats, memory and models are never touched (they aren't in the repo).

---

## Picking and adding models

Click the model name next to the **on** button in **1:llm** (or type `/models`). The picker lists every model in
`models\` and tells you, for **your** PC, how each one will run:

- **fits on GPU**: the whole model and its context fit in VRAM. Fastest.
- **GPU + RAM**: a Mixture-of-Experts model whose expert weights partly live in system RAM. Still fast, because
  each token only touches a few experts.
- **mostly RAM · slow**: a dense model too big for the GPU. It works, but pick a smaller quant if you can.
- **too big**: needs more VRAM + RAM than this PC has.

Click a card to use it (if a model is running it switches). Each card has a **context** menu: more context lets the
model read longer chats and files, but costs VRAM; the default is the biggest (up to 128K) that still fits.

**Adding a model:** paste a Hugging Face link into **add a model** (for example `huggingface.co/unsloth/Qwen3-8B-GGUF`).
You get every size in that repo with a fit badge and **★ best for this PC** on the largest one that fits your GPU;
press download. If the repo has a vision projector, tick "include image input" to get image support too.
Or just copy any `.gguf` into `models\` (any subfolder) and press **rescan**. A `mmproj*.gguf` next to a model gives
it image input; split files (`-00001-of-00003.gguf`) count as one model.

**Tuned for your hardware, automatically:** the bundled Qwen3.6-35B files use the measured settings that setup wrote to
`llm-config.bat` for your GPU. Every other model uses llama.cpp's `--fit`: at load time it measures the model and your
free VRAM and puts as much on the GPU as fits, MoE experts spilling to RAM first. Threads, batch size and the KV cache
format are set for you. If a model fails to start, the reason shows in **1:llm** and the full log is in
`command-center\llama.log`.

Memory keeps working with any model. `start-llm.bat` still works from a terminal for the bundled models.

## Files

```
setup.bat / setup.ps1      first-time setup (safe to re-run)
start-llm.bat              model launcher (menu), port 8090; reads llm-config.bat
start-standard.bat / start-uncensored.bat / check-model.bat
start-command-center.bat   opens the app (the desktop shortcut does the same without a console)
start-vscode-laptop.bat    second VS Code for the laptop's Cline (optional)
sync.bat                   commit + push in one step (for maintainers)
command-center\
  server.py      HTTP server on 127.0.0.1:8099: UI, chat proxy (adds the API key), model control, stats, Cline proxy
  models.py      model library: scans models\, reads GGUF metadata, sizes models for this PC, launches, downloads
  memory.py      long-term memory (recall, learning, embedding server, moving, import/export)
  importers.py   ChatGPT / Claude export readers
  cloud.py       optional web search / stronger-model tools (Claude, ChatGPT, Gemini)
  updater.py     update check and install (from this GitHub repo)
  chats.py       saved chats
  workspaces.py  VS Code / Cline / Tailscale helpers
  tokens.py      tokens-used meter
  index.html     the whole UI (no build step, works offline)
  fonts\         Inter and IBM Plex Mono (SIL Open Font License)
docs\review-prompt.md      a prompt for a full review/improvement pass with a strong model
```

**Not in the repo** (created on your PC): `api-key.txt`, `command-center\secrets.json` (cloud keys), `llm-config.bat`, `llama.cpp\`, `models\`, `memory\`,
`command-center\settings.json`, token logs, browser/VS Code profiles.

## Privacy and security

- Every server listens on `127.0.0.1` only. The browser never sees the API key; the command center adds it.
- Requests are checked for the right Host/Origin, so other websites can't drive the app.
- Phone access is Tailscale-only and requires Tailscale's signed-in user header.
- Your memories, chats and settings stay in files on your PC (the memory folder). Nothing is uploaded anywhere.

## Troubleshooting

- **"model is off" / ECONNREFUSED 127.0.0.1:8090**: start the model (1:llm → on, or `start-standard.bat`).
- **Cline: connection refused on 8099**: the command center isn't open (or switch Cline to direct in 5:cline).
- **Model window closes right away**: usually out of VRAM: raise `MOE_STD`/`MOE_UNC` in `llm-config.bat`.
- **Memory says "folder not found"**: the drive with the memory folder isn't connected; plug it in or pick another
  folder in 6:set.
- **Something else**: run `start-llm.bat` from a terminal to see llama.cpp's messages.
