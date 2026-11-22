# Demo script: two AI agents build one feature side by side

A 60-90 second screen recording for LinkedIn: one goal goes in, a planner splits it, two local coding agents build
their halves at the same time in separate git worktrees, and you review and merge both. Everything runs on your own
hardware (this PC + the laptop through PAIR), no cloud.

## Before you record (10 minutes)

1. **A small demo project**, so the agents finish in a few minutes. In a terminal:

   ```bat
   mkdir D:\demo\todo-app && cd /d D:\demo\todo-app
   git init -b main
   mkdir server static tests
   ```

   Put a tiny app in it (or ask Cline to write one first): `server/app.py` (a stdlib `http.server` that serves
   `static/` and `GET /api/todos` as JSON from a list in memory), `static/index.html` (a list of todos fetched from
   that route) and `tests/smoke.py` (starts the server on a spare port and checks that `/api/todos` answers). Then
   `git add -A && git commit -m "todo app"`.
2. **Models on**: in **1:llm** turn on this PC's model; choose **run on → laptop** once and turn the laptop's model
   on too, then switch back to **this pc**. Both chips should say *on*.
3. **Cline routed** (5:cline → *route through here* for both VS Code setups, with VS Code closed), so the steps show
   live in 7:team.
4. **Clean screen**: close other windows, hide the taskbar clock if you like. Don't show the API key, your tailnet
   name, or paths with your name in them (use a demo folder like `D:\demo`). Turn notifications off.
5. **Layout**: command center on the left half of the screen, the two VS Code windows stacked on the right (they open
   in step 3 of the recording; snap them into place during a cut).

## The recording

| time | on screen | say (or caption) |
|---|---|---|
| 0:00 | 7:team, empty. Paste `D:\demo\todo-app`, press **load** | "One feature, two AI coding agents, one GPU each, all local." |
| 0:05 | Type the goal: *Add CSV export: a GET /api/todos.csv route and an "Export CSV" button that downloads it.* Press **plan** | "A planner splits the work, and decides who owns which files." |
| 0:15 | The split appears: *pc* owns `static/`, *laptop* owns `server/` + `tests/`. Point at the **owns** boxes | "No two agents touch the same file, so the merge stays clean." |
| 0:22 | **start team**. Two VS Code windows open, each on its own worktree (`todo-app-agents\pc`, `...\laptop`) | "Each agent gets its own git worktree and branch." |
| 0:28 | Paste the starter prompt (Ctrl+V, Enter) into each Cline | |
| 0:35 | Back to 7:team (speed up 4x in editing): both panels fill with steps: *read_file*, *write_to_file*, tokens, % cached | "You watch both at once: every step, every file, how much came from the cache." |
| 0:50 | A handoff note appears: *laptop: the route is /api/todos.csv, Content-Type text/csv* | "When one agent decides something the other needs, it leaves a handoff note." |
| 0:55 | When each Cline says it's done, press **finished** on its panel; *tests passed* appears | "Its work is committed on its branch and the tests run in its own copy." |
| 1:05 | **diff** on one panel (scroll briefly), then **merge** on both | "I review each branch and merge. Nothing merges or pushes by itself." |
| 1:15 | Open the app in the browser, click **Export CSV**, the file downloads | "Two agents, one feature, about N minutes, on my own machines." |
| 1:20 | **clean up…** → confirm | |

Variations: with Claude Code installed, add a headless agent (**agents → + Claude Code agent**, model *this pc*) and
show a three-agent split; there are no extra windows, its steps just appear in its panel.

## If something goes wrong on camera

- **A panel says "waiting for Cline"**: the starter prompt wasn't pasted into that window yet, or that Cline isn't
  routed (5:cline).
- **"changed files it doesn't own"**: say so, it's a good moment for the demo; **send back…** with a note such as
  *only edit static/*.
- **Merge conflict**: the app aborts it and leaves everything as it was. **send back…** or merge by hand.
- **Start over**: **discard** both, **clean up…**, then plan again. Your `main` branch is untouched until you merge.
