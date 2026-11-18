"""Shared helpers for the platform (deployer/admin) tests."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from cryptography.fernet import Fernet

from mcp_hosting_platform.deployer.config import DeployerSettings
from mcp_hosting_platform.deployer.core import Platform
from mcp_hosting_platform.deployer.db import Database
from mcp_hosting_platform.deployer.gaming import Gaming
from mcp_hosting_platform.deployer.sites import Sites

OWNER = "owner@mcp-hosting.test"
API_TOKEN = "deployer-test-token-" + "y" * 32


def deployer_settings(tmp_path: Path, **overrides) -> DeployerSettings:
    values = dict(
        data_dir=tmp_path / "data",
        sites_dir=tmp_path / "sites",
        pomerium_config=tmp_path / "pomerium" / "config.yaml",
        control_dir=tmp_path / "control",
        socket_path=tmp_path / "run" / "deployer.sock",
        static_log=tmp_path / "logs" / "access.log",
        base_domain="example.com",
        owner_email=OWNER,
        api_token=API_TOKEN,
        secrets_key=Fernet.generate_key().decode(),
        admin_url="https://admin.example.com",
        extra_reserved=(),
        # Nothing listens here: probes report "unreachable" at once and never roll back.
        pomerium_url="http://127.0.0.1:9",
        static_url="http://127.0.0.1:9",
        probe_timeout_seconds=0,
    )
    values.update(overrides)
    return DeployerSettings(**values)


def make_platform(tmp_path: Path, **overrides) -> tuple[Platform, Gaming]:
    settings = deployer_settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    platform = Platform(settings, db, Sites(settings.sites_dir))
    return platform, Gaming(settings, db, None)


def site_archive(files: dict[str, bytes | str], tmp_path: Path, name: str = "site.tar.gz") -> Path:
    """Write a tar.gz with the given files and return its path."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    target = tmp_path / name
    target.write_bytes(buffer.getvalue())
    return target
