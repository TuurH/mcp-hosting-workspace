from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from .config import settings
from .execution import workspace_environment

GPU_QUEUE_POLL_SECONDS = 3
# Free GPU memory that must remain after a job's stated need, for the desktop and drivers.
GPU_MEMORY_MARGIN_MB = 512
# Reserve estimates only while a job starts loading its model. WSL cannot
# reliably attribute VRAM to individual processes; after this grace period use
# measured free memory rather than counting the whole estimate a second time.
GPU_STARTUP_RESERVATION_SECONDS = 120


class GPUCapacityError(ValueError):
    """A requirement that cannot fit even on an otherwise empty GPU."""


def validate_gpu_capacity(need: int) -> None:
    from .activity import gpu_state

    totals = [gpu['memory_total_mb'] for gpu in gpu_state(max_age=2).get('gpus', [])
              if gpu.get('memory_total_mb')]
    if totals and need + GPU_MEMORY_MARGIN_MB > max(totals):
        raise GPUCapacityError(
            f'This job needs {need} MB plus {GPU_MEMORY_MARGIN_MB} MB margin, '
            f'but the largest detected GPU has {max(totals)} MB total. '
            'Reduce gpu_memory_mb or use a smaller workload; this request cannot fit.')


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


async def _pump(stream: asyncio.StreamReader, path: Path, max_bytes: int) -> dict[str, Any]:
    total = 0
    written = 0
    truncated = False
    with path.open("wb") as handle:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if written < max_bytes:
                piece = chunk[: max_bytes - written]
                handle.write(piece)
                handle.flush()
                written += len(piece)
                if len(piece) < len(chunk):
                    truncated = True
            else:
                truncated = True
    return {"bytes_seen": total, "bytes_stored": written, "truncated": truncated}


def runner_alive(pid: Any, job_id: str) -> bool:
    """True when this process is the runner of this job. The process number alone
    is not enough: after a restart of the container another program can have it."""
    try:
        return job_id in psutil.Process(int(pid)).cmdline()
    except (psutil.Error, ValueError, TypeError, OSError):
        return False


def _other_gpu_jobs(job_id: str) -> list[dict[str, Any]]:
    """GPU jobs of other runners that are alive: waiting, starting or running."""
    jobs = []
    for directory in settings.jobs_dir.iterdir():
        if directory.name == job_id or not directory.is_dir():
            continue
        try:
            other = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not other.get("gpu") or other.get("status") not in ("queued", "waiting_for_gpu", "starting", "running"):
            continue
        pid = other.get("runner_pid")
        if pid:
            if not runner_alive(pid, other["job_id"]):
                continue
        elif time.time() - float(other.get("created_at") or 0) > 30:
            continue  # never got a runner
        jobs.append(other)
    return jobs


def gpu_wait_reason(job_id: str, spec: dict[str, Any]) -> str | None:
    """Why this GPU job cannot start yet, or None when it may start now.

    GPU jobs start in the order they were asked for. One runs at a time, unless
    every job involved states how much GPU memory it needs and it fits in what
    is free. Gaming mode, or the owner blocking the GPU, holds the whole queue.
    """
    from . import platform_tools
    from .activity import gpu_free_mb

    need = spec.get('gpu_memory_mb')
    if need:
        # Check before queue ordering/gaming holds: an impossible request must
        # fail, not sit at the front of the queue indefinitely.
        validate_gpu_capacity(int(need))
    blocked = platform_tools.gpu_blocked()
    if blocked:
        return blocked
    mine = (float(spec.get("created_at") or 0), job_id)
    running = []
    for other in _other_gpu_jobs(job_id):
        name = f"{other['job_id'][:8]}" + (f" ({other['label']})" if other.get("label") else "")
        if other["status"] in ("starting", "running"):
            running.append((other, name))
        elif (float(other.get("created_at") or 0), other["job_id"]) < mine:
            return f"GPU job {name} is ahead in the queue."
    names = ", ".join(name for _, name in running)
    if running and (not need or not all(other.get("gpu_memory_mb") for other, _ in running)):
        return (f"GPU job {names} is running. GPU jobs run one at a time, unless every job states "
                "gpu_memory_mb and they fit together.")
    if not need:
        return None
    free = gpu_free_mb()
    if free is None:
        return "The free GPU memory cannot be read; this job has a GPU memory requirement."
    # Avoid promising the same free memory to simultaneous launches, without
    # reserving already-accounted-for usage for an entire long-running job.
    now = time.time()
    reserved = sum(int(other['gpu_memory_mb']) for other, _ in running
                   if now - float(other.get('started_at') or other.get('created_at') or now)
                   < GPU_STARTUP_RESERVATION_SECONDS)
    if free < int(need) + reserved + GPU_MEMORY_MARGIN_MB:
        return (f"{free} MB of GPU memory is free, {reserved} MB is reserved for GPU jobs in their first "
                f"{GPU_STARTUP_RESERVATION_SECONDS} seconds after admission; "
                f"this job needs {int(need)} MB plus {GPU_MEMORY_MARGIN_MB} MB margin.")
    return None


