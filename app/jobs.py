from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from .config import settings
from .execution import workspace_environment
from .job_runner import runner_alive, validate_gpu_capacity
from .paths import relative_workspace_path, workspace_path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _job_dir(job_id: str) -> Path:
    if not job_id or any(character not in "0123456789abcdef" for character in job_id):
        raise ValueError("Invalid job_id")
    directory = settings.jobs_dir / job_id
    if not directory.is_dir():
        raise FileNotFoundError(f"Unknown job: {job_id}")
    return directory


def start_job(
    *,
    command: str,
    cwd: str = ".",
    label: str = "",
    env: dict[str, str] | None = None,
    gpu: bool = False,
    gpu_memory_mb: int | None = None,
    started_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not command.strip():
        raise ValueError("command cannot be empty")
    if gpu_memory_mb is not None:
        gpu_memory_mb = int(gpu_memory_mb)
        if not 1 <= gpu_memory_mb <= 1_000_000:
            raise ValueError("gpu_memory_mb must be the GPU memory the job needs, in MB")
        validate_gpu_capacity(gpu_memory_mb)
        gpu = True
    working_directory = workspace_path(cwd, must_exist=True)
    if not working_directory.is_dir():
        raise NotADirectoryError(working_directory)

    job_id = uuid.uuid4().hex
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True)
    created_at = time.time()
    spec = {
        "job_id": job_id,
        "label": label[:200],
        "command": command,
        "cwd": str(working_directory),
        "env": env or {},
        "created_at": created_at,
        "gpu": bool(gpu),
        "gpu_memory_mb": gpu_memory_mb,
        "started_by": started_by or {"client": "unknown"},
    }
    _write_json(job_dir / "spec.json", spec)
    _write_json(
        job_dir / "status.json",
        {
            "job_id": job_id,
            "label": label[:200],
            "command": command,
            "cwd": relative_workspace_path(working_directory) or ".",
            "status": "queued",
            "created_at": created_at,
            "gpu": bool(gpu),
            "gpu_memory_mb": gpu_memory_mb,
            "started_by": started_by or {"client": "unknown"},
        },
    )

    runner = subprocess.Popen(
        [sys.executable, "-m", "app.job_runner", job_id],
        cwd=str(Path(__file__).resolve().parent.parent),  # /opt/app in the container: where the app package is
        env=workspace_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    # The runner owns status.json after spawn. A parent update could overwrite
    # running/finished with queued, or race its atomic rename of status.tmp.
    _write_json(job_dir / "runner.json", {"runner_pid": runner.pid})
    result = {"job_id": job_id, "runner_pid": runner.pid, "status": "starting"}
    if gpu:
        result["gpu_queue"] = ("This job starts when the GPU is free: after GPU jobs asked for earlier, and not during "
                               "gaming mode. workspace_job_status shows waiting_for_gpu and the reason while it waits.")
    return result


def job_status(job_id: str) -> dict[str, Any]:
    job_dir = _job_dir(job_id)
    status_path = job_dir / "status.json"
    status = _read_json(status_path)
    if not status.get('runner_pid'):
        try:
            status['runner_pid'] = _read_json(job_dir / 'runner.json')['runner_pid']
        except (OSError, ValueError, KeyError):
            pass
    runner_pid = status.get("runner_pid")
    if status.get("status") in {"queued", "waiting_for_gpu", "starting", "running"} and runner_pid:
        if not runner_alive(runner_pid, job_id):
            status["status"] = "stopped_or_lost"
            status.setdefault("finished_at", time.time())
            _write_json(status_path, status)
    status["stdout_log"] = relative_workspace_path(job_dir / "stdout.log")
    status["stderr_log"] = relative_workspace_path(job_dir / "stderr.log")
    return status


def _tail(path: Path, max_chars: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    read_bytes = min(size, max_chars * 4)
    with path.open("rb") as handle:
        if size > read_bytes:
            handle.seek(-read_bytes, os.SEEK_END)
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    return text[-max_chars:]


def job_output(job_id: str, *, stream: str = "both", max_chars: int = 40_000) -> dict[str, Any]:
    if stream not in {"stdout", "stderr", "both"}:
        raise ValueError("stream must be stdout, stderr, or both")
    max_chars = max(1, min(max_chars, settings.max_tool_output_chars))
    job_dir = _job_dir(job_id)
    result: dict[str, Any] = {"job_id": job_id, "status": job_status(job_id)["status"]}
    if stream in {"stdout", "both"}:
        result["stdout"] = _tail(job_dir / "stdout.log", max_chars)
    if stream in {"stderr", "both"}:
        result["stderr"] = _tail(job_dir / "stderr.log", max_chars)
    return result


def stop_job(job_id: str, *, force_after_seconds: int = 10) -> dict[str, Any]:
    status = job_status(job_id)
    runner_pid = status.get("runner_pid")
    if not runner_pid or status["status"] not in {"queued", "waiting_for_gpu", "starting", "running"}:
        return {"job_id": job_id, "stopped": False, "status": status["status"]}

    runner_pid = int(runner_pid)
    try:
        os.killpg(runner_pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"job_id": job_id, "stopped": False, "status": "already_stopped"}

    deadline = time.monotonic() + max(1, force_after_seconds)
    while time.monotonic() < deadline:
        if not psutil.pid_exists(runner_pid):
            return {"job_id": job_id, "stopped": True, "forced": False}
        time.sleep(0.2)

    try:
        os.killpg(runner_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return {"job_id": job_id, "stopped": True, "forced": True}


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    entries: list[tuple[float, dict[str, Any]]] = []
    for directory in settings.jobs_dir.iterdir():
        status_path = directory / "status.json"
        if not status_path.is_file():
            continue
        try:
            status = job_status(directory.name)
            entries.append((float(status.get("created_at", 0)), status))
        except Exception:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    return [status for _, status in entries[:limit]]
