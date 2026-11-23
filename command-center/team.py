"""Agent team: several coding agents on one project at once, each in its own git worktree, coordinated here.

This file holds the agent registry. An agent is a name plus
  backend  which model it thinks with: "pc" (llama-server on :8090), "laptop" (PAIR on :8080) or
           "cloud:anthropic" (Claude, billed to your key; only for the claude-code client, off unless you add one)
  client   what does the work: "vscode" (Cline in one of the two VS Code setups, as in 2:code) or
           "claude-code" (the Claude Code CLI, run headless by the command center)

The registry lives in settings.json -> "agents". Without one, the two agents of "open · both" are used:
pc (main VS Code, this PC's model) and laptop (the laptop VS Code, the laptop's model).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import fsutil
import workspaces

BACKENDS = ("pc", "laptop", "cloud:anthropic")
CLIENTS = ("vscode", "claude-code")
VSCODE_SETUPS = ("pc", "laptop")      # a vscode agent is one of the two VS Code setups; its name says which
MAX_AGENTS = 6
DEFAULT_AGENTS = [{"name": "pc", "backend": "pc", "client": "vscode"},
                  {"name": "laptop", "backend": "laptop", "client": "vscode"}]
NOWIN = 0x08000000


def agents(settings):
    """The registry, always valid (a broken or missing list falls back to the default)."""
    ok, _, clean = check_agents(settings.get("agents"))
    return clean if ok else [dict(a) for a in DEFAULT_AGENTS]


def check_agents(lst):
    """-> (ok, message, cleaned list). Unknown keys are dropped; names, backends and clients come from fixed sets."""
    if not isinstance(lst, list) or not lst:
        return False, "at least one agent is needed", None
    if len(lst) > MAX_AGENTS:
        return False, "at most %d agents" % MAX_AGENTS, None
    out, names = [], set()
    for a in lst:
        if not isinstance(a, dict):
            return False, "each agent is an object", None
        name, backend, client = a.get("name"), a.get("backend"), a.get("client")
        if not isinstance(name, str) or not workspaces.AGENT_NAME.fullmatch(name):
            return False, "agent names are 1-20 lower-case letters, digits and dashes", None
        if name in names:
            return False, "two agents are called %s" % name, None
        if backend not in BACKENDS:
            return False, "%s: backend must be one of %s" % (name, ", ".join(BACKENDS)), None
        if client not in CLIENTS:
            return False, "%s: client must be one of %s" % (name, ", ".join(CLIENTS)), None
        if client == "vscode":
            if name not in VSCODE_SETUPS:
                return False, ("%s: a VS Code agent is one of the two VS Code setups, so it must be called pc "
                               "(main VS Code) or laptop (the laptop VS Code)" % name), None
            if backend.startswith("cloud:"):
                return False, "%s: Cline in VS Code uses this PC's or the laptop's model" % name, None
        names.add(name)
        out.append({"name": name, "backend": backend, "client": client})
    return True, "saved", out


def save_agents(settings, save, lst):
    ok, msg, clean = check_agents(lst)
    if ok:
        settings["agents"] = clean
        save()
    return ok, msg, clean


_cli = {"t": 0, "v": None}


def claude_exe():
    return shutil.which("claude")


def clients():
    """What is installed, checked rather than assumed (cached for a minute)."""
    if _cli["v"] and time.time() - _cli["t"] < 60:
        return _cli["v"]
    v = {"vscode": {"installed": os.path.exists(workspaces.CODE_CMD), "laptop_setup": os.path.exists(workspaces.LAPTOP_BAT)},
         "claude-code": {"installed": False, "version": None}}
    exe = claude_bin()
    if exe:
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, creationflags=NOWIN,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            ver = (p.stdout or "").strip().split()
            v["claude-code"] = {"installed": p.returncode == 0, "version": ver[0] if ver else None}
        except (OSError, subprocess.TimeoutExpired):
            pass
    _cli.update(t=time.time(), v=v)
    return v


def public(settings):
    """GET /api/team (phone too: nothing here is secret)."""
    return {"agents": agents(settings), "default": not isinstance(settings.get("agents"), list),
            "backends": list(BACKENDS), "clients": clients(), "max": MAX_AGENTS}


# ================================================================ task board
# One board per repo, in the repo's own .git folder (shared by all its worktrees, never committed):
#   <git common dir>\cc-team\board.json
# {goal, base, base_sha, planner, state: draft|running|review|done, created, updated,
#  tasks: [{id, agent, title, slug, goal, owns: [paths], status, branch, worktree, notes: [{t, by, text}], ...}]}
TASK_STATES = ("draft", "running", "stopped", "done", "failed", "merged", "discarded")
_board_lock = threading.RLock()
MAX_TASKS = 8
MAX_NOTE = 2000


def git_dir(repo):
    """The repo's shared .git folder (the same for every worktree of it), or None."""
    code, out, _ = workspaces.run([workspaces.GIT, "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    p = out.strip()
    return os.path.normpath(p) if code == 0 and p and os.path.isdir(p) else None


def board_path(repo):
    g = git_dir(repo)
    return os.path.join(g, "cc-team", "board.json") if g else None


def load_board(repo):
    p = board_path(repo)
    b = fsutil.read_json(p) if p else None
    return b if isinstance(b, dict) and isinstance(b.get("tasks"), list) else None


def save_board(repo, board):
    p = board_path(repo)
    if not p:
        raise OSError("not a git repo: " + repo)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    board["updated"] = time.time()
    with _board_lock:
        fsutil.write_json(p, board, indent=1)
    return board


def slugify(text, taken=()):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:32].strip("-") or "task"
    out, n = s, 2
    while out in taken:
        out, n = "%s-%d" % (s, n), n + 1
    return out


def norm_path(p):
    """An owned path as the board stores it: relative to the repo, forward slashes, no '..', no drive. Folders end
    in '/'. None if it can't be one."""
    p = str(p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or re.match(r"^[a-zA-Z]:", p) or any(x in ("..", "", ".") for x in p.rstrip("/").split("/")):
        return None
    if len(p) > 200 or any(c in p for c in '<>|"?*\x00'):
        return None
    return p


def overlaps(a, b):
    """Two owned paths collide if they are the same or one is inside the other (folders end in '/')."""
    a, b = a.lower(), b.lower()
    if a.rstrip("/") == b.rstrip("/"):
        return True
    return (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def check_tasks(tasks, registry):
    """The split as edited by you or written by the planner. -> (ok, message, cleaned tasks, [overlap pairs]).
    Every task needs a known agent, a title and at least one owned path; no two tasks may own the same file."""
    if not isinstance(tasks, list) or not tasks:
        return False, "the split has no tasks", None, []
    if len(tasks) > MAX_TASKS:
        return False, "at most %d tasks" % MAX_TASKS, None, []
    names = {a["name"] for a in registry}
    out, slugs, used = [], set(), set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return False, "task %d isn't an object" % (i + 1), None, []
        agent = t.get("agent")
        if agent not in names:
            return False, "task %d: no agent called %r" % (i + 1, agent), None, []
        if agent in used:
            return False, "%s has two tasks: give each agent one task at a time" % agent, None, []
        used.add(agent)
        title = " ".join(str(t.get("title") or "").split())[:120]
        if not title:
            return False, "task %d needs a title" % (i + 1), None, []
        owns = t.get("owns")
        if isinstance(owns, str):
            owns = re.split(r"[\n,]+", owns)
        owns = [x for x in (owns if isinstance(owns, list) else []) if str(x).strip()][:40]
        clean = [norm_path(x) for x in owns]
        if not clean or None in clean:
            bad = next((x for x, c in zip(owns, clean) if c is None), None)
            why = "owned path %r isn't a path inside the repo" % bad if bad is not None else "list the files or folders it owns"
            return False, "%s: %s" % (title, why), None, []
        slug = slugify(t.get("slug") or title, slugs)
        slugs.add(slug)
        out.append({"id": "t%d" % (i + 1), "agent": agent, "title": title, "slug": slug,
                    "goal": str(t.get("goal") or "").strip()[:6000], "owns": list(dict.fromkeys(clean))})
    clash = [(a, b, pa, pb) for i, a in enumerate(out) for b in out[i + 1:]
             for pa in a["owns"] for pb in b["owns"] if overlaps(pa, pb)]
    if clash:
        a, b, pa, pb = clash[0]
        return (False, "%s and %s both own %s: give it to one of them" % (a["agent"], b["agent"], pa if len(pa) <= len(pb) else pb),
                out, [(a["id"], b["id"], pa, pb) for a, b, pa, pb in clash])
    return True, "ok", out, []


def new_board(repo, goal, tasks, planner=None):
    code, sha, _ = workspaces.run([workspaces.GIT, "-C", repo, "rev-parse", "HEAD"])
    _, br, _ = workspaces.run([workspaces.GIT, "-C", repo, "branch", "--show-current"])
    now = time.time()
    for t in tasks:
        t.update(status="draft", branch=workspaces.agent_branch(t["agent"], t["slug"]), worktree=None, notes=[])
    return {"version": 1, "repo": repo, "goal": goal, "base": br.strip() or None,
            "base_sha": sha.strip() if code == 0 else None, "planner": planner, "state": "draft",
            "created": now, "updated": now, "tasks": tasks}


def editable(board):
    return not board or board.get("state") in ("draft", "done")


def edit_board(repo, registry, goal, tasks, planner=None, conduct=None):
    """Save a split (yours or the planner's). Only while nothing runs: a working agent's files can't change under it."""
    b = load_board(repo)
    if not editable(b):
        return False, "the team is already working on this; stop or finish it first", b
    ok, msg, clean, _ = check_tasks(tasks, registry)
    if not ok:
        return False, msg, b
    goal = str(goal if goal is not None else (b or {}).get("goal") or "").strip()[:4000]
    if not goal:
        return False, "say what the team should build", b
    if planner is None and b and b.get("state") == "draft":
        planner = b.get("planner")
    nb = new_board(repo, goal, clean, planner)
    if conduct is not None or (b or {}).get("conductor"):
        # whether Claude conducts, with which model, and its calls so far (the plan's) outlive an edit of the split
        old = (b or {}).get("conductor") or {}
        nb["conductor"] = {k: old[k] for k in ("model", "calls", "stats") if old.get(k)}
        nb["conductor"]["on"] = bool(old.get("on")) if conduct is None else bool(conduct)
    return True, "saved", save_board(repo, nb)


def add_note(repo, task_id, by, text):
    text = str(text or "").strip()[:MAX_NOTE]
    with _board_lock:
        b = load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
        if not t or not text:
            return False, "no such task" if not t else "empty note"
        t.setdefault("notes", []).append({"t": time.time(), "by": by, "text": text})
        t["notes"] = t["notes"][-50:]
        save_board(repo, b)
    return True, "noted"


# ================================================================ planner
PLAN_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object",
        "properties": {"agent": {"type": "string"}, "title": {"type": "string"}, "goal": {"type": "string"},
                       "owns": {"type": "array", "items": {"type": "string"}}},
        "required": ["agent", "title", "goal", "owns"]}}},
    "required": ["tasks"]}

