# Prompt: deep review and improvement pass (for Claude Fable)

Copy everything below the line into a new Claude Code session started in the project folder
(`D:\oLLama server`, or wherever you cloned it), with the strongest model selected.

---

You are taking over a working, personal software project and your job is to make it as good as it can be: faster,
more reliable, more secure, easier to share, and pleasant to use. Work like a senior engineer who owns it now:
measure before optimizing, keep everything that works working, and leave it better documented than you found it.

## What the project is

A local AI "command center" for Windows + NVIDIA GPUs, built around llama.cpp. It runs large open models on the
user's own PC (and a second laptop through NVIDIA PAIR), and wraps them in a single-page app served by a small Python
server. Read `README.md` first for the user-facing view, then every file below in full before changing anything.

- `start-llm.bat`, `start-standard.bat`, `start-uncensored.bat`, `check-model.bat`: llama-server launchers
  (port 8090, API key in `api-key.txt`, MoE experts partly in system RAM via `--n-cpu-moe`).
- `setup.ps1` / `setup.bat`: first-time setup for a new machine (downloads llama.cpp, models, writes config).
- `command-center/server.py`: stdlib-only HTTP server on 127.0.0.1:8099. Serves the UI, proxies chat to the model
  (adding the API key server-side), model on/off, stats, Tailscale phone access, Cline proxy, memory API.
- `command-center/index.html`: the whole UI in one file (no build step, no external requests, strict CSP).
  Tabs: 1:llm (brain visual + chat), 2:code (VS Code/Cline setups), 3:web (phone access), 4:mem (memory tree),
  5:cline (Cline routed through the app), 6:set (memory settings, location, import, theme). Responsive phone layout.
- `command-center/memory.py`: long-term memory shared by every model. SQLite (FTS5 + embedding vectors) in one
  portable folder; a CPU-only embedding llama-server on :8091 (Qwen3-Embedding-0.6B); recall injects a memory block
  into the newest user message (chat) or the task message (Cline) to keep llama.cpp's prompt cache valid; a
  background learner asks the active model to add/update/delete memories from saved chats.
- `command-center/importers.py`: ChatGPT / Claude data-export import.
- `command-center/chats.py`, `workspaces.py`, `tokens.py`, `taskbar.py`, `launch.pyw`: chats on disk, VS Code/Cline
  and Tailscale helpers, token meter, taskbar identity, console-less launcher.

## Hard rules

1. **Never lose or leak user data.** The memory folder (`memory/` or wherever `settings.json -> memory_dir` points),
   chats, `api-key.txt`, `settings.json`, `tokens*.json*`, browser/VS Code profiles and models are personal. Never
   delete them, never commit them. When you need test data, create clearly-marked test items and delete exactly
   those ids afterwards.
2. **The GitHub repo is public.** `.gitignore` is a whitelist: only code, launchers and docs are tracked. Before every
   commit, check the staged diff for keys, personal names, hostnames and tailnet names. Commit in small, described
   steps and push to `main` (the owner wants every change pushed).
3. **Stay dependency-light.** Python stdlib (numpy optional), no npm, no build step, no CDN at runtime: the app must
   work offline and on a phone over Tailscale.
4. **Keep the security model**: servers bind 127.0.0.1 only; Host/Origin checks; phone access only through
   `tailscale serve` with the `Tailscale-User-Login` header; the browser never sees the API key; the phone can't open
   VS Code, change the memory location, or switch phone access off.
5. **Ask before** anything expensive or outward-facing: multi-GB downloads, changing Tailscale config, rewriting the
   user's Cline settings while VS Code is open, deleting files you didn't create.
6. Windows paths contain spaces (`D:\oLLama server`): quote everything.

## What to do

Work through these in order. For each: find the problems, fix the important ones, verify, commit.

1. **Performance of the models (biggest user-visible win).** Measure tokens/s (prompt processing and generation) on
   a ~12k-token prompt and a Cline-style "rewrite this file with one change" task, for each model and config.
   A benchmark harness exists in the git history notes / README "Performance" section; rebuild it if needed.
   Explore: `--n-cpu-moe` vs VRAM headroom, `-ub/-b`, threads (the 9950X3D has two CCDs; try pinning to the V-cache
   CCD), MTP speculative decoding (`--spec-type draft-mtp`, needs MTP GGUFs, no `--mmproj` yet), ngram speculation
   for code edits, newer llama.cpp builds, KV cache type, context size trade-offs. Keep vision available somewhere.
   Record results in the README with the exact flags.
2. **Memory quality.** Build a small offline evaluation (a fixed set of memories + questions with expected recalls)
   and tune: thresholds, hybrid scoring, chunking of old chats, dedupe/consolidation of near-duplicate memories,
   contradiction handling, decay or archiving of stale memories, the learner prompt (precision over recall: no junk
   memories), and the token budget of the injected block. Make sure prompt-cache reuse still holds after changes
   (check `cache_n` in llama.cpp timings across turns).
3. **Correctness and robustness.** Read for bugs: threading (ThreadingHTTPServer + shared SQLite + background
   workers), partial writes, the streaming proxy (client disconnects, upstream errors), the idle-exit logic, the
   embedding server lifecycle (orphans, port conflicts), large imports, missing drives. Add a test suite
   (`python -m unittest`, stdlib only) for memory.py, importers.py, chats.py and the HTTP API (run the server on a
   spare port with a temp memory folder and a fake upstream model server).
4. **Security review.** Path traversal, request smuggling through the proxies, CSRF from other local sites (Origin
   checks on every state-changing route), the upload endpoint, Tailscale remote restrictions, anything that could
   expose the API key or memories. Fix what you find and explain it.
5. **Code health.** `server.py` and `index.html` have grown by patching. Refactor where it clearly pays off (route
   table instead of if-chains, splitting the UI script into well-named sections or files served by the server),
   without changing behaviour. Remove dead code. Keep the comments explaining *why*.
6. **UX polish.** Walk every tab at 1400x920 and at phone width (390px), in every theme. Fix layout bugs, empty
   states, error messages, keyboard access and contrast. The memory tree must stay readable with 1,000+ memories.
7. **Shareability.** A friend with a different NVIDIA GPU (8-24 GB) should go from `git clone` to chatting by
   running `setup.bat`. Test the setup logic paths you can, make VRAM-dependent settings adapt, and make the README
   instructions exact.
8. **Optional features if time allows**: web search as a tool the local model can call; an optional cloud model
   (Claude API) the local model can consult; both behind settings, off by default, keys stored outside the repo.

## How to report

Finish with a short report in `docs/review-report.md`: what you measured (before/after numbers), what you changed and
why, what you found but did not change (with reasons), and the next three things worth doing.
