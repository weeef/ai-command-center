"""Agent team: several coding agents on one project at once, each in its own git worktree, coordinated here.

This file holds the agent registry. An agent is a name plus
  backend  which model it thinks with: "pc" (llama-server on :8090), "laptop" (PAIR on :8080) or
           "cloud:anthropic" (Claude, billed to your key; only for the claude-code client, off unless you add one)
  client   what does the work: "vscode" (Cline in one of the two VS Code setups, as in 2:code) or
           "claude-code" (the Claude Code CLI, run headless by the command center)

The registry lives in settings.json -> "agents". Without one, the two agents of "open · both" are used:
pc (main VS Code, this PC's model) and laptop (the laptop VS Code, the laptop's model).
"""
import os
import shutil
import subprocess
import time

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
