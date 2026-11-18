from __future__ import annotations

import re
from pathlib import Path

from .config import settings

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")


def workspace_path(value: str | Path, *, must_exist: bool = False) -> Path:
    raw = Path(value)
    candidate = raw.resolve() if raw.is_absolute() else (settings.workspace / raw).resolve()

    try:
        candidate.relative_to(settings.workspace)
    except ValueError as exc:
        raise ValueError("Path must remain inside /workspace") from exc

    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def safe_filename(value: str | None, fallback: str = "file") -> str:
    name = Path(value or fallback).name.strip().replace("\x00", "")
    name = _SAFE_FILENAME.sub("_", name)
    name = name.strip(" .")
    return name[:240] or fallback


def relative_workspace_path(path: Path) -> str:
    return str(path.resolve().relative_to(settings.workspace))
