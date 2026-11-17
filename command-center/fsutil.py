"""Small file helpers shared by the command center's modules.

Everything personal (settings, chats, cloud keys, token counters) is written through write_json: to a temp file
first, then renamed over the old one, so a crash or a full disk mid-write never leaves a half-written file.
"""
import json
import os
import threading
import time

_seq = 0
_seq_lock = threading.Lock()


def replace(tmp, path, tries=8):
    """os.replace that waits out a reader. On Windows, renaming over a file that another thread (or a virus
    scanner, or a backup tool) has open fails with PermissionError; a moment later it works."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02 * (i + 1))


def write_json(path, data, **dump_args):
    """Atomically write `data` as JSON to `path`. Raises OSError if it can't (the old file is left untouched)."""
    global _seq
    with _seq_lock:
        _seq += 1
        n = _seq
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), n)   # unique, so two writers never share a temp file
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_args)
            f.flush()
            os.fsync(f.fileno())
        replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_log(path, text, max_bytes=256 * 1024):
    """Append to a small diagnostic log, starting over when it grows past max_bytes."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
