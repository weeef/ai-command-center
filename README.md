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
| **1:llm** | Turn the model on/off, pick *standard* or *uncensored*, choose **run on** *this pc* or *laptop*, and chat. Paste or drop images (vision). Live GPU/RAM/CPU usage, speed of the last reply (pp = reading, tg = writing) and tokens used. The **brain** shows the state: grey = off, nodes lighting up = loading, pulses = thinking. In the *corpus* theme its glowing nodes are your memories; it drifts and breathes even when the model is off, leans toward the pointer, and a click fires the nearest neurons. |
| **2:code** | Open projects in VS Code with the Cline coding agent wired to your local model. Optional second "laptop" VS Code (see *Two machines*). |
| **3:web** | Phone access over Tailscale: one button to turn it on, the address to open on your phone, your devices. |
| **4:mem** | Your memories as a **brain**: every memory is a neuron in its topic's region (about you = frontal lobe, preferences = prefrontal, how-tos = motor strip, projects = parietal, people + events = temporal, facts = occipital, other = cerebellum). Bigger = recalled more often, ring = pinned. Synapses join memories with related meaning; signals travel along them and a neuron fires when the model recalls it. Click a neuron to read/edit/pin/forget it; switch to **tree** for a radial tree by topic. Scroll to zoom, drag to move. |
| **5:cline** | Route Cline through the command center so it gets memory too, plus a log of every Cline task (steps, memories used, tokens, cache hit rate). |
| **7:team** | An **agent team**: give one goal, a planner splits it into one task per coding agent with the files each one owns, every agent works in its own git worktree at the same time, and you review each branch (diff, tests) and merge it. Optionally **Claude conducts**: it plans, checks every part and sends it back with fixes until it's right. See *Agent team*. |
| **6:set** | Memory on/off and auto-learning, the memory folder (move it to another drive), import from ChatGPT/Claude, export, the searchable memory list, the theme, and **cloud & web search** (API keys for Claude / ChatGPT / Gemini). |

Keyboard: **Alt+1 … Alt+7** switch tabs. In chat: **Enter** sends, **Shift+Enter** new line, `/help` lists commands
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
| `SPEC` | `--spec-type ngram-mod` drafts repeated text, making code rewrites (what Cline does all day) 2.5-4x faster. The default drafts long runs (`n-min 16, n-max 256, n-match 16`). A `SPEC=` line in your own `llm-config.bat` overrides it. |
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

**Re-measured on 2026-11-11** with `bench\bench.py` (same PC and the standard Q4 model of that date, before Qwen3.8
became the standard; about 2.8 GB of VRAM was already in use by the desktop, so these are not comparable with the
table above row for row):

| speculation | writing | rewriting a 2.9k-token file | peak VRAM |
|---|---|---|---|
| none | 79 tok/s | 74 tok/s | 15.4 GB |
| `ngram-mod`, old defaults | 77 tok/s | 191 tok/s | 15.4-15.6 GB |
| **`ngram-mod`, `n-min 16, n-max 256, n-match 16` (the default now)** | 76 tok/s | **292 tok/s** | 15.1 GB |

One file and one task were measured, and run-to-run noise is about ±4 tok/s on writing. In the same run: thread
count, pinning to the V-cache cores, process priority, polling, an f16 KV cache and a smaller context made no
difference; `-ub 4096` read prompts ~30% faster but needs VRAM that is already tight. With a desktop using 2.8 GB,
`MOE 23` leaves under 1 GB of VRAM spare, so use 24 if other GPU apps are open.

### Two agents on one model (`--parallel`)

**model picker → two agents at once** gives this PC's model two slots (`-np 2 -c 2×ctx --kv-unified-per-slot ctx`)
from its next start. It's off by default. Measured on 2026-11-22 with `bench\bench.py --tasks agents` (two Cline-style
agents, each with its own ~15-20k-token project context: a first request, a file rewrite, then ~300 words of free
writing, all in the same conversation; RTX 5080, llama.cpp b12590, ngram-mod speculation, 1.7 GB of VRAM used by
the desktop):

