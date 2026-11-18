from __future__ import annotations

import asyncio
import json
import re
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .paths import relative_workspace_path, workspace_path


def workspace_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    persistent_venv = settings.workspace / ".venv"
    env.update(
        {
            "HOME": "/home/ai",
            "WORKSPACE": str(settings.workspace),
            "VIRTUAL_ENV": str(persistent_venv),
            "PATH": (
                f"{persistent_venv}/bin:/opt/venv/bin:/home/ai/.local/bin:"
                + env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            ),
            "HF_HOME": str(settings.workspace / ".cache" / "huggingface"),
            "TORCH_HOME": str(settings.workspace / ".cache" / "torch"),
            "XDG_CACHE_HOME": str(settings.workspace / ".cache"),
            "UV_CACHE_DIR": str(settings.workspace / ".cache" / "uv"),
            "PIP_CACHE_DIR": str(settings.workspace / ".cache" / "pip"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    secret_file = Path(os.getenv("WORKSPACE_SECRETS_FILE", "/run/workspace-secrets/environment.json"))
    try:
        if secret_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("workspace token file exceeds its size limit")
        tokens = json.loads(secret_file.read_text())
        if not isinstance(tokens, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", k)
            and isinstance(v, str) and "\0" not in v for k, v in tokens.items()
        ):
            raise ValueError("invalid workspace token file")
        env.update(tokens)
    except FileNotFoundError:
        pass  # a fresh installation has no tokens yet
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


async def _pump_stream(
    stream: asyncio.StreamReader,
    destination: Path,
    max_bytes: int,
) -> tuple[int, bool]:
    total = 0
    written = 0
    truncated = False
    with destination.open("wb") as handle:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if written < max_bytes:
                remaining = max_bytes - written
                piece = chunk[:remaining]
                handle.write(piece)
                written += len(piece)
                if len(piece) < len(chunk):
                    truncated = True
            else:
                truncated = True
    return total, truncated


def _read_for_tool(path: Path, limit_chars: int) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit_chars:
        return text
    half = max(1, limit_chars // 2)
    return (
        text[:half]
        + f"\n\n--- output omitted: {len(text) - (2 * half):,} characters ---\n\n"
        + text[-half:]
    )


async def execute_command(
    *,
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 300,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not command.strip():
        raise ValueError("command cannot be empty")
    if timeout_seconds < 1 or timeout_seconds > settings.max_sync_timeout_seconds:
        raise ValueError(
            f"timeout_seconds must be between 1 and {settings.max_sync_timeout_seconds}"
        )

    working_directory = workspace_path(cwd, must_exist=True)
    if not working_directory.is_dir():
        raise NotADirectoryError(working_directory)

    run_id = uuid.uuid4().hex
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-lc",
        command,
        cwd=str(working_directory),
        env=workspace_environment(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(
        _pump_stream(process.stdout, stdout_path, settings.max_command_log_bytes)
    )
    stderr_task = asyncio.create_task(
        _pump_stream(process.stderr, stderr_path, settings.max_command_log_bytes)
    )

    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    stdout_total, stdout_truncated = await stdout_task
    stderr_total, stderr_truncated = await stderr_task
    elapsed = time.monotonic() - started

    return {
        "run_id": run_id,
        "command": command,
        "cwd": relative_workspace_path(working_directory) or ".",
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": _read_for_tool(stdout_path, settings.max_tool_output_chars),
        "stderr": _read_for_tool(stderr_path, settings.max_tool_output_chars),
        "stdout_bytes": stdout_total,
        "stderr_bytes": stderr_total,
        "stdout_log_truncated": stdout_truncated,
        "stderr_log_truncated": stderr_truncated,
        "stdout_log": relative_workspace_path(stdout_path),
        "stderr_log": relative_workspace_path(stderr_path),
    }
