from __future__ import annotations

from typing import Any

import httpx

from .config import settings
from .paths import relative_workspace_path, safe_filename, workspace_path


class DownloadError(RuntimeError):
    """An anticipated file-service failure safe to show to the tool caller."""


def create_download(path: str, ttl_seconds: int = 3600) -> dict[str, Any]:
    """Ask the files service for an expiring download link to a workspace file.

    The files service runs in its own container, serves the file read-only as an
    attachment and keeps its token database outside the workspace.
    """
    source = workspace_path(path, must_exist=True)
    if not source.is_file():
        raise ValueError("Only regular files can be exported")
    if not settings.files_mint_token:
        raise DownloadError("FILES_MINT_TOKEN is not configured; download links are unavailable")

    ttl_seconds = max(60, min(int(ttl_seconds), settings.max_download_ttl_seconds))
    try:
        response = httpx.post(
            f"{settings.files_mint_url}/internal/links",
            json={
                "path": relative_workspace_path(source),
                "ttl_seconds": ttl_seconds,
                "filename": safe_filename(source.name, "download"),
            },
            headers={"Authorization": f"Bearer {settings.files_mint_token}"},
            timeout=httpx.Timeout(15.0),
            trust_env=False,  # internal network: never through a proxy from the environment
        )
    except httpx.HTTPError as exc:
        raise DownloadError(
            f"The files service is not reachable ({exc.__class__.__name__}); the owner can "
            "check it with 'docker compose ps files'"
        ) from exc
    if response.status_code != 200:
        detail = response.text[:500]
        raise DownloadError(f"Files service refused the link ({response.status_code}): {detail}")
    return response.json()
