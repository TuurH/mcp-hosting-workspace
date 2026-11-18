from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True)
class FilesSettings:
    workspace: Path = field(
        default_factory=lambda: Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
    )
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("FILES_DATA_DIR", "/data")))
    public_files_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_FILES_URL", "https://files.example.com").rstrip("/")
    )
    mint_token: str = field(default_factory=lambda: os.getenv("FILES_MINT_TOKEN", ""))
    max_ttl_seconds: int = field(default_factory=lambda: _int("MAX_DOWNLOAD_TTL_SECONDS", 86_400))
    public_port: int = field(default_factory=lambda: _int("FILES_PUBLIC_PORT", 8080))
    mint_port: int = field(default_factory=lambda: _int("FILES_MINT_PORT", 8081))
    # Client networks allowed to mint links (empty: any). compose.yaml sets the
    # files-mint subnet, so cloudflared cannot reach the mint API.
    mint_allowed_clients: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            part.strip() for part in os.getenv("FILES_MINT_ALLOWED_CLIENTS", "").split(",") if part.strip()
        )
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "links.sqlite3"

    def validate(self) -> None:
        if len(self.mint_token) < 32:
            raise RuntimeError("FILES_MINT_TOKEN must be set to a random value of at least 32 characters")
