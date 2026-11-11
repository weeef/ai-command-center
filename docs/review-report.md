# Review pass: status report (2026-11-11)

This pass was stopped early (usage limit). What is on `main` is small and measured. The larger rewrite is parked,
unmerged, on the branch `review-pass-wip`; it passed a smoke test but not a full test run, so it is **not** on `main`.

## 1. Measured (model performance)

Harness: `bench/bench.py` (stdlib only; starts its own llama-server on port 8190; prompts are frozen copies of repo
files at commit 53b6f77). Reference PC: RTX 5080 16 GB, Ryzen 9 9950X3D, 64 GB, llama.cpp b12290, standard Q4 model,
`-c 131072 -ctk/-ctv q8_0 --load-mode none -b 2048`, about 2.8 GB of VRAM already in use by the desktop.
Tasks: **read** = 12.4k-token prompt; **write** = 400 tokens of free text; **rewrite** = rewrite a 2.9k-token file
with one change. Run-to-run noise is roughly ±4 tok/s on writing, so differences below ~5% mean nothing.

| config (changes from the default) | read tok/s | write tok/s | rewrite tok/s | peak VRAM |
|---|---|---|---|---|
| default: `--n-cpu-moe 23 -ub 2048 -t 16 --spec-type ngram-mod` | 3,054 | 77 | 191 | 15.4-15.6 GB |
| no speculation | 3,066 | 79 | 74 | 15.4 GB |
| **ngram-mod `n-max 256`** | - | 76 | **279** | 15.1 GB |
| **ngram-mod `n-min 16, n-max 256, n-match 16`** (new default) | - | 76 | **292** | 15.1 GB |
| ngram-mod `n-max 128` / `n-min 16, n-max 128, n-match 16` | - | 77 / 75 | 239 / 245 | 14.8 GB |
| ngram-mod `n-min 8, n-max 128, n-match 12` | - | 69 | 159 (worse) | 15.1 GB |
| `-t 8` / `-t 8` pinned to the V-cache CCD (`--cpu-range 0-15 --cpu-strict 1`) / `-t 16` pinned | 3,139 / 3,109 / 3,136 | 78 / 74 / 74 | 190 / 188 / 189 | 15.4 GB |
| `--prio 2` | 3,028 | 75 | 188 | 15.5 GB |
| `--poll 0` / `--poll 100` | - | 76 / 73 | 189 / 189 | 14.8 GB |
| `--n-cpu-moe 22` / `24` | 3,207 / 3,023 | 79 / 74 | 193 / 183 | 15.4 / 14.5 GB |
| KV cache f16 (needs `--n-cpu-moe 26`) | 2,862 | 70 | 173 | 15.3 GB |
| `-c 65536 --n-cpu-moe 22` | 3,109 | 72 | 189 | 14.8 GB |
| `-ub 1024` | 2,003 | 77 | 190 | 14.5 GB |
| `-ub 4096 -b 4096` / same with `--n-cpu-moe 24` | 3,904 / 4,153 | 72 / 74 | 181 / 181 | 15.7 GB |
| MTP model, `--n-cpu-moe 25`, `draft-mtp,ngram-mod --spec-draft-n-max 2`, no vision | 2,833 | **94** | 180 | 15.6 GB |

Conclusions:

- **Longer ngram drafts are the one clear win**: code rewrites go from 191 to 292 tok/s (+53%; 3.9x the speed without
  speculation) with no cost to normal writing. Applied to `start-llm.bat` and `setup.ps1`. One file, one task was
  measured; worth re-checking on your real Cline work. If you have an `llm-config.bat` with its own `SPEC=` line, it
  overrides the new default.
- Thread count, pinning to the V-cache CCD, priority, polling, KV cache type and a smaller context do not help.
- `-ub 4096` reads prompts ~30% faster but costs VRAM that is already tight and writing got slightly slower; not changed.
- MTP: +21% writing on this run (94 vs 77 tok/s), but still no vision and the VRAM peak is at the limit. Still not the default.
- VRAM headroom is thinner than the README says: peaks were 15.4-15.6 GB of 16.3 GB with today's 2.8 GB desktop usage.
  `--n-cpu-moe 23` works, but there is under 1 GB spare; `24` is the safe choice if other GPU apps are open.

The README "Speed" table has not been updated with these numbers yet.

## 2. Found, fixed only on the branch `review-pass-wip`

The most important finding is a **security bug in `server.py` on `main`**:

- **Request smuggling past the Origin check.** When a request is refused (bad Origin, wrong Content-Type, too large)
  the server answers without reading the request body and keeps the connection open, so the body is then parsed as
  a second request. Any web page open in a browser on the PC can send a "simple" cross-origin POST whose body is a
  complete API request with a local `Host` and no `Origin`; that inner request passes every check. It could add or
  delete memories, delete chats, switch the memory folder, start a model, or save a cloud key. The fix (branch):
  a connection is closed unless the body was read in full, and `Transfer-Encoding` requests are refused. The smoke
  test confirms the smuggled request is no longer answered.

Also on the branch (written, smoke-tested, not fully tested):

- `server.py`: route table instead of if-chains; routes are PC-only unless marked for the phone; `Sec-Fetch-Site`
  check; the phone's Tailscale login must be this PC's own login; the API key is never sent to the page (copy goes
  through the server to the clipboard); atomic `settings.json`; errors logged to `server-errors.log`; idle exit
  waits for imports and moves; upload checks happen before the upload is read.
- `memory.py`: the memory block each message was sent with is stored in `memory.db`, so the prompt cache survives a
  restart of the command center; vectors are updated in place instead of re-read on every change; archived
  memories (events long past, losers of a merge) instead of deletion; model-judged merging of near-duplicates;
  stricter learner prompt plus server-side checks (no secrets, no relative dates, no "the user asked..."); the
  learner can only update or delete memories it was shown; keyword-only recall needs two shared words; the
  embedding server dies with the app (job object), orphans and port conflicts are handled; the drive coming back is
  noticed without a restart.
- `importers.py`: conversations are read one at a time instead of loading the whole export into RAM; split
  `conversations-000.json` files are recognised.
- `chats.py`: history listing only re-reads changed files; a rename no longer moves the chat to the top.
- `cloud.py`: at most 3 tool calls per reply, identical searches cached for 15 minutes, broken tool-call JSON is
  recovered, the tool loop stops when the browser is gone.
- `workspaces.py`: folder paths are always quoted for cmd.exe (a folder named `a&b` could run a command).
- `fsutil.py` (atomic writes), `tests/helpers.py` (fake model and embedding servers).

**Known gap on the branch:** `index.html` has not been adapted yet. Its "copy API key" buttons would copy the masked
key; they need to call `POST /api/key/copy`. The 6:set list also has no control for archived memories yet. Do not
merge the branch before that and before the tests exist.

## 3. Not done

Memory evaluation set and threshold tuning (the thresholds on the branch are the old ones plus untested additions),
the unit/HTTP test suite beyond the helpers, the UI split and CSP tightening, the UX walk-through, setup testing for
other GPUs, cloud provider checks with a real key, and the README update.

## 4. Next three things

1. Finish the branch: adapt `index.html` (key copy, archived memories), write the tests around the smuggling fix,
   the route table and memory, then merge. The smuggling fix alone is about 15 lines and could be merged first.
2. Build the memory evaluation (fixed memories + questions) and tune the recall thresholds and the learner prompt
   against it with the real embedding model, checking `cache_n` across turns.
3. Re-run `bench/bench.py` for the uncensored model and on real Cline tasks, then update the README speed table.
