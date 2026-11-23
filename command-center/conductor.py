"""Conductor: Claude, on your own Claude Code login, directs the local agents of 7:team.

Claude plans the split (with step-by-step goals, since the workers are smaller local models), then checks every part
an agent finishes: its diff, its test result and its handoff notes. It approves the part, or sends it back to the same
agent with exact fixes (at most MAX_ROUNDS times per task), or gives up and says why. When every part is approved it
does one last check of all parts together, and can send a part back once more if they don't fit. It never merges:
merging stays with you.

How Claude is called: the Claude Code CLI, headless (`claude -p`), with every tool turned off, so it only reads what
is sent to it and only answers with JSON. Its environment has no ANTHROPIC_* variables, so it uses the login you use
for Claude Code (your plan's usage, no per-token bill). The command line is fixed (model from MODELS, prompts and
schemas from this file); the goal, the code and the diffs go in on stdin. What is sent: the goal, the repo's file
list, the README's start, the tracked text files when the whole repo is small (SMALL_REPO), and each agent's diff and
test output. Files that look like secrets are never sent.
"""
import json
import os
import subprocess
import threading
import time

import team
import workspaces

MODELS = ("sonnet", "opus", "haiku")
DEFAULT_MODEL = "sonnet"
MAX_ROUNDS = 3          # how many times Claude may send one task back
MAX_FINAL = 2           # whole-feature checks per run
MAX_DIFF = 60000        # characters of one agent's diff Claude sees
MAX_FINAL_DIFF = 120000
SMALL_REPO = 60000      # a repo whose tracked text files add up to less than this is sent whole with the plan
TIMEOUT = 300
MAX_CALLS = 40          # calls kept in the board's log
SECRETISH = ("api-key", "apikey", "secret", "password", "credential", "token", ".env", ".pem", ".key", ".pfx",
             ".p12", "id_rsa", "id_ed25519", ".kdbx", "settings.local")

PLAN_SCHEMA = team.PLAN_SCHEMA

PLAN_SYSTEM = team.PLAN_PROMPT + """

You are Claude, conducting. The agents are smaller local models (about 27-35B parameters) running on the owner's own
machines. They follow precise instructions well and make poor design decisions alone, so the goal of each task must
read like a work order:
1. Start with one sentence on what the part is for.
2. Then numbered steps, each one concrete: the file, the function or route or element, its exact name, signature or
   JSON shape, and what it does. Name what the other agents' parts provide and expect, with the same names.
3. End with a line "Done when:" followed by checks the agent can verify itself (it runs, a test passes, a page shows X).
Keep each goal under 3500 characters. Don't ask an agent to merge, push or switch branches."""

REVIEW_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["approve", "send_back", "give_up"]},
    "note": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["verdict", "note", "summary"]}

REVIEW_SYSTEM = """You are Claude, the lead engineer directing smaller local coding models that each build one part of a \
feature in their own copy of a repository. One agent says its part is finished. You get its task, its diff against the \
starting commit, the project's test result on its branch and the team's handoff notes. Decide:

- approve: the part does what its task says, the "Done when" checks hold as far as the diff shows, it stays in the \
files it owns, and the tests pass (or a failure is clearly outside this part and was there before). Small style issues \
are not a reason to send it back.
- send_back: something needed is missing or wrong. Write `note` as a work order the agent can carry out alone: \
numbered, concrete (file, function, what to change and to what), at most 8 items, only about this part and only \
inside the files it owns. If a fix needs another agent's file, say what the agent should write as a handoff note instead.
- give_up: this agent can't get it right (the same mistake came back after your feedback), or the task itself is \
wrong or impossible as split. Explain in `note` what the owner should do.

`summary`: one or two plain sentences for the human owner: what the part does now and your verdict. Return JSON only."""

FINAL_SCHEMA = {"type": "object", "properties": {
    "ok": {"type": "boolean"}, "summary": {"type": "string"},
    "fixes": {"type": "array", "items": {"type": "object", "properties": {
        "agent": {"type": "string"}, "note": {"type": "string"}}, "required": ["agent", "note"]}}},
    "required": ["ok", "summary", "fixes"]}