PLAN_PROMPT = """You are the lead engineer splitting one feature between coding agents that work AT THE SAME TIME, each \
in its own copy of the repository. Their work is merged afterwards, so two agents must never edit the same file.

Write one task per agent (leave an agent out if the feature doesn't need it). For each task:
- agent: one of the agent names given
- title: a short imperative title (under 60 characters)
- goal: what to build, precisely enough to work alone: files to create or change, functions or endpoints, and how \
the pieces meet (names, signatures, JSON shapes), so the parts fit together without the agents talking
- owns: the files and folders this agent may edit, relative to the repo root with forward slashes; folders end in \
"/". New files go in the owner's list too. No path may be inside another agent's path.

Split along existing boundaries (backend / frontend / tests / docs) where you can. If both sides need one thing (a \
shared type, a route name), give it to one agent and describe it exactly in both goals.
Return JSON only: {"tasks": [{"agent": "...", "title": "...", "goal": "...", "owns": ["..."]}]}"""

BACKEND_NAMES = {"pc": "this PC's model", "laptop": "the laptop's model", "cloud:anthropic": "Claude (cloud)"}


def repo_outline(repo, max_files=400, readme_chars=3000):
    """What the planner sees of the repo: the tracked file list and the start of the README. Nothing else."""
    _, out, _ = workspaces.run([workspaces.GIT, "-C", repo, "ls-files"], timeout=30)
    files = [l for l in out.splitlines() if l.strip()]
    listing = "\n".join(files[:max_files]) + ("\n... and %d more files" % (len(files) - max_files) if len(files) > max_files else "")
    readme = ""
    for name in ("README.md", "README.txt", "README", "readme.md"):
        p = os.path.join(repo, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                readme = f.read(readme_chars)
            break
    return listing, readme


def plan_messages(goal, registry, repo):
    listing, readme = repo_outline(repo)
    who = "\n".join("- %s (%s, %s)" % (a["name"], BACKEND_NAMES[a["backend"]], a["client"]) for a in registry)
    user = "AGENTS:\n%s\n\nFEATURE TO BUILD:\n%s\n\nFILES IN THE REPO:\n%s\n\nREADME (start):\n%s" % (
        who, goal.strip(), listing or "(none yet)", readme or "(none)")
    return [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": user}]


def parse_plan(text):
    """The planner's JSON, even with thinking or prose around it."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    a, b = text.find("{"), text.rfind("}")
    try:
        j = json.loads(text[a:b + 1]) if a >= 0 else None
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def planner_order(cloud_ready, use_cloud, pc_on, laptop_on, pick=None):
    """Which backends may plan, best first: cloud only if it is set up AND you ticked it for this plan, then this
    PC, then the laptop. `pick` (pc | laptop | cloud) narrows it to that one."""
    order = (["cloud"] if use_cloud and cloud_ready else []) + (["pc"] if pc_on else []) + (["laptop"] if laptop_on else [])
    return [p for p in order if p == pick] if pick else order


# ================================================================ the brief each agent works from
BRIEF_FILE = ".cc-agent.md"                       # in the agent's worktree, kept out of git via .git/info/exclude
CLINE_RULE = ".clinerules/cc-team.md"             # Cline reads .clinerules/ into every task


def brief(board, task):
    """The task message: this agent's task, the files it owns, what the others are doing, and the handoff notes."""
    me = task["agent"]
    others = [t for t in board["tasks"] if t["id"] != task["id"] and t.get("status") != "discarded"]
    lines = ["# Your task (from the command center's agent team)", "",
             "You are agent **%s**, one of %d coding agents building one feature at the same time, each in its own "
             "copy of the repository. Your work is reviewed and merged afterwards." % (me, len(others) + 1), "",
             "## The feature", board.get("goal", ""), "",
             "## Your part: %s" % task["title"], task.get("goal") or "", ""]
    if task.get("feedback"):
        last = task["feedback"][-1]
        who = "Claude, who conducts this team," if last.get("by") == "conductor" else "The owner"
        lines += ["## Sent back for changes",
                  "%s reviewed your work and sent it back. Do the latest round now; your earlier work is still in "
                  "this folder:" % who, ""]
        for i, f in enumerate(task["feedback"][-3:]):
            latest = i == len(task["feedback"][-3:]) - 1
            lines += ["### %s%s" % ("Latest: " if latest else "Earlier: ", "from Claude" if f.get("by") == "conductor" else "from the owner"),
                      str(f.get("text") or "").strip(), ""]
    lines += ["## Files you own",
             "Edit or create only these. Everything else belongs to another agent or stays as it is:"]
    lines += ["- `%s`" % p for p in task["owns"]]
    lines += ["", "## The other agents"]
    lines += ["- **%s** (%s): %s. Owns %s." % (t["agent"], t.get("status") or "draft", t["title"],
                                                ", ".join("`%s`" % p for p in t["owns"])) for t in others] or ["- none"]
    notes = sorted((n for t in board["tasks"] for n in t.get("notes") or []), key=lambda n: n.get("t", 0))
    lines += ["", "## Handoff notes",
              "If you need a change in a file you don't own, or decide something the others must know (a name, a route, "
              "a JSON shape), don't edit their files: add a line at the end of this section that starts with `- %s:`. "
              "The command center passes it on." % me]
    lines += ["- %s: %s" % (n.get("by"), " ".join(str(n.get("text")).split())) for n in notes[-30:]]
    lines += ["", "## When you're done",
              "Make sure the code runs, then say you're finished. Don't merge, push or switch branches: the owner "
              "reviews every agent's branch and merges it."]
    return "\n".join(lines) + "\n"


STARTER = "Read the file .cc-agent.md in this folder: it is your task in a team of coding agents. Do it."


def claude_bin():
    """The Claude Code program itself (the npm package ships a native claude.exe; the claude.cmd next to it is only a
    wrapper, and starting it would put cmd.exe in between)."""
    w = claude_exe()
    if not w:
        return None
    exe = os.path.join(os.path.dirname(w), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if os.path.isfile(exe) else None


def exclude(repo, names):
    """Keep the command center's files out of git in every worktree of the repo (.git/info/exclude is shared)."""
    g = git_dir(repo)
    if not g:
        return
    p = os.path.join(g, "info", "exclude")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            have = f.read()
    except OSError:
        have = ""
    add = ["/" + n for n in names if "/" + n not in have.splitlines()]
    if add:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(("" if not have or have.endswith("\n") else "\n") + "# command center agent team\n" + "\n".join(add) + "\n")


def _write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def write_brief(board, task, client):
    """The brief in the agent's worktree: .cc-agent.md, plus .clinerules/cc-team.md for Cline (which reads that folder
    into every task by itself). Returns a note if the Cline rule couldn't be written."""
    wt = task.get("worktree")
    if not wt or not os.path.isdir(wt):
        return "no worktree"
    text = brief(board, task)
    _write_if_changed(os.path.join(wt, BRIEF_FILE), text)
    if client == "vscode":
        rules = os.path.join(wt, ".clinerules")
        if os.path.isfile(rules):   # the project keeps its Cline rules in one file: leave it alone
            return ".clinerules is a file in this repo, so the brief is only in .cc-agent.md: tell Cline to read it"
        _write_if_changed(os.path.join(wt, *CLINE_RULE.split("/")),
                          "# Agent team\n\nYou are working as part of an agent team. Your task, the files you own and the "
                          "other agents' status are in `.cc-agent.md` in the project root: read it before anything else "
                          "and follow it.\n")
    return None


def prepare(repo, board, task, client):
    """The agent's own worktree on its task branch, with its brief written. -> (ok, message)."""
    ok, wt, _ = workspaces.agent_worktree(repo, task["agent"], task["slug"], board.get("base_sha"))
    if not ok:
        return False, wt
    task["worktree"] = wt
    # Python's bytecode caches appear as soon as an agent runs the code; in a repo without a .gitignore they would be
    # committed with its work and flagged as files it doesn't own (seen in the live run). Local exclude only.
    exclude(repo, [BRIEF_FILE, CLINE_RULE, "**/__pycache__/", "**/*.pyc"])
    return True, write_brief(board, task, client) or "ready"


# ---------------------------------------------------------------- live state (this process only)
LIVE = {}            # agent -> {"proc", "token", "repo", "task", "steps": [...], "last", "result", "log"}
_live_lock = threading.Lock()
MAX_STEPS = 60


def live(agent):
    with _live_lock:
        return LIVE.setdefault(agent, {"proc": None, "token": None, "repo": None, "task": None, "steps": [], "last": 0,
                                       "result": None})


def running(agent):
    """Is a headless agent's process still going?"""
    p = live(agent)["proc"]
    return p is not None and p.poll() is None


def busy():
    return any(running(a) for a in list(LIVE))


def record_step(repo, task_id, step):
    """One step of an agent (a model request it made) into the board: the newest steps and running totals."""
    step = {k: v for k, v in step.items() if v not in (None, "")}
    step.setdefault("t", time.time())
    with _board_lock:
        b = load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
        if not t:
            return
        t["steps"] = (t.get("steps") or [])[-(MAX_STEPS - 1):] + [step]
        s = t.setdefault("stats", {"steps": 0, "prompt": 0, "cached": 0, "out": 0})
        s["steps"] = s.get("steps", 0) + 1
        for k in ("prompt", "cached", "out"):
            s[k] = s.get(k, 0) + int(step.get(k) or 0)
        if step.get("cost") is not None:
            s["cost"] = round(s.get("cost", 0) + float(step["cost"]), 4)
        t["last_step"] = step["t"]
        save_board(repo, b)


def set_status(repo, task_id, status, **extra):
    with _board_lock:
        b = load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
        if not t:
            return None
        t["status"] = status
        t.update(extra)
        live_states = [x["status"] for x in b["tasks"]]
        if all(s in ("done", "failed", "stopped", "merged", "discarded") for s in live_states):
            b["state"] = "review"
        save_board(repo, b)
        return b


CLAUDE_ALLOW = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "TodoWrite",
                "Bash(python *)", "Bash(py *)", "Bash(pytest *)", "Bash(npm test*)", "Bash(npm run *)",
                "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git add *)", "Bash(git commit *)"]
CLAUDE_DENY = ["Bash(git push*)", "Bash(git merge*)", "Bash(git switch*)", "Bash(git checkout*)", "Bash(git reset*)",
               "Bash(git rebase*)", "Bash(git branch*)", "Bash(git worktree*)", "Bash(git clean*)", "WebFetch", "WebSearch"]


def claude_command(exe):
    """A fixed command line: nothing in it comes from the browser or the goal (the task is in .cc-agent.md)."""
    return ([exe, "-p", STARTER, "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
             "--permission-prompts", "none", "--strict-mcp-config", "--no-session-persistence",
             "--allowedTools"] + CLAUDE_ALLOW + ["--disallowedTools"] + CLAUDE_DENY)


def claude_env(base_url=None, token=None, api_key=None):
    """Claude Code's environment. Local: it talks to the command center's /agent/<name> route with a token made for
    this one run. Cloud: your Claude key from 6:set (or, without one, your own Claude Code login)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if base_url:
        env.update(ANTHROPIC_BASE_URL=base_url, ANTHROPIC_AUTH_TOKEN=token, ANTHROPIC_MODEL="local",
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="local", ANTHROPIC_DEFAULT_SONNET_MODEL="local",
                   ANTHROPIC_DEFAULT_OPUS_MODEL="local", ANTHROPIC_SMALL_FAST_MODEL="local")
    elif api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def _step_from_claude(ev, seen, wt=""):
    """A stream-json event from Claude Code -> a step, or None. Claude Code emits one event per content block of a
    model reply, each carrying the same usage: the tokens count once per reply (message id), and a later block of
    the same reply only adds its tool call. Paths inside the worktree are shown relative to it."""
    if ev.get("type") != "assistant":
        return None
    msg = ev.get("message") or {}
    tools = [c for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"]
    tool = tools[0] if tools else None
    first = msg.get("id") not in seen
    if msg.get("id"):
        seen.add(msg["id"])
    if not first and not tool:
        return None
    inp = (tool or {}).get("input") or {}
    target = str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or "")
    if wt and target.lower().startswith(wt.lower()):
        target = target[len(wt):].lstrip("\\/") or "."
    u = (msg.get("usage") or {}) if first else {}
    return {"tool": tool.get("name") if tool else "reply", "target": target[:160],
            "prompt": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0),
            "cached": u.get("cache_read_input_tokens") or 0, "out": u.get("output_tokens") or 0}


def start_claude(repo, task, base_url=None, token=None, api_key=None, on_exit=None):
    """Run Claude Code headless in the agent's worktree. Its stream of events becomes the step log."""
    exe = claude_bin()
    if not exe:
        return False, "Claude Code isn't installed (npm install -g @anthropic-ai/claude-code)"
    wt, agent = task["worktree"], task["agent"]
    logdir = os.path.join(git_dir(repo), "cc-team", "logs")
    os.makedirs(logdir, exist_ok=True)
    err = open(os.path.join(logdir, agent + ".log"), "w", encoding="utf-8", errors="replace")
    try:
        p = subprocess.Popen(claude_command(exe), cwd=wt, env=claude_env(base_url, token, api_key),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                             creationflags=NOWIN | 0x00000200)
    except OSError as e:
        err.close()
        return False, "couldn't start Claude Code: %s" % e
    L = live(agent)
    L.update(proc=p, token=token, repo=repo, task=task["id"], result=None, last=time.time())

    def read():
        result, seen, outs = None, set(), 0
        try:
            for raw in p.stdout:
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                st = _step_from_claude(ev, seen, wt)
                if st:
                    L["last"] = time.time()
                    outs += st["out"]
                    record_step(repo, task["id"], st)
                if ev.get("type") == "result":
                    result = ev
                    fin = {"tool": "result", "target": str(ev.get("result") or "")[:160]}
                    if not outs:   # the per-reply usage had no output count: take the run's total
                        fin["out"] = (ev.get("usage") or {}).get("output_tokens") or 0
                    if api_key or not base_url:   # a real bill; on a local model Claude Code's price is made up
                        fin["cost"] = ev.get("total_cost_usd")
                    record_step(repo, task["id"], fin)
        except (OSError, ValueError):
            pass
        rc = p.wait()
        err.close()
        L["result"] = result
        b = load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task["id"]), None)
        if t and t.get("status") == "running":
            ok = rc == 0 and result is not None and not result.get("is_error")
            set_status(repo, task["id"], "done" if ok else "failed", finished=time.time(),
                       message=(str((result or {}).get("result") or "")[:600] if ok else
                                "Claude Code stopped (exit %s): %s" % (rc, str((result or {}).get("result") or tail(os.path.join(logdir, agent + ".log")))[:400])))
        if on_exit:
            on_exit(task["id"])

    threading.Thread(target=read, daemon=True).start()
    return True, "Claude Code is working on it"