| model | slots | both writing at once: together | each agent | reading both first prompts | later turns cached | peak VRAM |
|---|---|---|---|---|---|---|
| Qwen3.6 35B-A3B Q4, `MOE 23`, 64K per agent | 1 (take turns) | 68 tok/s | 77 / 68 tok/s | 13.5 s | 99.8% | 13.5 GB |
| | **2** | **97 tok/s (+42%)** | 52 / 54 tok/s | 13.5 s | 99.8% | 14.3 GB |
| Qwen3.8 27B IQ3_S, 32K per agent, without MTP | 1 (take turns) | 52 tok/s | 55 / 54 tok/s | 17.9 s | 99.8% | 14.6 GB |
| | **2** | **84 tok/s (+63%)** | 44 / 44 tok/s | 18.8 s | 99.8% | 15.7 GB |

- **The prompt cache holds either way.** With one slot the agent that waits gets its conversation back from
  `--cache-ram` (it doesn't have to read its 15-20k tokens again), so taking turns costs waiting time, not re-reading.
- **Two slots only help while both agents write at the same time.** Reading long prompts is no faster (the GPU is
  already busy), and each agent alone is slower than it would be with the model to itself.
- **It costs 0.8-1.2 GB of VRAM.** Qwen3.8 27B with two 32K slots peaked at 15.7 GB of 16.3, which is too close
  with other GPU apps open. Two 16K slots are not an option: a Cline-sized prompt (19k tokens) didn't fit.
- Turn it on when two agents in **7:team** use this PC's model at once. With one agent on this PC and one on the
  laptop (the default team), leave it off.

### Optimise any model automatically

In the model list, **learn the fastest settings for each model while you use it** is on by default. A model always
loads straight away: no trial runs before you can use it, so load time is just what this PC needs to load it. While
you use it, the command center reads llama-server's own counters (tokens and seconds spent reading and writing) and
the GPU's peak VRAM every few seconds; nothing extra is sent to the model. Once a setting has done enough real work
(about 1,500 tokens written) it is judged, and the next time the model loads it tries one other setting. When every
candidate has been judged the fastest is kept and nothing more is tried. It works for any GGUF on any NVIDIA GPU.

- **What is tried:** how much of the model sits on the GPU (llama.cpp's own fit with different VRAM reserves, and
  for mixture-of-experts models the number of expert layers kept in RAM), then the prompt batch size on the winner.
- **How it decides:** writing speed first. A change has to be at least 3% faster to replace the default, a setting
  that leaves less than 0.6 GB of VRAM free is never chosen, and of two near-equal settings the one using less VRAM
  wins, because a model that touches the VRAM limit slows to a crawl. A setting that is clearly slower (below 80% of
  the best so far) is judged after a few hundred tokens, so a bad one is dropped quickly.
- **When a load fails:** if the default prompt batch doesn't fit (some architectures, such as Inkling-2-Mini, need
  memory that grows with context x batch), the size llama-server reports sets the batch that fits and it loads again
  at once; that batch is remembered for the model. A setting being tried that doesn't load is never tried again and
  the best known one starts at once. Saved settings that stop fitting (a driver or engine update, less free VRAM)
  fall back to the defaults instead of failing.
- **When it learns again:** a result belongs to one model file, context size, mode (fast text / images) and GPU.
  Change one and that combination is learned from scratch.
- **optimise / optimise again** on a model's card measures everything at once instead: a few trial copies on a spare
  port, a few minutes, only when you ask. Turning the model off cancels it. Results are in
  `command-center\tuning.json`, the trial servers' output in `command-center\tune.log`.

Measure with the programs you normally keep open: what else is using VRAM at that moment decides what fits.