FINAL_SYSTEM = """You are Claude, the lead engineer. Every part of a feature has been built by a separate agent on its own \
branch and you approved each part on its own. They are about to be merged together. Check that they fit: the names, \
routes, JSON shapes, imports and file paths one part uses must match what the other part provides, and nothing the \
feature needs is missing between them.

If they fit, ok=true, fixes=[]. If not, ok=false and one fix per agent that must change, as a numbered work order inside \
the files that agent owns. `summary`: two or three plain sentences for the human owner. Return JSON only."""

RUNNER = None           # the tests put a fake Claude here
_busy = [0]
_busy_lock = threading.Lock()
_locks = {}
_locks_lock = threading.Lock()


def busy():
    return _busy[0] > 0


def _repo_lock(repo):
    with _locks_lock:
        return _locks.setdefault(os.path.normcase(repo), threading.Lock())


def model(settings):
    m = (settings.get("conductor") or {}).get("model")
    return m if m in MODELS else DEFAULT_MODEL


def command(exe, mdl, schema, system):
    """Fixed: no tools, no MCP servers, no saved session, the model from MODELS, the prompts from this file."""
    return [exe, "-p", "--tools", "", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
            "--model", mdl, "--json-schema", json.dumps(schema), "--system-prompt", system]


def info(settings):
    """For the page: is Claude Code there to conduct, and with which model."""
    return {"available": bool(team.claude_bin()), "model": model(settings), "models": list(MODELS),
            "max_rounds": MAX_ROUNDS}


def state(board):
    return board.setdefault("conductor", {"on": False})


def set_on(repo, on, mdl=None):
    with team._board_lock:
        b = team.load_board(repo)
        if not b:
            return False, "plan or write a split first", None
        c = state(b)
        c["on"] = bool(on)
        if mdl in MODELS:
            c["model"] = mdl
        team.save_board(repo, b)
    return True, ("Claude conducts this team: it checks every finished part" if on else "Claude stops checking: you review"), b


def _log(repo, entry):
    """One Claude call into the board: the newest MAX_CALLS calls and running totals."""
    with team._board_lock:
        b = team.load_board(repo)
        if not b:
            return
        c = state(b)
        c["calls"] = (c.get("calls") or [])[-(MAX_CALLS - 1):] + [entry]
        s = c.setdefault("stats", {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "secs": 0.0})
        s["calls"] += 1
        for k in ("in", "out"):
            s[k] += int(entry.get(k) or 0)
        s["cost"] = round(s.get("cost", 0) + float(entry.get("cost") or 0), 4)
        s["secs"] = round(s.get("secs", 0) + float(entry.get("secs") or 0), 1)
        team.save_board(repo, b)


def _activity(repo, text):
    with team._board_lock:
        b = team.load_board(repo)
        if b:
            state(b)["doing"] = text
            team.save_board(repo, b)


