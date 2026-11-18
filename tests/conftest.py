"""Test setup: point the workspace server at a temporary /workspace before import."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "platform"))

_WORKSPACE = Path(tempfile.mkdtemp(prefix="mcp-hosting-test-workspace-")).resolve()
os.environ["WORKSPACE_ROOT"] = str(_WORKSPACE)
os.environ.setdefault("FILES_MINT_URL", "http://files.test:8081")
os.environ.setdefault("FILES_MINT_TOKEN", "test-mint-token-" + "x" * 32)
os.environ.setdefault("MAX_WRITE_FILE_BYTES", "1024")
os.environ.setdefault("DEPLOYER_URL", "http://deployer.test:8090")
os.environ.setdefault("DEPLOYER_API_TOKEN", "deployer-test-token-" + "y" * 32)

import pytest  # noqa: E402


@pytest.fixture
def workspace_root() -> Path:
    return _WORKSPACE


@pytest.fixture(autouse=True)
def reachable_pomerium(monkeypatch):
    """Unit tests have no Pomerium: by default its routing check passes. Tests
    that need a failing or unreachable one patch pomerium.probe themselves."""
    from mcp_hosting_platform.deployer import pomerium

    monkeypatch.setattr(pomerium, "probe", lambda *args, **kwargs: ([], True))
