# Review pass: status report (2026-11-11)

> **Update 2026-11-16.** Most of what this report lists as open is now on `main`, done as small patches instead of
> merging the branch (which had fallen 11 commits behind):
>
> - ported from the branch: `workspaces.py` cmd.exe quoting, streamed imports, chat list caching, cloud search
>   cache and tool-argument recovery, atomic writes (`fsutil.py`)
> - hardening: the API key never reaches the page, cross-site requests refused, the phone must use this PC's own
>   Tailscale login, phone routes are an allow-list, atomic `settings.json`, idle exit waits for imports and moves,
>   3 cloud tool calls per reply
> - `tests/smoke.py` covers the above, including the request-smuggling fix
> - `bench/memeval.py` exists; the recall floor (0.36 -> 0.40) and duplicate threshold (0.93 -> 0.85) were tuned
>   against it
> - the README speed section has the numbers from section 1
>
> Still open: the route-table refactor and the `memory.py` rewrite on `review-pass-wip` (the rewrite should be
> judged against `bench/memeval.py` before any of it is taken), an evaluation of the learner prompt, a benchmark
> run on the current standard model, the UI split and CSP tightening, setup testing on other GPUs, and cloud
> provider checks with a real key. The rest of this report is unchanged from 2026-11-11.

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

The most important finding was a **security bug in `server.py`**. The minimal fix for it is now on `main` as well
(connections are closed unless the request body was read in full; verified that a smuggled request is no longer answered):

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

---

# Agent team pass (2026-11-22)

Goal: turn "open · both" into a real multi-agent mode, where one goal is split between coding agents that work at the
same time, each in its own worktree, and you review and merge. Five steps, each committed and pushed on its own.

## What was built

1. **Agent registry + worktree helper** (`team.py`, `workspaces.py`). Agents are a name, a backend (`pc`, `laptop`,
   `cloud:anthropic`) and a client (`vscode` = one of the two existing VS Code setups, `claude-code` = headless). All
   three are checked against fixed sets. `ensure_worktree()` is general; `laptop_worktree()` now uses it and behaves
   as before. `agent_worktree()` gives `<repo>-agents\<agent>` on `agent/<agent>/<task>`. A folder with uncommitted
   work is never switched or removed, and a folder that isn't one of the repo's worktrees is never touched.
2. **Task board + planner.** One board per repo in `<git common dir>\cc-team\board.json`, never committed. A split
   needs one task per agent and owned paths inside the repo, and no two tasks may own overlapping paths. The planner
   is the strongest backend that's on: cloud only when it's set up *and* ticked for that plan. It sees the goal, the
   tracked file list and the README head. Each agent's brief (`.cc-agent.md`, plus `.clinerules/cc-team.md` for
   Cline) has its task, its files, the others' status and the handoff notes; agents append `- <name>: …` lines,
   which are passed on. The brief files are excluded via `.git\info\exclude`.
3. **7:team tab + live status.** Start opens VS Code on each Cline agent's worktree (the starter prompt goes on the
   clipboard: Cline can't be handed a task from outside) and runs Claude Code headless (its native `claude.exe`,
   with a fixed argument list, since the task is in the brief). On a local model, Claude Code goes through
   `/agent/<name>/v1/messages` with a token made for that one run. Steps come from the Cline proxy and from Claude
   Code's stream-json: tool and file, tokens, cache share. Also shown: changed files, files changed outside what the
   agent owns, and files more than one agent touched. Stop kills a headless agent's process tree; for Cline, the
   proxy refuses its next step.
4. **Review + merge.** When an agent finishes, its leftovers are committed on its own branch and the project's tests
   run in its worktree (`tests\smoke.py` > pytest > `npm test`, one run at a time). Per agent: diff, tests, merge,
   send back with a note, discard. Merge is `--no-ff` in your checkout. It refuses when your checkout has uncommitted
   changes or is on another branch; a conflict is aborted and named. Never forced, never pushed. Clean-up asks first
   and keeps folders with unsaved work; merged branches are deleted with `-d`, discarded ones only if ticked. The
   uninstaller lists `-agents` worktrees.
5. **`--parallel` option**, off by default: **model picker → two agents at once** gives two slots,
   `-np 2 -c 2×ctx --kv-unified-per-slot ctx`.