async def _wait_for_gpu(job_id: str, spec: dict[str, Any], status: dict[str, Any], status_path: Path,
                        stop_requested: asyncio.Event) -> bool:
    """Wait for this job's turn on the GPU. False when it was stopped while waiting."""
    import fcntl

    lock_path = settings.jobs_dir / ".gpu-queue.lock"
    waiting_since = time.time()
    while not stop_requested.is_set():
        # One runner decides at a time, so two jobs never both see a free GPU.
        with lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            reason = gpu_wait_reason(job_id, spec)
            if reason is None:
                status.pop("waiting_for", None)
                status.update({"status": "starting", "started_at": time.time(),
                               "waited_for_gpu_seconds": round(time.time() - waiting_since)})
                _atomic_json(status_path, status)
                return True
        status.update({"status": "waiting_for_gpu", "waiting_for": reason, "waiting_since": waiting_since})
        _atomic_json(status_path, status)
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=GPU_QUEUE_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    return False


async def run(job_id: str) -> int:
    job_dir = settings.jobs_dir / job_id
    spec_path = job_dir / "spec.json"
    status_path = job_dir / "status.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    command = spec["command"]
    cwd = spec["cwd"]
    env = workspace_environment(spec.get("env"))
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"

    status: dict[str, Any] = {
        "job_id": job_id,
        "label": spec.get("label") or "",
        "command": command,
        "cwd": cwd,
        "runner_pid": os.getpid(),
        "status": "starting",
        "created_at": spec["created_at"],
        "gpu": bool(spec.get("gpu")),
        "gpu_memory_mb": spec.get("gpu_memory_mb"),
        "started_by": spec.get("started_by") or {"client": "unknown"},
    }

    stop_requested = asyncio.Event()
    running: list[asyncio.subprocess.Process] = []

    def request_stop() -> None:
        stop_requested.set()
        for process in running:
            if process.returncode is None:
                process.terminate()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    if status["gpu"]:
        try:
            admitted = await _wait_for_gpu(job_id, spec, status, status_path, stop_requested)
        except GPUCapacityError as exc:
            status.pop('waiting_for', None)
            status.update(status='failed', exit_code=2, finished_at=time.time(), error=str(exc))
            _atomic_json(status_path, status)
            return 2
        if not admitted:
            status.pop("waiting_for", None)
            status.update({"status": "stopped", "finished_at": time.time(), "note": "Stopped while waiting for the GPU."})
            _atomic_json(status_path, status)
            return 0
    else:
        status["started_at"] = time.time()
        _atomic_json(status_path, status)

    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-lc",
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    running.append(process)
    assert process.stdout is not None
    assert process.stderr is not None
    status.update({"status": "running", "process_pid": process.pid})
    _atomic_json(status_path, status)
    if stop_requested.is_set():
        process.terminate()

    stdout_task = asyncio.create_task(_pump(process.stdout, stdout_path, settings.max_job_log_bytes))
    stderr_task = asyncio.create_task(_pump(process.stderr, stderr_path, settings.max_job_log_bytes))

    await process.wait()
    stdout_info, stderr_info = await asyncio.gather(stdout_task, stderr_task)
    status.update(
        {
            "status": "stopped" if stop_requested.is_set() else "finished",
            "exit_code": process.returncode,
            "finished_at": time.time(),
            "stdout": stdout_info,
            "stderr": stderr_info,
        }
    )
    _atomic_json(status_path, status)
    return int(process.returncode or 0)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.job_runner JOB_ID")
    raise SystemExit(asyncio.run(run(sys.argv[1])))


if __name__ == "__main__":
    main()
