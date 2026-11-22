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
    exe = claude_exe()
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
                    "goal": str(t.get("goal") or "").strip()[:4000], "owns": list(dict.fromkeys(clean))})
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


def edit_board(repo, registry, goal, tasks, planner=None):
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
    return True, "saved", save_board(repo, new_board(repo, goal, clean, planner))


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
             "## Your part: %s" % task["title"], task.get("goal") or "", "",
             "## Files you own",
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
