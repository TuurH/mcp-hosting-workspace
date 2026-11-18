"""What is going on in the workspace right now, and who started it.

Several chats (Claude, ChatGPT, Codex) can use this server at the same time and
none of them sees the others. These helpers record who started a command or
job, read the state of the GPU, and remember the answer to a request so that a
repeated call after a broken connection does not start the same work twice.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import weakref
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import settings

# Seen in the User-Agent the client sends. The first match wins.
CLIENTS = (("codex", "Codex"), ("claude", "Claude"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"))
REMEMBER_SECONDS = 86_400


def caller(ctx: Any) -> dict[str, Any]:
    """Who made this call, as far as the request says. The client name comes
    from a header the client sends itself: useful to tell chats apart, not proof."""
    agent = ""
    try:
        agent = str((ctx.headers or {}).get("user-agent") or "")[:160]
    except Exception:
        pass
    low = agent.lower()
    who: dict[str, Any] = {"client": next((name for key, name in CLIENTS if key in low), "unknown"),
                           "user_agent": agent}
    try:
        claims = ctx.request_context.request.scope.get("state", {}).get("cf_access_claims") or {}
        if claims.get("email"):
            who["login"] = str(claims["email"])
    except Exception:
        pass
    return who


# ---------------------------------------------------------------- running commands
_lock = threading.Lock()
_running: dict[str, dict[str, Any]] = {}
_recent: deque[dict[str, Any]] = deque(maxlen=20)


@contextmanager
def track(command: str, cwd: str, started_by: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Register a workspace_exec call while it runs."""
    entry = {"id": uuid.uuid4().hex[:8], "command": command[:300], "cwd": cwd, "started_at": time.time(),
             "started_by": started_by or {"client": "unknown"}}
    with _lock:
        _running[entry["id"]] = entry
    try:
        yield entry
    finally:
        with _lock:
            _running.pop(entry["id"], None)
            _recent.appendleft({**entry, "finished_at": time.time()})


def commands() -> dict[str, list[dict[str, Any]]]:
    now = time.time()
    with _lock:
        running = [{**entry, "running_seconds": round(now - entry["started_at"])} for entry in _running.values()]
        recent = [dict(entry) for entry in _recent if now - entry["finished_at"] < 900]
    return {"running": sorted(running, key=lambda entry: entry["started_at"]), "finished_last_15_minutes": recent}


# ---------------------------------------------------------------- GPU
_gpu_cache: tuple[float, dict[str, Any]] | None = None


def _query(arguments: list[str]) -> list[list[str]]:
    result = subprocess.run(["nvidia-smi", *arguments, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=8, check=True)
    return [[cell.strip() for cell in line.split(",")] for line in result.stdout.splitlines() if line.strip()]


def gpu_state(max_age: float = 5.0) -> dict[str, Any]:
    """Memory and load of the GPU as nvidia-smi reports them inside this container."""
    global _gpu_cache
    now = time.monotonic()
    if _gpu_cache and now - _gpu_cache[0] < max_age:
        return dict(_gpu_cache[1])
    try:
        gpus = []
        for row in _query(["--query-gpu=index,name,memory.used,memory.total,utilization.gpu"]):
            index, name, used, total, load = row[:5]
            gpus.append({"index": int(index), "name": name, "memory_used_mb": int(float(used)),
                         "memory_total_mb": int(float(total)),
                         "memory_free_mb": int(float(total)) - int(float(used)),
                         "utilization_percent": int(float(load)) if load.replace(".", "").isdigit() else None})
        state: dict[str, Any] = {"available": bool(gpus), "gpus": gpus}
        if not gpus:
            state["reason"] = "nvidia-smi reports no GPU"
    except FileNotFoundError:
        state = {"available": False, "gpus": [], "reason": "nvidia-smi is not installed in this container"}
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        state = {"available": False, "gpus": [], "reason": f"nvidia-smi failed ({exc.__class__.__name__})"}
    _gpu_cache = (now, state)
    return dict(state)


def gpu_free_mb() -> int | None:
    """Free memory on the fullest-free GPU, or None when it cannot be read."""
    gpus = gpu_state(max_age=2.0).get("gpus") or []
    return max((gpu["memory_free_mb"] for gpu in gpus), default=None)


# ---------------------------------------------------------------- repeated requests
_request_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _request_file(tool: str, request_id: str) -> Path:
    digest = hashlib.sha256(f"{tool}\0{request_id}".encode()).hexdigest()
    return settings.state_dir / "requests" / f"{digest}.json"


def once(tool: str, request_id: str | None, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run work once per request_id. A repeat within a day gets the first
    answer back, marked "repeated", and nothing is started again."""
    if not request_id:
        return work()
    request_id = str(request_id)
    if not 8 <= len(request_id) <= 128:
        raise ValueError("request_id must be 8 to 128 characters; use a fresh random value for each new action")
    path = _request_file(tool, request_id)
    with _lock:
        lock = _request_locks.setdefault(path.name, threading.Lock())
    with lock:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(stored["time"]) < REMEMBER_SECONDS:
                return {**stored["result"], "repeated": True,
                        "repeated_note": "This request_id was already used: nothing was started again. "
                                         "This is the answer to the first call."}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        result = work()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _forget_old(path.parent)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"time": time.time(), "tool": tool, "result": result}), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError):
            pass  # the work is done; only the memory of it is missing
        return result


def _forget_old(directory: Path) -> None:
    limit = time.time() - REMEMBER_SECONDS
    for entry in directory.glob("*.json"):
        try:
            if entry.stat().st_mtime < limit:
                entry.unlink()
        except OSError:
            pass
