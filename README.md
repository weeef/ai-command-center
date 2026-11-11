# ai command center

A small local "command center" for running local LLMs on Windows with llama.cpp, plus a second machine
reached through NVIDIA's Personal AI Router (PAIR), and VS Code + Cline agents on top of both.

Standard-library Python only, no dependencies. It serves a terminal-styled page on `127.0.0.1:8099`
(opened as an Edge app window) and only ever binds to localhost.

## What's in it

| tab | what it does |
|---|---|
| **1:llm** | turn the local model on/off and pick a model, chat with it, live VRAM/RAM/GPU/CPU usage for this PC and the PAIR laptop, and **tokens burned** since each model was turned on (plus lifetime and a session log) |
| **2:code** | two VS Code + Cline setups: the main VS Code → this PC's model (`:8090`), a separate "laptop" VS Code → the laptop via PAIR (`:8080`). Open projects in either, or **open · both**: the PC window on the repo and the laptop window on its own git worktree (`<repo>-laptop`, branch `cline-laptop`) so two agents can work at once without touching the same files |
| **3:web** | phone access over Tailscale (`tailscale serve` → this command center on `:8099`) with an on/off toggle and the tailnet's devices. On a phone the same UI stacks into tabs: **1:llm** (brain + chat), **2:sys** (model on/off, run on, usage), **3:web**. From the phone, VS Code, the API key and the phone-access toggle are blocked; requests must carry Tailscale's `Tailscale-User-Login` header. The phone works while the command center is open on the PC; it shuts down ~3 min after the window closes, as before |

## Layout

```
start-llm.bat              llama-server launcher (menu: standard / uncensored / 256K context), port 8090, --metrics on
start-standard.bat         start-llm.bat 1
start-uncensored.bat       start-llm.bat 2
check-model.bat            which model is running on 8090
start-vscode-laptop.bat    VS Code with its own profile (vscode-laptop\, cline-laptop\) for the laptop's Cline
start-command-center.bat   opens the UI
sync.bat                   commit + push everything in one step
command-center\
  server.py                HTTP server + model control + stats + chat proxy (adds the API key server-side)
  workspaces.py            2:code and 3:web
  chats.py                 saved chats (one JSON per chat in <memory folder>\chats\, never committed)
  memory.py                long-term memory shared by every model (recall + learning + the embedding server)
  tokens.py                token meter (llama.cpp /metrics counters; falls back to sampling /slots)
  index.html               the whole UI
  launch.pyw, taskbar.py   console-less launcher and its own taskbar identity
```

## Not in the repo (create/supply locally)

- `api-key.txt`: the llama-server API key (one line)
- `llama.cpp\`: a llama.cpp CUDA build (`llama-server.exe` and DLLs)
- `models\`: the GGUF files that `start-llm.bat` points at
- per-machine state the app writes itself: `command-center\settings.json`, `tokens.json`, `tokens-log.jsonl`, `chats\`,
  `edge-profile\`, `vscode-laptop\`, `cline-laptop\`

## Memory (4:mem)

One long-term memory shared by every model (standard, uncensored, the laptop, any future model) and every device
(this window, the phone). It lives in **one folder**, `memory\` by default (`settings.json` -> `memory_dir`):
`memory.db` (SQLite: memories as plain sentences, chat snippets, FTS5 keyword index, embedding vectors) and `chats\`.

- **recall**: before each chat request the newest message is matched against memories (meaning + keywords) and
  against snippets of other chats; the best matches go into the prompt as a system message. The reply's stat line
  shows what was recalled.
- **learn**: after each reply is saved, the active model (when idle) extracts durable facts: add / update / delete.
- **embedding model**: `models\Qwen3-Embedding-0.6B-GGUF\Qwen3-Embedding-0.6B-Q8_0.gguf` in its own llama-server on
  127.0.0.1:8091, CPU only (no VRAM). Vectors are tagged with the model's name; switch models (`embed_model` in
  settings.json) and the index rebuilds itself. Without it, recall falls back to keywords. numpy speeds up search.
- **moving drives**: 4:mem -> location -> *move here* copies the folder, verifies it, switches; the old copy stays as a
  backup. *use existing* points at a folder you copied yourself. A missing drive pauses memory instead of starting
  an empty one elsewhere. Export/import as plain JSON for anything else.

## Tokens burned

With `--metrics` (already in `start-llm.bat`) the numbers are exact: llama-server's own
`prompt_tokens_total` / `tokens_predicted_total` counters, so they stay right even if the command center was closed.
If a server runs without `--metrics` (for example the PAIR laptop, unless `--metrics` is added to its launch
arguments in PAIR's llama.cpp engine settings), it falls back to counting generated tokens from `/slots` while the
window is open, shown with `≈`.
