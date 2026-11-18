"""The real size and time limits, in one place, so tool texts and errors state numbers."""

from __future__ import annotations

from .config import settings


def size(count: int) -> str:
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if count >= factor:
            value = count / factor
            return f"{value:.0f} {unit}" if value == int(value) else f"{value:.1f} {unit}"
    return f"{count} bytes"


LIMITS = {
    "workspace_write_file_max_bytes": settings.max_write_file_bytes,
    "workspace_write_file_practical_bytes": 100 * 1024,
    "workspace_import_file_max_bytes": settings.max_import_bytes,
    "app_deploy_max_source_bytes": settings.max_site_upload_bytes,
    "tool_output_max_chars": settings.max_tool_output_chars,
    "workspace_exec_default_timeout_seconds": settings.default_sync_timeout_seconds,
    "workspace_exec_max_timeout_seconds": settings.max_sync_timeout_seconds,
    "client_tool_call_limit_seconds": 240,
    "job_log_max_bytes": settings.max_job_log_bytes,
    "download_link_max_ttl_seconds": settings.max_download_ttl_seconds,
    "owner_upload": "any size, on the admin page (Files)",
}

TRANSFER_GUIDE = (
    f"Moving files in: workspace_write_file for content you type out (text or base64; the server takes "
    f"{size(settings.max_write_file_bytes)}, but keep it under about 100 KB per call); workspace_import_file for a "
    f"ChatGPT attachment (file) or a public HTTPS URL (url), up to {size(settings.max_import_bytes)}; anything larger, or a file "
    "on the owner's computer, the owner uploads on the admin page (Files). Moving files out: workspace_export_file "
    f"gives a download link for a file of any size, valid up to {settings.max_download_ttl_seconds // 3600} hours. "
    f"Tool results carry at most {settings.max_tool_output_chars:,} characters of command output."
)