def tail(path, n=6):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return " | ".join(l.strip() for l in f.readlines()[-n:] if l.strip())
    except OSError:
        return ""


def stop(agent):
    """Stop a headless agent (its whole process tree). Cline in VS Code is stopped by the proxy refusing its next step."""
    p = live(agent)["proc"]
    if p is not None and p.poll() is None:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=20, creationflags=NOWIN)
        return True
    return False


# ---------------------------------------------------------------- what changed in each worktree
_changed = {}


def changed_files(task, base_sha, max_age=5):
    """Files the agent changed against the base commit (committed or not), plus new files. Cached a few seconds."""
    wt = task.get("worktree")
    if not wt or not base_sha or not os.path.isdir(wt):
        return []
    hit = _changed.get(wt)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    files = []
    _, out, _ = workspaces.run([workspaces.GIT, "-C", wt, "diff", "--numstat", base_sha, "--"], timeout=30)
    for l in out.splitlines():
        parts = l.split("\t")
        if len(parts) == 3:
            files.append({"path": parts[2], "add": parts[0], "del": parts[1]})
    _, out, _ = workspaces.run([workspaces.GIT, "-C", wt, "ls-files", "--others", "--exclude-standard"], timeout=30)
    files += [{"path": l.strip(), "add": "new", "del": "0"} for l in out.splitlines() if l.strip()]
    _changed[wt] = (time.time(), files)
    return files


