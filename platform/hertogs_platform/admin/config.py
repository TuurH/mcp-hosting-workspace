from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class AdminSettings:
    public_host: str = field(default_factory=lambda: _env("ADMIN_PUBLIC_HOST", "admin.example.com").lower())
    team_domain: str = field(default_factory=lambda: _env("CF_ACCESS_TEAM_DOMAIN").rstrip("/"))
    audience: str = field(default_factory=lambda: _env("ADMIN_CF_ACCESS_AUD"))
    owner_email: str = field(default_factory=lambda: _env("PLATFORM_OWNER_EMAIL").lower())
    csrf_key: str = field(default_factory=lambda: _env("ADMIN_CSRF_KEY"))
    deployer_socket: str = field(default_factory=lambda: _env("DEPLOYER_SOCKET", "/run/hertogs/deployer.sock"))
    workspace: Path = field(default_factory=lambda: Path(_env("WORKSPACE_ROOT", "/workspace")).resolve())
    port: int = field(default_factory=lambda: int(_env("ADMIN_PORT", "8080")))
    base_domain: str = field(default_factory=lambda: _env("PLATFORM_DOMAIN", "example.com").lower())
    max_upload_gb: float = field(default_factory=lambda: float(_env("ADMIN_MAX_UPLOAD_GB", "50")))
    # Local testing only (compose.local.yaml / tests): no Cloudflare Access check.
    # Instead a one-time link with this token (random when empty) is printed to
    # the container log, which the AI cannot read.
    local_dev_insecure: bool = field(
        default_factory=lambda: _env("ADMIN_LOCAL_DEV_INSECURE", "false").lower() in {"1", "true", "yes"}
    )
    local_token: str = field(default_factory=lambda: _env("ADMIN_LOCAL_TOKEN"))

    @property
    def origin(self) -> str:
        return f"https://{self.public_host}"

    def problems(self) -> list[str]:
        problems = []
        if len(self.csrf_key) < 32:
            problems.append("ADMIN_CSRF_KEY must be at least 32 random characters")
        if "@" not in self.owner_email:
            problems.append("PLATFORM_OWNER_EMAIL is not set")
        if not self.local_dev_insecure:
            if not self.team_domain or "change-me" in self.team_domain.lower():
                problems.append("CF_ACCESS_TEAM_DOMAIN is not set")
            if not self.audience or "change-me" in self.audience.lower():
                problems.append("ADMIN_CF_ACCESS_AUD is not set")
        return problems