**Checking a change:** `python bench\bench.py` measures speed (it starts its own llama-server on port 8190);
`python bench\memeval.py` measures memory recall on a fixed, invented set of memories and questions (run it before changing the thresholds in `memory.py`); `python tests\smoke.py` checks the command center's request guard, phone rules, key handling and importers without
starting a model.

---

## Web access for the local model (optional, free)

A connector that lets the local model use the web by itself, with no account and no cost:

- **web_search**: asks a search engine and gets back titles, links and short excerpts.
- **web_fetch**: opens one page and reads its text (a search result, or a link you paste into the chat).

Turn it on in **6:set → web access**. In the chat you see each search (`🔎`) and each page it reads (`📄`), up to 6
per reply.

- **How dependable search is.** Without a key, searches go through the public pages of DuckDuckGo and Brave, taking
  turns. They can refuse a program that asks too often; one that refuses is left alone for ten minutes while the
  other answers. If that gets in the way, save a **search key** in the same box (Tavily or the Brave Search API):
  it is asked first and the public pages become the fallback. The key path has not been run against a real key yet.
- Pages are downloaded with `curl`, which ships with Windows 10 and later, because search engines and many sites
  turn away Python's own client. Without curl it falls back to Python and search will mostly be refused.
- **What leaves your PC.** The search the model writes goes to the search engine, and a page request goes to the
  site being read. Never the conversation or the memories.
- **Safety.** Web pages can contain text aimed at the model. The connector never opens addresses on this PC, your
  home network or your tailnet, and it only opens links that came from a search result, from a page it read, or
  from the conversation, so a page can't talk the model into sending your chat somewhere inside a made-up link.
  The model is also told to treat web text as information, not instructions.
- With the connector on, the cloud provider below is no longer used for searching, only for `ask_cloud`.

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
devices signed in to the same Tailscale account as the PC can reach it; it is not on the public internet. A device
that someone else shared into your tailnet is refused. To let another account in, add its login to a list called
`tailscale_allow` in `command-center\settings.json`, for example `"tailscale_allow": ["partner@example.com"]`.

## VS Code + Cline (optional)

Install [VS Code](https://code.visualstudio.com/) and the **Cline** extension. In Cline's settings choose
*OpenAI Compatible*, base URL `http://127.0.0.1:8099/cline/pc/v1` (through the command center, with memory; or
`http://127.0.0.1:8090/v1` directly), any model id, and the key from `api-key.txt`. Or let **5:cline** set it for you
(close VS Code first). Routed Cline needs the command center to be open.

## Two machines (optional)

If a second PC runs NVIDIA's Personal AI Router (PAIR), the command center can use its model: **run on → laptop** in
1:llm. `start-vscode-laptop.bat` opens a separate VS Code whose Cline uses the laptop, and **2:code → open · both**
puts the two agents on separate git worktrees of the same repo so they can work side by side. **7:team** turns that
into a coordinated team (next section).

## Agent team (optional)

Several coding agents build one feature at the same time, each in its own copy of the project, and you merge what
they made. Open **7:team** (Alt+7):

1. **Pick the project** (any folder inside a git repo with at least one commit) and **write the goal** in a few
   sentences.
2. **plan**: a planner splits it into one task per agent and says which files and folders each agent **owns**. It
   uses the strongest model that's on: cloud only if you tick *may use cloud* (the goal, the repo's file list and the
   start of its README are sent; billed), then this PC's model, then the laptop's. You can also write the split
   yourself. **Edit it** before anything starts: no two agents may own the same file, which is what keeps the
   merge clean; the app refuses a split that overlaps.