def outside(files, owns):
    """Changed files the agent doesn't own."""
    return [f["path"] for f in files if not any(overlaps(f["path"], o) or (o.endswith("/") and f["path"].lower().startswith(o.lower()))
                                                 for o in owns)]


# ---------------------------------------------------------------- review: commit, test, diff, merge, clean up
def _git(cwd, *args, timeout=60):
    return workspaces.run([workspaces.GIT, "-C", cwd, "-c", "core.quotepath=off"] + list(args), timeout=timeout)


def commit_work(task, why=""):
    """Commit whatever the agent left uncommitted, on its own branch in its own worktree (the brief files are
    excluded, so they never go in). Returns (ok, message)."""
    wt = task.get("worktree")
    if not wt or not os.path.isdir(wt):
        return False, "no worktree"
    if workspaces.is_clean(wt):
        return True, "nothing to commit"
    code, out, err = _git(wt, "add", "-A")
    if code != 0:
        return False, "git add failed: " + (err or out).strip()[:200]
    msg = "%s: %s%s" % (task["agent"], task["title"], (" (" + why + ")") if why else "")
    code, out, err = _git(wt, "-c", "user.name=agent " + task["agent"], "-c", "user.email=agent@command-center.local",
                          "commit", "-q", "--no-verify", "-m", msg)
    if code != 0:
        return False, "git commit failed: " + (err or out).strip()[:200]
    _changed.pop(wt, None)
    return True, "committed"


