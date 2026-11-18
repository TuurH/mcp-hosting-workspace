from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True)
class DeployerSettings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYER_DATA_DIR", "/data")))
    sites_dir: Path = field(default_factory=lambda: Path(_env("SITES_DIR", "/srv/sites")))
    pomerium_config: Path = field(
        default_factory=lambda: Path(_env("POMERIUM_CONFIG_PATH", "/pomerium/config.yaml"))
    )
    control_dir: Path = field(default_factory=lambda: Path(_env("CONTROL_DIR", "/control")))
    socket_path: Path = field(
        default_factory=lambda: Path(_env("DEPLOYER_SOCKET", "/run/mcp-hosting-workspace/deployer.sock"))
    )
    static_log: Path = field(default_factory=lambda: Path(_env("STATIC_LOG", "/logs/access.log")))
    docker_socket: str = field(default_factory=lambda: _env("DOCKER_SOCKET", "/var/run/docker.sock"))

    base_domain: str = field(default_factory=lambda: _env("PLATFORM_DOMAIN", "example.com").lower())
    owner_email: str = field(default_factory=lambda: _env("PLATFORM_OWNER_EMAIL").lower())
    api_token: str = field(default_factory=lambda: _env("DEPLOYER_API_TOKEN"))
    secrets_key: str = field(default_factory=lambda: _env("DEPLOYER_SECRETS_KEY"))
    admin_url: str = field(default_factory=lambda: _env("ADMIN_URL", "https://admin.example.com").rstrip("/"))
    extra_reserved: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            part.strip().lower() for part in _env("RESERVED_APP_NAMES").split(",") if part.strip()
        )
    )

    narrow_port: int = field(default_factory=lambda: _int("DEPLOYER_PORT", 8090))
    pomerium_url: str = field(default_factory=lambda: _env("POMERIUM_PROBE_URL", "http://pomerium:80"))
    static_url: str = field(default_factory=lambda: _env("STATIC_UPSTREAM", "http://static:8080"))
    workspace_container: str = field(
        default_factory=lambda: _env("WORKSPACE_CONTAINER", "mcp-hosting-workspace")
    )
    helper_image: str = field(default_factory=lambda: _env("HELPER_IMAGE", "mcp-hosting-workspace/platform:local"))
    builder_image: str = field(default_factory=lambda: _env('BUILDER_IMAGE', 'moby/buildkit:v0.35.0-rootless'))
    workspace_volume: str = field(default_factory=lambda: _env('WORKSPACE_VOLUME', 'mcp-hosting-workspace-data'))
    # Where build workers leave their image for the deployer. Used only when the
    # volume name is set (compose.yaml does) and the directory is mounted here.
    build_scratch_dir: str = field(default_factory=lambda: _env('BUILD_SCRATCH_DIR', '/scratch'))
    build_scratch_volume: str = field(default_factory=lambda: _env('BUILD_SCRATCH_VOLUME', ''))
    probe_timeout_seconds: int = field(default_factory=lambda: _int("PROBE_TIMEOUT_SECONDS", 25))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "deployer.sqlite3"

    @property
    def snapshot_path(self) -> Path:
        return self.data_dir / "backup" / "deployer.sqlite3"

    @property
    def auth_host(self) -> str:
        return f"auth.{self.base_domain}"

    @property
    def canary_host(self) -> str:
        return f"canary.{self.base_domain}"

    def host_for(self, app: str) -> str:
        return f"{app}.{self.base_domain}"

    def preview_host_for(self, app: str) -> str:
        return f"next-{app}.{self.base_domain}"

    def problems(self) -> list[str]:
        problems = []
        if "@" not in self.owner_email:
            problems.append("PLATFORM_OWNER_EMAIL is not set")
        if len(self.api_token) < 32:
            problems.append("DEPLOYER_API_TOKEN must be at least 32 random characters")
        return problems