def ask(cwd, mdl, schema, system, text, timeout=TIMEOUT, runner=None):
    """One headless Claude call. -> (ok, parsed JSON or error message, usage dict). `runner` replaces subprocess.run
    in the tests."""
    exe = team.claude_bin()
    if not exe:
        return False, "Claude Code isn't installed (npm install -g @anthropic-ai/claude-code)", {}
    os.makedirs(cwd, exist_ok=True)
    t0 = time.time()
    try:
        p = (runner or RUNNER or subprocess.run)(command(exe, mdl, schema, system), input=text.encode("utf-8"), cwd=cwd,
                                       env=team.claude_env(), capture_output=True, timeout=timeout,
                                       creationflags=team.NOWIN)
    except subprocess.TimeoutExpired:
        return False, "Claude didn't answer within %d s" % timeout, {"secs": round(time.time() - t0, 1)}
    except OSError as e:
        return False, "couldn't start Claude Code: %s" % e, {}
    secs = round(time.time() - t0, 1)
    try:
        j = json.loads((p.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        j = None
    if not isinstance(j, dict):
        err = ((p.stderr or b"").decode("utf-8", "replace").strip() or (p.stdout or b"").decode("utf-8", "replace").strip())
        return False, "Claude Code failed (exit %s): %s" % (p.returncode, err[-300:] or "no output"), {"secs": secs}
    u = j.get("usage") or {}
    usage = {"secs": secs, "out": u.get("output_tokens") or 0, "cost": j.get("total_cost_usd"),
             "in": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)}
    if j.get("is_error"):
        return False, "Claude: %s" % str(j.get("result") or j.get("subtype") or "error")[:300], usage
    out = j.get("structured_output")
    if not isinstance(out, dict):
        out = team.parse_plan(j.get("result") or "")
    if not isinstance(out, dict):
        return False, "Claude's answer had no JSON in it", usage
    return True, out, usage


def _cwd(repo):
    """An empty folder for the calls, so Claude Code finds no project instructions to load."""
    return os.path.join(team.git_dir(repo), "cc-team", "conductor")


def _secretish(path):
    p = path.lower().replace("\\", "/")
    return any(s in p.rsplit("/", 1)[-1] for s in SECRETISH)


def repo_files(repo, limit=SMALL_REPO):
    """The tracked text files, whole, when they add up to less than `limit` characters; else ''."""
    _, out, _ = workspaces.run([workspaces.GIT, "-C", repo, "ls-files"], timeout=30)
    parts, total = [], 0
    for f in [l for l in out.splitlines() if l.strip()]:
        if _secretish(f):
            continue
        p = os.path.join(repo, f)
        try:
            if os.path.getsize(p) > limit:
                return ""
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue   # binary
        text = data.decode("utf-8", "replace")
        total += len(text)
        if total > limit:
            return ""
        parts.append("=== %s ===\n%s" % (f, text))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- plan
def plan(repo, goal, registry, mdl, runner=None):
    """Claude splits the goal. -> (ok, plan dict or message, usage)."""
    msgs = team.plan_messages(goal, registry, repo)
    text = msgs[1]["content"]
    whole = repo_files(repo)
    if whole:
        text += "\n\nTHE REPO'S FILES (the whole repo is small, so here it is):\n" + whole
    return ask(_cwd(repo), mdl, PLAN_SCHEMA, PLAN_SYSTEM, text, runner=runner)


def log_plan(repo, usage, mdl, n):
    """The plan's call goes into the log once its board exists."""
    _log(repo, dict(usage, t=time.time(), kind="plan", model=mdl, verdict="%d tasks" % n))


# ---------------------------------------------------------------- review one part
def rounds(task):
    """How often the conductor has sent this task back."""
    return sum(1 for r in task.get("reviews") or [] if r.get("verdict") == "send_back" and r.get("sent"))


def review_text(board, task, d):
    others = [t for t in board["tasks"] if t["id"] != task["id"] and t.get("status") != "discarded"]
    notes = sorted((n for t in board["tasks"] for n in t.get("notes") or []), key=lambda n: n.get("t", 0))
    test = task.get("test") or {}
    files = team.changed_files(task, board.get("base_sha"))
    out = team.outside(files, task["owns"])
    lines = ["FEATURE:", board.get("goal", ""), "",
             "THIS PART (agent %s, round %d of at most %d):" % (task["agent"], rounds(task) + 1, MAX_ROUNDS + 1),
             "Title: " + task["title"], task.get("goal") or "", "",
             "FILES IT OWNS: " + ", ".join(task["owns"]), "",
             "OTHER AGENTS:"] + ["- %s (%s): %s; owns %s" % (t["agent"], t.get("status"), t["title"], ", ".join(t["owns"]))
                                 for t in others] + ["", "HANDOFF NOTES:"] + \
            ["- %s: %s" % (n.get("by"), n.get("text")) for n in notes[-30:]] + ["", "WHAT THE AGENT SAID AT THE END:",
            str(task.get("message") or "(nothing)")[:1500], "",
            "TESTS: %s%s" % ({"passed": "passed", "failed": "FAILED", "none": "this project has no tests"}.get(test.get("state"), "not run"),
                             (" (" + test["cmd"] + ")") if test.get("cmd") else "")]
    if test.get("tail"):
        lines += ["Test output (end):", test["tail"][-2500:]]
    if out:
        lines += ["", "CHANGED FILES IT DOESN'T OWN: " + ", ".join(out)]
    prev = [r for r in task.get("reviews") or [] if r.get("verdict") == "send_back"]
    if prev:
        lines += ["", "YOUR EARLIER FEEDBACK TO THIS AGENT:"] + ["Round %d: %s" % (i + 1, r.get("note")) for i, r in enumerate(prev)]
    diff_text = (d or {}).get("diff") or ""
    lines += ["", "DIFF STAT:", (d or {}).get("stat") or "(no changes)", "", "DIFF:",
              diff_text[:MAX_DIFF] + ("\n... (cut off here: the diff is longer)" if len(diff_text) > MAX_DIFF else "")]
    return "\n".join(lines)


def review(repo, task_id, send_back, mdl=None, runner=None):
    """Claude checks one finished part and acts on its verdict. `send_back(repo, task_id, note)` -> (ok, msg) is the
    command center's own send-back (it restarts the agent). Runs in the caller's thread; one at a time per repo."""
    with _busy_lock:
        _busy[0] += 1
    try:
        with _repo_lock(repo):
            b = team.load_board(repo)
            c = (b or {}).get("conductor") or {}
            t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
            if not t or not c.get("on") or t.get("status") != "done":
                return None
            mdl = mdl or c.get("model") or DEFAULT_MODEL
            _activity(repo, "checking %s's part" % t["agent"])
            ok, out, usage = ask(_cwd(repo), mdl, REVIEW_SCHEMA, REVIEW_SYSTEM, review_text(b, t, team.diff(repo, task_id)), runner=runner)
            verdict = out.get("verdict") if ok and out.get("verdict") in ("approve", "send_back", "give_up") else None
            _log(repo, dict(usage, t=time.time(), kind="review", task=task_id, agent=t["agent"], model=mdl,
                            **({"verdict": verdict} if verdict else {"error": out if not ok else "no verdict in the answer"})))
            rec = {"t": time.time(), "verdict": verdict or "error", "note": str(out.get("note") or "")[:team.MAX_NOTE] if ok else "",
                   "summary": str(out.get("summary") or "")[:600] if ok else str(out)[:300]}
            if verdict == "send_back" and rounds(t) >= MAX_ROUNDS:
                rec["verdict"], rec["out_of_rounds"] = "give_up", True
                rec["summary"] = ("Sent back %d times and still not right: over to you. " % MAX_ROUNDS) + rec["summary"]
            # the board may have changed during the call (you may have merged, discarded or stopped the conductor)
            with team._board_lock:
                b = team.load_board(repo)
                t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
                if not t:
                    return rec
                go = rec["verdict"] == "send_back" and t.get("status") == "done" and state(b).get("on")
                rec["sent"] = bool(go)
                t["reviews"] = (t.get("reviews") or [])[-9:] + [rec]
                if rec["verdict"] != "approve":
                    state(b).pop("final", None)
                state(b)["doing"] = None
                team.save_board(repo, b)
            if go:
                ok2, msg = send_back(repo, task_id, rec["note"])
                if not ok2:
                    _mark(repo, task_id, sent=False, summary=rec["summary"] + " (couldn't send it back: %s)" % msg)
            else:
                final_check(repo, send_back, mdl, runner)
            return rec
    finally:
        with _busy_lock:
            _busy[0] -= 1
        try:
            _activity(repo, None)
        except OSError:
            pass


def _mark(repo, task_id, **fields):
    with team._board_lock:
        b = team.load_board(repo)
        t = next((x for x in (b or {}).get("tasks", []) if x["id"] == task_id), None)
        if t and t.get("reviews"):
            t["reviews"][-1].update(fields)
            team.save_board(repo, b)


def approved(t):
    r = (t.get("reviews") or [{}])[-1]
    return t.get("status") == "merged" or (t.get("status") == "done" and r.get("verdict") == "approve")


# ---------------------------------------------------------------- all parts together
def final_check(repo, send_back, mdl, runner=None):
    """When every part that's still in is approved: one check that they fit together (at most MAX_FINAL per run)."""
    b = team.load_board(repo)
    if not b:
        return None
    c = state(b)
    live = [t for t in b["tasks"] if t.get("status") != "discarded"]
    if not c.get("on") or c.get("final") or not live or not all(approved(t) for t in live):
        return None
    if len(live) < 2 or c.get("finals", 0) >= MAX_FINAL:
        with team._board_lock:
            b = team.load_board(repo)
            state(b)["final"] = {"t": time.time(), "ok": True, "summary": "Every part is approved: review and merge them." if len(live) < 2
                                 else "Every part is approved (the whole-feature check already ran %d times)." % MAX_FINAL}
            team.save_board(repo, b)
        return state(b)["final"]
    _activity(repo, "checking that the parts fit together")
    parts, room = [], MAX_FINAL_DIFF
    for t in live:
        d = team.diff(repo, t["id"]) or {}
        body = (d.get("diff") or "")[:max(room // max(1, len(live)), 4000)]
        parts.append("=== AGENT %s: %s ===\nOwns: %s\nTask:\n%s\n\nDiff:\n%s" % (t["agent"], t["title"], ", ".join(t["owns"]), t.get("goal") or "", body))
    notes = [n for t in b["tasks"] for n in t.get("notes") or []]
    text = "FEATURE:\n%s\n\nHANDOFF NOTES:\n%s\n\n%s" % (b.get("goal", ""), "\n".join("- %s: %s" % (n.get("by"), n.get("text")) for n in notes[-30:]) or "(none)",
                                                       "\n\n".join(parts))
    ok, out, usage = ask(_cwd(repo), mdl, FINAL_SCHEMA, FINAL_SYSTEM, text, runner=runner)
    _log(repo, dict(usage, t=time.time(), kind="final", model=mdl,
                    **({"verdict": "fits" if out.get("ok") else "%d fixes" % len(out.get("fixes") or [])} if ok else {"error": out})))
    names = {t["agent"]: t for t in live}
    fixes = [f for f in (out.get("fixes") or []) if isinstance(f, dict) and f.get("agent") in names and str(f.get("note") or "").strip()] if ok else []
    with team._board_lock:
        b = team.load_board(repo)
        c = state(b)
        c["finals"] = c.get("finals", 0) + 1
        c["final"] = {"t": time.time(), "ok": bool(ok and out.get("ok") and not fixes),
                      "summary": str(out.get("summary") or "")[:800] if ok else "the whole-feature check failed: %s" % str(out)[:300],
                      "fixes": [f["agent"] for f in fixes]}
        team.save_board(repo, b)
    for f in fixes:
        t = names[f["agent"]]
        if t.get("status") != "done":
            continue   # already merged: the fix is yours
        if rounds(t) >= MAX_ROUNDS:
            continue
        with team._board_lock:
            b = team.load_board(repo)
            tt = next(x for x in b["tasks"] if x["id"] == t["id"])
            tt["reviews"] = (tt.get("reviews") or [])[-9:] + [{"t": time.time(), "verdict": "send_back", "sent": True,
                                                                "note": str(f["note"])[:team.MAX_NOTE],
                                                                "summary": "Sent back after the whole-feature check: the parts don't fit yet."}]
            state(b).pop("final", None)
            team.save_board(repo, b)
        send_back(repo, t["id"], str(f["note"]))
    return out