def test_command(wt):
    """The project's own tests, as a fixed command (nothing here comes from the browser), or None."""
    py = shutil.which("python") or shutil.which("py")
    if os.path.isfile(os.path.join(wt, "tests", "smoke.py")) and py:
        return [py, os.path.join("tests", "smoke.py")]
    has_pytest = any(os.path.isfile(os.path.join(wt, f)) for f in ("pytest.ini", "conftest.py", "tox.ini"))
    if not has_pytest and os.path.isfile(os.path.join(wt, "pyproject.toml")):
        with open(os.path.join(wt, "pyproject.toml"), encoding="utf-8", errors="replace") as f:
            has_pytest = "[tool.pytest" in f.read()
    if not has_pytest:
        for d in ("tests", "test"):
            p = os.path.join(wt, d)
            if os.path.isdir(p) and any(n.startswith("test_") and n.endswith(".py") for n in os.listdir(p)):
                has_pytest = True
    if has_pytest and py:
        if _has_pytest(py):
            return [py, "-m", "pytest", "-q"]
        # pytest isn't installed (live run: "No module named pytest" reported as failed tests): run the test
        # files as plain scripts when there is one, else unittest's discovery
        found = sorted(os.path.join(d, n) for d in ("tests", "test") if os.path.isdir(os.path.join(wt, d))
                       for n in os.listdir(os.path.join(wt, d)) if n.startswith("test_") and n.endswith(".py"))
        if len(found) == 1:
            return [py, found[0]]
        return [py, "-m", "unittest", "discover", "-s", found[0].split(os.sep)[0] if found else "tests", "-p", "test_*.py"]
    pj = fsutil.read_json(os.path.join(wt, "package.json"))
    npm = shutil.which("npm")
    if isinstance(pj, dict) and isinstance(pj.get("scripts"), dict) and pj["scripts"].get("test") and npm:
        return [npm, "test"]
    return None