Phone: can read 7:team and diffs. Every POST under `/api/team` is refused from the phone, including routes added
later (`REMOTE_READONLY`). `/api/models/slots` is PC-only.

Tests: `tests\smoke.py` grew from 9 to 16 groups (worktree, team, board, teamrun, review, slots, claude). They use
throw-away repos (paths with a space and `&`), a fake model server and a fake Claude Code. No real VS Code, model or
cloud is touched.

## What was measured

**`--parallel` on the RTX 5080** (`bench.py --tasks agents`): two agents, ~15-20k-token contexts each, three turns
each. Full table in the README.

| model | slots | both writing at once | each alone | first prompts | later turns cached | peak VRAM |
|---|---|---|---|---|---|---|
| Qwen3.6 35B-A3B, 64K/agent | 1 | 68 tok/s | 77 / 68 | 13.5 s | 99.8% | 13.5 GB |
| | 2 | 97 tok/s (+42%) | 52 / 54 | 13.5 s | 99.8% | 14.3 GB |
| Qwen3.8 27B, 32K/agent, no MTP | 1 | 52 tok/s | 55 / 54 | 17.9 s | 99.8% | 14.6 GB |
| | 2 | 84 tok/s (+63%) | 44 / 44 | 18.8 s | 99.8% | 15.7 GB |

What the numbers say:
- One slot already keeps each agent's prompt cache when the agents take turns: `--cache-ram` restores it.
- Two slots only pay off while both agents generate text at the same time, and they cost 0.8-1.2 GB of VRAM.
- Two 16K slots can't hold a 19k-token agent prompt.

Hence an opt-in, not the default.

**Live runs** (real planner, real Claude Code 2.1.330 on the local Qwen3.8 27B, temp repo):
- **Planner:** the first plan gave both tasks to one agent, which the validation refused. One retry with the reason
  fixed it, so local models now get one retry with the broken rule. Planning took 9 s.
- **First Claude Code run:** HTTP 500, *"System message must be at the beginning"*. Claude Code puts `system`-role
  messages mid-conversation and Qwen's template refuses them. The proxy now folds them into the user turn as
  `<system-reminder>`, which keeps the prompt prefix stable for the cache.
- **After that fix:** one agent built its part in 154 s (31 steps, 95% of prompt tokens cached), stayed inside its
  files, and left a correct handoff note. In the two-agent run the second agent failed with "Prompt is too long":
  Claude Code's own prompt is ~27-32k tokens and the model ran at 32K. Start now refuses a Claude Code agent on a
  model under 64K, with that explanation.
- **The live run also fixed:**
  - token counting: Claude Code repeats a reply's usage on every content block, so prompt tokens were counted twice
  - a made-up cost on local models
  - `__pycache__` files that would have been committed with an agent's work

## Left open, and why

- **Cline agents were not run live.** Their path is the existing 5:cline proxy plus the step parser, tested with a
  fake model server. A real run needs VS Code windows and your clicks (pasting the starter prompt). Worth doing
  while recording the demo.
- **Cline CLI** (npm `cline` 3.0.75) isn't installed and wasn't installed: it needs your OK. It would give headless
  Cline agents.
- **More than two VS Code agents** would need more VS Code profiles (one Cline config each); headless agents have
  no such limit.
- **Not run on a real model:**
  - Claude Code on the laptop's model (PAIR's `/v1/messages`)
  - Claude Code on cloud Claude (costs money)
  - `--parallel` with MTP speculation
- **Phone layout:** checked only at Edge's minimum headless width (492 px): nothing overflows there. The real 390 px
  layout and the phone (remote) view weren't checked on a phone.
- **Restarts:** if the command center restarts while a headless agent works, the agent loses its output pipe and is
  marked stopped. Send it back to resume.

## Next three things

1. Record the demo (`docs\team-demo.md`) with two Cline agents. That's also the first live run of the Cline path.
2. Run the planner on a bigger real repo and compare it with cloud planning on the same goal (with your OK on the
   cost).
3. Decide on the Cline CLI. If it works headless against the command center's `/cline` route, Cline agents would no
   longer need VS Code windows or a pasted prompt.
