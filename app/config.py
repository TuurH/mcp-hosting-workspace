from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MCP_SERVER_NAME = "MCP Hosting Workspace"
DEFAULT_ALLOWED_ORIGINS = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://claude.ai",
    "https://claude.ai/",
    "https://claude.com",
)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _placeholder(value: str) -> bool:
    lowered = value.lower()
    return "change-me" in lowered or lowered.endswith("@example.com")


@dataclass(frozen=True)
class Settings:
    workspace: Path = field(
        default_factory=lambda: Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
    )
    mcp_server_name: str = field(
        default_factory=lambda: os.getenv("MCP_SERVER_NAME", "").strip() or DEFAULT_MCP_SERVER_NAME
    )
    public_mcp_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_MCP_URL", "https://mcp.example.com")
    )
    public_mcp_host: str = field(
        default_factory=lambda: os.getenv("PUBLIC_MCP_HOST", "mcp.example.com").lower()
    )
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv("MCP_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    )

    cf_access_enabled: bool = field(default_factory=lambda: _bool("CF_ACCESS_ENABLED", False))
    cf_access_team_domain: str = field(
        default_factory=lambda: os.getenv("CF_ACCESS_TEAM_DOMAIN", "").rstrip("/")
    )
    cf_access_aud: str = field(default_factory=lambda: os.getenv("CF_ACCESS_AUD", ""))
    cf_access_allowed_emails: tuple[str, ...] = field(
        default_factory=lambda: tuple(email.lower() for email in _csv("CF_ACCESS_ALLOWED_EMAILS"))
    )
    jwt_leeway_seconds: int = field(default_factory=lambda: _int("CF_ACCESS_JWT_LEEWAY_SECONDS", 60))

    # Only for compose.local.yaml: disables authentication completely.
    local_dev_insecure: bool = field(default_factory=lambda: _bool("LOCAL_DEV_INSECURE", False))

    files_mint_url: str = field(
        default_factory=lambda: os.getenv("FILES_MINT_URL", "http://files:8081").rstrip("/")
    )
    files_mint_token: str = field(default_factory=lambda: os.getenv("FILES_MINT_TOKEN", ""))

    deployer_url: str = field(
        default_factory=lambda: os.getenv("DEPLOYER_URL", "http://deployer:8090").rstrip("/")
    )
    deployer_api_token: str = field(default_factory=lambda: os.getenv("DEPLOYER_API_TOKEN", ""))
    control_file: Path = field(
        default_factory=lambda: Path(os.getenv("CONTROL_FILE", "/control/state.json"))
    )
    max_site_upload_bytes: int = field(
        default_factory=lambda: _int("MAX_SITE_UPLOAD_BYTES", 200 * 1024 * 1024)
    )

    max_sync_timeout_seconds: int = field(default_factory=lambda: _int("MAX_SYNC_TIMEOUT_SECONDS", 1800))
    default_sync_timeout_seconds: int = field(
        default_factory=lambda: _int("DEFAULT_SYNC_TIMEOUT_SECONDS", 180)
    )
    max_tool_output_chars: int = field(default_factory=lambda: _int("MAX_TOOL_OUTPUT_CHARS", 120_000))
    max_command_log_bytes: int = field(
        default_factory=lambda: _int("MAX_COMMAND_LOG_BYTES", 50 * 1024 * 1024)
    )
    max_job_log_bytes: int = field(default_factory=lambda: _int("MAX_JOB_LOG_BYTES", 250 * 1024 * 1024))
    max_import_bytes: int = field(
        default_factory=lambda: _int("MAX_IMPORT_BYTES", 2 * 1024 * 1024 * 1024)
    )
    max_write_file_bytes: int = field(
        default_factory=lambda: _int("MAX_WRITE_FILE_BYTES", 10 * 1024 * 1024)
    )
    max_download_ttl_seconds: int = field(default_factory=lambda: _int("MAX_DOWNLOAD_TTL_SECONDS", 86_400))
    max_parallel_commands: int = field(default_factory=lambda: _int("MAX_PARALLEL_COMMANDS", 4))

    @property
    def state_dir(self) -> Path:
        return self.workspace / ".mcp"

    @property
    def inbox_dir(self) -> Path:
        return self.workspace / "inbox"

    @property
    def outputs_dir(self) -> Path:
        return self.workspace / "outputs"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    def access_problems(self) -> list[str]:
        """Return what is missing for Cloudflare Access enforcement (empty when complete).

        The placeholders shipped in .env.example count as missing.
        """
        problems = []
        if not self.cf_access_enabled:
            problems.append("CF_ACCESS_ENABLED is not true")
        if not self.cf_access_team_domain or _placeholder(self.cf_access_team_domain):
            problems.append("CF_ACCESS_TEAM_DOMAIN is not set")
        if not self.cf_access_aud or _placeholder(self.cf_access_aud):
            problems.append("CF_ACCESS_AUD is not set")
        emails = [e for e in self.cf_access_allowed_emails if not _placeholder(e)]
        if not emails or len(emails) != len(self.cf_access_allowed_emails):
            problems.append("CF_ACCESS_ALLOWED_EMAILS is not set")
        return problems

    @property
    def locked(self) -> bool:
        """True when the MCP endpoint must refuse all requests (fail closed)."""
        return not self.local_dev_insecure and bool(self.access_problems())


settings = Settings()

if settings.local_dev_insecure:
    logger.warning(
        "LOCAL_DEV_INSECURE is on: the MCP endpoint accepts requests WITHOUT authentication. "
        "Use this only with compose.local.yaml and never while the tunnel is running."
    )
elif settings.locked:
    logger.error(
        "MCP endpoint is LOCKED because Cloudflare Access is not fully configured: %s",
        "; ".join(settings.access_problems()),
    )

for directory in (
    settings.workspace,
    settings.state_dir,
    settings.inbox_dir,
    settings.outputs_dir,
    settings.runs_dir,
    settings.jobs_dir,
):
    directory.mkdir(parents=True, exist_ok=True)