_pytest = {}


def _has_pytest(py):
    if py not in _pytest:
        try:
            _pytest[py] = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=30,
                                         creationflags=NOWIN).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _pytest[py] = False
    return _pytest[py]


_test_lock = threading.Lock()   # one test run at a time: projects' tests often use fixed ports


def run_tests(repo, task_id, timeout=900, on_done=None):
    """Run the project's tests in the agent's worktree, in the background; the result goes into the board, then
    on_done(repo, task_id) runs (the conductor's check)."""
    b = load_board(repo)
    t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
    if not t or not t.get("worktree") or not os.path.isdir(t["worktree"]):
        return False, "no worktree to test"
    cmd = test_command(t["worktree"])
    if not cmd:
        set_status(repo, task_id, t["status"], test={"state": "none", "t": time.time(), "msg": "no tests found in this project"})
        if on_done:
            threading.Thread(target=on_done, args=(repo, task_id), daemon=True).start()
        return True, "no tests found in this project"
    label = " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd))
    set_status(repo, task_id, t["status"], test={"state": "running", "cmd": label, "t": time.time()})

    def go():
        with _test_lock:
            t0 = time.time()
            try:
                p = subprocess.run(cmd, cwd=t["worktree"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, creationflags=NOWIN, stdin=subprocess.DEVNULL)
                ok, out = p.returncode == 0, (p.stdout or "") + (p.stderr or "")
            except subprocess.TimeoutExpired:
                ok, out = False, "timed out after %d s" % timeout
            except OSError as e:
                ok, out = False, "couldn't run: %s" % e
        cur = next((x for x in (load_board(repo) or {}).get("tasks", []) if x["id"] == task_id), None)
        if cur:
            set_status(repo, task_id, cur["status"], test={"state": "passed" if ok else "failed", "cmd": label, "t": time.time(),
                                                            "secs": round(time.time() - t0, 1), "tail": out.strip()[-3000:]})
        if on_done:
            on_done(repo, task_id)
    threading.Thread(target=go, daemon=True).start()
    return True, "testing: " + label


def diff(repo, task_id, limit=400000):
    """The agent's whole change against the base commit (committed and not), for review."""
    b = load_board(repo)
    t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
    if not t or not t.get("worktree") or not os.path.isdir(t["worktree"]) or not b.get("base_sha"):
        return None
    wt = t["worktree"]
    _, stat, _ = _git(wt, "diff", "--stat", b["base_sha"], "--")
    _, out, _ = _git(wt, "diff", b["base_sha"], "--", timeout=60)
    _, new, _ = _git(wt, "ls-files", "--others", "--exclude-standard")
    for f in [l for l in new.splitlines() if l.strip()][:50]:
        p = os.path.join(wt, f)
        try:
            if os.path.getsize(p) > 200000:
                out += "\n+++ new file %s (too big to show)\n" % f
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                out += "\n+++ new file %s (not committed yet)\n" % f + "".join("+" + l for l in fh.readlines())
        except OSError:
            continue
    return {"task": task_id, "agent": t["agent"], "branch": t.get("branch"), "stat": stat.strip(),
            "diff": out[:limit], "cut": len(out) > limit}


def merge(repo, task_id):
    """Merge one agent's branch into the base branch in your own checkout: --no-ff, never forced, never pushed.
    Refuses when your checkout has uncommitted changes or isn't on the base branch; a conflict is aborted and
    reported, leaving everything as it was. -> (ok, message)."""
    b = load_board(repo)
    t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
    if not t:
        return False, "no such task"
    if t.get("status") not in ("done", "stopped", "failed"):
        return False, "%s is %s: merge it when it's finished" % (t["agent"], t.get("status"))
    if not t.get("branch") or not t.get("worktree"):
        return False, "this task never started"
    ok, msg = commit_work(t, "left uncommitted, committed at merge")
    if not ok:
        return False, msg
    _, cur, _ = _git(repo, "branch", "--show-current")
    if b.get("base") and cur.strip() != b["base"]:
        return False, "your checkout is on %s, not %s: switch back to %s first" % (cur.strip() or "a detached HEAD", b["base"], b["base"])
    code, st, _ = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if code != 0 or st.strip():
        return False, "your checkout (%s) has uncommitted changes: commit or stash them first" % repo
    _, ahead, _ = _git(repo, "rev-list", "--count", "HEAD.." + t["branch"])
    if ahead.strip() == "0":
        set_status(repo, task_id, "merged", merged=time.time(), message="nothing new to merge")
        return True, "%s had nothing new to merge" % t["agent"]
    code, out, err = _git(repo, "-c", "user.name=command center", "-c", "user.email=agent@command-center.local",
                          "merge", "--no-ff", "--no-edit", "-m", "Merge %s: %s" % (t["branch"], t["title"]), t["branch"], timeout=120)
    if code != 0:
        _, conf, _ = _git(repo, "diff", "--name-only", "--diff-filter=U")
        _git(repo, "merge", "--abort")
        files = [l for l in conf.splitlines() if l.strip()]
        return False, ("merge conflict in %s: nothing was changed. Send it back with a note, or merge by hand" % ", ".join(files[:8])
                       if files else "git merge failed: " + (err or out).strip()[:300])
    _, sha, _ = _git(repo, "rev-parse", "--short", "HEAD")
    set_status(repo, task_id, "merged", merged=time.time(), message="merged into %s as %s" % (b.get("base") or "HEAD", sha.strip()))
    return True, "merged %s into %s (%s). Nothing was pushed" % (t["branch"], b.get("base") or "HEAD", sha.strip())


def send_back(repo, task_id, note, by="owner"):
    """The note (yours, or the conductor's) goes into the brief and the task runs again."""
    note = str(note or "").strip()[:MAX_NOTE]
    if not note:
        return False, "write what should change", None
    with _board_lock:
        b = load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
        if not t or t.get("status") not in ("done", "stopped", "failed"):
            return False, "only a finished, stopped or failed task can be sent back", None
        t.setdefault("feedback", []).append({"t": time.time(), "text": note, "by": by})
        t.update(status="running", finished=None, message=None, test=None, started=time.time())
        b["state"] = "running"
        save_board(repo, b)
    return True, "sent back to %s" % t["agent"], b


def discard(repo, task_id):
    b = load_board(repo)
    t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
    if not t or t.get("status") not in ("done", "stopped", "failed", "draft"):
        return False, "stop it first" if t else "no such task"
    set_status(repo, task_id, "discarded", message="discarded: its branch and folder stay until you clean up")
    return True, "discarded %s's work (the branch %s is kept until you clean up)" % (t["agent"], t.get("branch"))


def cleanup_plan(repo):
    """What clean-up would do: worktrees of merged or discarded tasks (only if they have no uncommitted work) and
    their branches (a merged branch is deleted with git branch -d, which refuses unmerged work; a discarded one
    only when you also tick it, since its commits go with it)."""
    b = load_board(repo)
    out = []
    for t in (b or {}).get("tasks", []):
        if t.get("status") not in ("merged", "discarded"):
            continue
        wt = t.get("worktree")
        have_wt = bool(wt and os.path.isdir(wt))
        out.append({"task": t["id"], "agent": t["agent"], "status": t["status"], "worktree": wt if have_wt else None,
                    "clean": workspaces.is_clean(wt) if have_wt else True, "branch": t.get("branch"),
                    "branch_exists": workspaces._branch_exists(repo, t["branch"]) if t.get("branch") else False})
    return out


def cleanup(repo, drop_discarded=False):
    done, kept = [], []
    b = load_board(repo)
    for c in cleanup_plan(repo):
        if c["worktree"]:
            ok, msg = workspaces.remove_worktree(repo, c["worktree"])
            (done if ok else kept).append(msg)
            if not ok:
                continue
        if c["branch_exists"]:
            if c["status"] == "merged":
                code, out, err = _git(repo, "branch", "-d", c["branch"])
                (done if code == 0 else kept).append(("deleted branch " + c["branch"]) if code == 0 else
                                                     "kept branch %s: %s" % (c["branch"], (err or out).strip()[:150]))
            elif drop_discarded:
                code, out, err = _git(repo, "branch", "-D", c["branch"])
                (done if code == 0 else kept).append(("deleted branch " + c["branch"]) if code == 0 else
                                                     "kept branch %s: %s" % (c["branch"], (err or out).strip()[:150]))
            else:
                kept.append("kept branch %s (discarded work; tick the box to delete it)" % c["branch"])
    ag = workspaces.agents_dir(repo)
    try:
        if os.path.isdir(ag) and not os.listdir(ag):
            os.rmdir(ag)
    except OSError:
        pass
    if b and all(t.get("status") in ("merged", "discarded") for t in b["tasks"]):
        b = load_board(repo)
        b["state"] = "done"
        save_board(repo, b)
    return done, kept


_sync_t = {}


def sync(repo, clients_by_agent, every=4):
    """Pass handoff notes along: read what each working agent appended to its brief, add it to the board, and
    rewrite every working agent's brief (its view of the others' status and notes). At most every few seconds."""
    if time.time() - _sync_t.get(repo, 0) < every:
        return load_board(repo)
    _sync_t[repo] = time.time()
    with _board_lock:
        b = load_board(repo)
        if not b or b.get("state") not in ("running", "review"):
            return b
        changed = False
        for t in b["tasks"]:
            wt = t.get("worktree")
            if t.get("status") not in ("running", "done", "stopped") or not wt:
                continue
            try:
                with open(os.path.join(wt, BRIEF_FILE), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            mine = [n["text"] for n in t.get("notes") or [] if n.get("by") == t["agent"]]
            for note in new_notes(text, t["agent"], mine + [n["text"] for x in b["tasks"] for n in x.get("notes") or []]):
                t.setdefault("notes", []).append({"t": time.time(), "by": t["agent"], "text": note})
                changed = True
        if changed:
            save_board(repo, b)
        for t in b["tasks"]:
            if t.get("status") == "running" and t.get("worktree"):
                try:
                    write_brief(b, t, clients_by_agent.get(t["agent"], "vscode"))
                except OSError:
                    pass
        return b


def new_notes(text, agent, known):
    """Handoff lines an agent added to its brief since the last read: '- <agent>: text' anywhere below
    '## Handoff notes' (agents often append at the very end of the file rather than inside the section)."""
    sec = text.split("## Handoff notes", 1)[1] if "## Handoff notes" in text else ""
    seen = {" ".join(str(k).split()) for k in known}
    out = []
    for l in sec.splitlines():
        m = re.match(r"\s*-\s*%s:\s*(.+)" % re.escape(agent), l)
        if m and " ".join(m.group(1).split()) not in seen:
            out.append(m.group(1).strip()[:MAX_NOTE])
            seen.add(" ".join(m.group(1).split()))
    return out