3. **start team**. Every agent gets its own git worktree, `<project>-agents\<agent>`, on a branch
   `agent/<agent>/<task>` made from the commit you're on, plus a brief, `.cc-agent.md`: its task, the files it owns,
   what the other agents are doing, and handoff notes. The brief files never reach git (`.git\info\exclude`).
   - **Cline in VS Code** agents (*pc* = your main VS Code, *laptop* = the laptop VS Code) open on their worktree.
     Cline can't be handed a task from outside, so the one-line starter prompt is put on the clipboard: paste it
     into each Cline. For the live log, route the main Cline through the command center (5:cline).
   - **Claude Code** agents (add them under *agents* in 7:team, if Claude Code is installed) run headless: no
     window, the command center starts `claude` in the worktree and reads its progress. On *this PC's* or *the
     laptop's* model they talk to llama-server's Anthropic-compatible endpoint through the command center
     (`/agent/<name>/v1/messages`, with a token made for that one run). On *Claude (cloud)* they use your Claude key
     from 6:set, or your own Claude Code login, and **every start asks first**, since it's billed.
     On a local model Claude Code needs **at least 64K of context** (its own instructions and tools are ~30k tokens);
     the team won't start one on a smaller context. Raise it in the model picker.
     They may edit files and run python / pytest / npm test / git status, diff, add, commit; never push, merge,
     switch branches or browse the web.
4. **Watch**: one panel per agent with its status, each step (tool and file, tokens, how much of the prompt came
   from the cache), the files it changed, files it changed **outside** what it owns, files **another agent** also
   changed, and handoff notes. An agent that needs something from another one writes `- <its name>: …` under
   *Handoff notes* in its brief; the command center passes it on to the others' briefs. **stop** ends a headless
   agent; for Cline it refuses its next request.
5. **Review**: when an agent is done (Claude Code finishes by itself; for Cline press **finished**), whatever it left
   uncommitted is committed on its branch and the project's tests run in its worktree (`tests\smoke.py`, else
   `pytest`, else `npm test`). Then per agent: **diff** against the starting commit, **run tests**, **merge**,
   **send back…** with a note (it goes into the brief and the agent works on it again), or **discard**.
   - **merge** is `git merge --no-ff` into the branch you started from, in your own checkout. It refuses while your
     checkout has uncommitted changes or is on another branch; a conflict is aborted and named, leaving everything
     as it was. Nothing is ever auto-merged, forced or pushed.
6. **clean up…** lists exactly what goes and asks first: worktrees of merged or discarded tasks are removed only if
   they have no uncommitted changes, merged branches are deleted with `git branch -d` (which refuses unmerged
   work), discarded branches only if you tick that too. The uninstaller also lists `<project>-agents` worktrees.

### Claude conducts (optional)

