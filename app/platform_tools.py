"""Client for the deployer's narrow API, used by the app_* MCP tools.

Everything here is untrusted from the deployer's point of view: it decides what
the AI may do. These helpers only package requests and format answers.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
import threading
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .paths import workspace_path

SKIP_NAMES = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".DS_Store", ".env", ".env.local", ".env.production", "secrets", ".ssh", ".aws"}
SITE_FOLDERS = ("site", "dist", "public", "build", "_site", "out")


class PlatformError(RuntimeError):
    """An expected platform rejection whose explanation is safe for the caller."""


class PlatformUnavailable(PlatformError):
    pass


def _url(path: str) -> str:
    return f"{settings.deployer_url}{path}"


def call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    if not settings.deployer_api_token:
        raise PlatformUnavailable("The app platform is not set up yet (DEPLOYER_API_TOKEN is missing).")
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {settings.deployer_api_token}"
    try:
        response = httpx.request(method, _url(path), headers=headers, timeout=kwargs.pop('timeout', httpx.Timeout(55.0)),
                                 trust_env=False, **kwargs)
    except httpx.TimeoutException as exc:
        if method != "GET":
            raise PlatformUnavailable(
                "The deployer did not answer in time; it may still be working. "
                "Check app_status before trying again.") from exc
        raise PlatformUnavailable("The deployer did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise PlatformUnavailable(f"The deployer is not reachable ({exc.__class__.__name__}).") from exc
    try:
        data = response.json()
    except ValueError:
        data = {"error": response.text[:500]}
    if response.status_code >= 400:
        raise PlatformError(data.get("error", f"HTTP {response.status_code}"))
    return data


_activity_cache: tuple[float, dict[str, Any]] | None = None
_activity_lock = threading.Lock()


def activity_snapshot() -> dict[str, Any]:
    """Share one brief platform read between info and activity calls."""
    global _activity_cache
    with _activity_lock:
        now = time.monotonic()
        if _activity_cache and now - _activity_cache[0] < 5:
            return dict(_activity_cache[1])
        try:
            report = call('GET', '/v1/activity', timeout=3.0)
        except (RuntimeError, ValueError) as exc:
            report = {'error':str(exc), 'operations':[], 'recent_deploys':[], 'gpu_apps':[]}
        _activity_cache = (now, report)
        return dict(report)


def site_root(name: str, folder: str = "") -> Path:
    """The folder whose contents become the website."""
    base = workspace_path(folder or f"apps/{name}", must_exist=True)
    if not base.is_dir():
        raise ValueError(f"{base} is not a folder")
    if folder:
        return base
    for candidate in SITE_FOLDERS:
        if (base / candidate / "index.html").is_file():
            return base / candidate
    return base


def pack(root: Path, max_bytes: int) -> tuple[bytes, int, int]:
    """tar.gz of regular files under root (symlinks and tool folders skipped)."""
    buffer = io.BytesIO()
    files = 0
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_NAMES and not os.path.islink(os.path.join(current, d)))
            for name in sorted(names):
                if name in SKIP_NAMES or name.startswith(".env.") or name.endswith((".pem", ".key")):
                    continue
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                if total > max_bytes:
                    raise ValueError(f"the site is larger than {max_bytes // (1024 * 1024)} MB")
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(root)))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
                files += 1
    if files == 0:
        raise ValueError(f"{root} contains no files")
    return buffer.getvalue(), files, total


def control_state(path: Path | None = None) -> dict[str, Any]:
    """Gaming mode / GPU flags published by the deployer (read-only mount)."""
    path = path or settings.control_file
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def gpu_blocked(path: Path | None = None) -> str | None:
    """A message when the workspace should not use the GPU right now, else None."""
    state = control_state(path)
    if not state:
        return None
    if state.get("gaming") or state.get("workspace_gpu_allowed") is False:
        return state.get("message") or "The GPU is not available to the workspace right now."
    return None