You give Claude the idea; Claude directs the local models. It runs on **your Claude Code login** (the `claude` you're
signed in to, so your plan's usage, no API key and no per-token bill) and needs Claude Code installed.

- **plan** with *planner: Claude, who then conducts*. Claude writes the split like work orders for smaller models:
  what the part is for, numbered steps with exact file, function and route names, and a *Done when:* line the agent
  can check itself. You can still edit the split before you start.
- Or tick **Claude conducts** under the split of any team, with any planner, at any time; pick the model there
  (sonnet by default, opus or haiku).
- When an agent finishes and its tests have run, Claude checks the part: the task, the diff, the test output and the
  handoff notes. It **approves** it, **sends it back** to the same agent with a numbered list of fixes (it goes
  into the agent's brief under *Sent back for changes*, and a Claude Code agent starts again by itself), or **gives
  up** and tells you why. After 3 send-backs on one task it stops and leaves it to you.
- When every part is approved, Claude checks them **together** (do the names, routes and JSON shapes match?) and can
  send one part back once more. Then the conductor box says *All parts approved* and you review and merge as usual.
  **Claude never merges**, and it never sends work back to a cloud agent (that's billed: you do it).
- A Cline agent that Claude sends back can't be restarted from outside: its panel says so; tell its Cline to read
  `.cc-agent.md` again. Your clipboard is left alone.
- Every call shows in the conductor box (what it checked, the verdict, tokens, seconds, and the equivalent API price,
  which your plan covers). **ask Claude** on a finished part checks it again on demand.

Claude is called as `claude -p` with **every tool turned off**, so it can't read your disk or run anything: it sees only
what the command center sends on stdin. That is the goal, the repo's file list and README start, the whole repo when
its tracked text adds up to less than 60 KB, and each agent's diff and test output. Files whose names look like
secrets (`*key*`, `*secret*`, `*token*`, `.env`, `*.pem`, …) are never sent. The command line is fixed: the model is
one of sonnet / opus / haiku, and the prompts are in `conductor.py`.

The board (tasks, owners, status, steps, notes, test results) is `.git\cc-team\board.json` inside the project:
shared by all its worktrees, never committed. The phone can watch 7:team but not plan, start, stop, merge or clean
up. One team runs at a time; each agent has one task at a time.

**Two agents on one GPU.** Two agents can use this PC's model at once. With one server slot (the default, `-np 1`)
their requests take turns; see *Speed and settings* for what two slots (`--parallel 2`) measured on a 16 GB card.

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

### llama.cpp engine updates

**6:set → llama.cpp engine** keeps `llama.cpp\` on a recent official build from
[ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/releases), the same kind you have (CUDA 13 / CUDA 12 /
Vulkan / CPU). It checks every 12 hours.

- **install automatically** (on by default): downloads the newest build that has been out for at least a day, checks
  it against the sha256 GitHub publishes, unpacks it to `llama.cpp.next\`, and tests it with `llama-server --version`.
  It swaps the build in only when no model is running: either when it finds the model off, or right before the
  command center starts a model. It never stops a running model by itself. The ~400 MB CUDA runtime is downloaded
  only when it has changed.
- **install bNNNNN now**: gets the very newest build, stops the model, swaps, and starts the same model again.
- **roll back**: the previous build is kept in `llama.cpp-prev\`. If a new build is slower or broken, one click
  switches back. The two folders just trade places.
- If anything from `llama.cpp\` is still running (for example a `start-llm.bat` window), Windows locks the folder.
  The swap then fails and nothing changes.

---

## Picking and adding models

**Qwen3.8 27B on a 16 GB card.** If `models\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-IQ3_S.gguf` (standard) and
`models\Qwen3.8-27B-Uncensored-MTP-GGUF\Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-IQ3_XS.gguf` (uncensored) are
present, *standard* and *uncensored* mean those. Otherwise they mean the Qwen3.6 files. A dense 27B model is only fast
when all of it fits on the GPU: the ~3-bit quants do (about 75 t/s with MTP on an RTX 5080), and a 4-bit one that
spills into RAM drops to about 12 t/s.

**Images or speed.** Each card shows **images** or **text only**, plus **MTP fast** when multi-token prediction is
on. llama.cpp can't run MTP and image input together, so models that have both get a **mode** switch on their card:
*fast text (MTP)* or *images* (about 30% slower). If you send an image to a text-only model, it gets your text plus a
note that an image was left out, instead of an error.

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
uninstall.bat / .ps1       removes it all again: app, models, memory, shortcuts, optionally the prerequisites
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
  webtool.py     the web connector: free web search and page reading as tools for the local model
  updater.py     update check and install (from this GitHub repo)
  llamaup.py     llama.cpp engine updates (official builds, checksum-verified, roll back)
  chats.py       saved chats
  workspaces.py  VS Code / Cline / Tailscale helpers, git worktrees for the agents
  team.py        agent team: agent registry, task board, planner prompt, briefs, Claude Code runner, review/merge
  conductor.py   Claude (your Claude Code login) planning the team, checking each part and sending it back
  tokens.py      tokens-used meter
  tuner.py       learns the fastest settings for each model while in use; "optimise" measures them on request
  fsutil.py      atomic file writes shared by the modules above
  index.html     the whole UI (no build step, works offline)
  fonts\         Inter and IBM Plex Mono (SIL Open Font License)
bench\bench.py             speed benchmark (starts its own llama-server)
bench\memeval.py           memory recall evaluation on a fixed, invented set of memories
tests\smoke.py             smoke tests: request guard, phone rules, key handling, importers, worktrees, agent team
docs\review-prompt.md      a prompt for a full review/improvement pass with a strong model
docs\review-report.md      what the last review pass measured, fixed and left open
docs\team-demo.md          a script for recording two agents building one feature side by side
```

**Not in the repo** (created on your PC): `api-key.txt`, `command-center\secrets.json` (cloud keys), `llm-config.bat`, `llama.cpp\`, `models\`, `memory\`,
`command-center\settings.json`, token logs, browser/VS Code profiles.

## Privacy and security

- Every server listens on `127.0.0.1` only. The page never receives the API key: it shows a masked key, and
  **copy** has the command center put the real one on the clipboard.
- Requests are checked for the right Host and Origin, and cross-site requests are refused, so other websites can't
  drive the app. A refused request closes its connection, so nothing can be hidden in its body.
- Phone access is Tailscale-only, requires the login of the account this PC is signed in to, and reaches only the
  routes listed for the phone; everything else is PC-only.
- Cloud tools are capped at 3 calls per reply, and an identical search within 15 minutes is answered from a cache.
- The web connector (off by default) only opens public web addresses, and only links that were in the conversation or
  in its own search results; it is capped at 6 calls per reply.
- Your memories, chats and settings stay in files on your PC (the memory folder). Nothing is uploaded anywhere.
- *Claude conducts* (7:team, off unless you pick it) sends the goal, the project's file list and its agents' diffs
  and test output to Claude through your Claude Code login, with every tool off; files that look like secrets are
  never sent.

## Uninstall

**6:set → uninstall** lists everything with its size as **checkboxes** (quick picks: *app*, *everything*, *models +
llama.cpp*, *none*). **uninstall N ticked…** opens a window that shows your picks again and deletes nothing until you
type `uninstall`. Or double-click `uninstall.bat` in the folder and choose:

1. **everything the command center made**: the program, its settings, API key and logs, the browser and laptop VS Code
   profiles, every llama.cpp build, the models, your memories and saved chats (zipped to the desktop first), the
   desktop / start-menu / taskbar shortcuts. It also points your main Cline back at the provider it had before
   (5:cline routing) and turns off phone access (`tailscale serve`).
2. **all of that plus the prerequisites**: the pip packages setup added (numpy, anthropic), Python, Tailscale, VS Code +
   the Cline extension + `~\.cline`, and Git (through winget). Other programs may use these, so they're off in option 1.
3. **just free disk space**: models + llama.cpp builds only.
4. **tick boxes yourself**: type the numbers to tick / untick, Enter when done.

Files of yours that happen to be in the folder are kept (it asks before deleting the folder with them in it). The
NVIDIA driver, Edge, and the laptop's PAIR app are never touched. "Open both" worktrees (`<project>-laptop`) are listed
separately and removed only if you pick them and they have no uncommitted changes.

From a terminal: `uninstall.bat -All`, `-Everything`, `-ModelsOnly`, `-Only models,engine`, plus `-KeepMemory`,
`-KeepModels`, `-NoExport`, `-Yes` (no questions) and `-DryRun` (only show what would go).

## Troubleshooting

- **"model is off" / ECONNREFUSED 127.0.0.1:8090**: start the model (1:llm → on, or `start-standard.bat`).
- **Cline: connection refused on 8099**: the command center isn't open (or switch Cline to direct in 5:cline).
- **Model window closes right away**: usually out of VRAM: raise `MOE_STD`/`MOE_UNC` in `llm-config.bat`.
- **Memory says "folder not found"**: the drive with the memory folder isn't connected; plug it in or pick another
  folder in 6:set.
- **Something else**: run `start-llm.bat` from a terminal to see llama.cpp's messages.
