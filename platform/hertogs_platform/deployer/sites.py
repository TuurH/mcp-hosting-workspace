"""Static site storage served by the shared Caddy container.

Layout under the sites volume::

    _versions/<app>/<version>/...      immutable copies of every uploaded version
    <host>/current -> ../_versions/<app>/<version>
"""

from __future__ import annotations

import difflib
import hashlib
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from ..common.tarsafe import ExtractResult, extract_untrusted

TEXT_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".md", ".xml", ".svg",
    ".csv", ".yaml", ".yml", ".toml", ".webmanifest", ".map",
    '.py', '.sh', '.bash', '.ts', '.tsx', '.jsx', '.sql', '.go', '.rs', '.java',
    '.c', '.h', '.cpp', '.hpp', '.ps1', '.bat', '.php', '.rb', '.conf', '.ini',
}


class Sites:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.versions_root = root / "_versions"
        self.staging_root = root / "_staging"
        for directory in (self.root, self.versions_root, self.staging_root):
            directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.root, self.versions_root):
            os.chmod(directory, 0o755)
        for leftover in self.staging_root.iterdir():
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)

    # -- versions ----------------------------------------------------------------
    def version_dir(self, app: str, version: str) -> Path:
        return self.versions_root / app / version

    def upload_path(self) -> Path:
        """A fresh file name for an incoming archive (on disk, not in memory)."""
        return self.staging_root / f"upload-{secrets.token_hex(8)}.tar.gz"

    def store_version(self, app: str, archive: Path, *, max_total_bytes: int) -> tuple[str, ExtractResult]:
        staging = self.staging_root / secrets.token_hex(8)
        try:
            result = extract_untrusted(archive, staging, max_total_bytes=max_total_bytes)
            if result.files == 0:
                raise ValueError("the site is empty")
            version = result.digest[:16]
            destination = self.version_dir(app, version)
            if destination.exists():
                shutil.rmtree(staging)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(destination.parent, 0o755)
                os.replace(staging, destination)
            return version, result
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def prune(self, app: str, keep: set[str]) -> list[str]:
        removed = []
        app_dir = self.versions_root / app
        if not app_dir.is_dir():
            return removed
        for child in app_dir.iterdir():
            if child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        return removed

    def remove_app(self, app: str) -> None:
        shutil.rmtree(self.versions_root / app, ignore_errors=True)

    # -- hosts -------------------------------------------------------------------
    def point(self, host: str, app: str | None, version: str | None) -> None:
        """Point <host>/current at a version, or remove the host when version is None."""
        host_dir = self.root / host
        if app is None or version is None:
            shutil.rmtree(host_dir, ignore_errors=True)
            return
        if not self.version_dir(app, version).is_dir():
            raise FileNotFoundError(f"version {version} of {app} is missing")
        host_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(host_dir, 0o755)
        tmp = host_dir / f".current-{secrets.token_hex(4)}"
        os.symlink(f"../_versions/{app}/{version}", tmp)
        os.replace(tmp, host_dir / "current")

    def pointed_version(self, host: str) -> str | None:
        link = self.root / host / "current"
        if not link.is_symlink():
            return None
        return Path(os.readlink(link)).name

    def hosts(self) -> list[str]:
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and not child.name.startswith("_")
        )

    def ensure_platform_page(self, host: str, html: str) -> None:
        """A tiny built-in page (used for the canary host)."""
        content = html.encode()
        version = hashlib.sha256(content).hexdigest()[:16]
        directory = self.version_dir("_platform", version)
        if not directory.exists():
            directory.mkdir(parents=True)
            (directory / "index.html").write_bytes(content)
            os.chmod(directory, 0o755)
            os.chmod(directory / "index.html", 0o644)
        self.point(host, "_platform", version)

    # -- reading versions ---------------------------------------------------------
    def list_files(self, app: str, version: str) -> list[dict[str, Any]]:
        base = self.version_dir(app, version)
        files = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files.append({"path": str(path.relative_to(base)), "bytes": path.stat().st_size})
        return files

    def read_file(self, app: str, version: str, relative: str, max_bytes: int = 200_000, offset: int = 0) -> bytes:
        base = self.version_dir(app, version).resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise FileNotFoundError(relative)
        with target.open("rb") as handle:
            handle.seek(offset)
            return handle.read(max_bytes)

    def diff(self, app: str, old: str | None, new: str, max_text_bytes: int = 100_000) -> dict[str, Any]:
        new_files = {f["path"]: f["bytes"] for f in self.list_files(app, new)}
        old_files = {f["path"]: f["bytes"] for f in self.list_files(app, old)} if old else {}
        added = sorted(set(new_files) - set(old_files))
        removed = sorted(set(old_files) - set(new_files))
        changed, text_diffs = [], {}
        for path in sorted(set(new_files) & set(old_files)):
            a = self.version_dir(app, old) / path  # type: ignore[arg-type]
            b = self.version_dir(app, new) / path
            if not self._same_contents(a, b):
                changed.append(path)
                if (Path(path).suffix.lower() in TEXT_SUFFIXES or Path(path).name in {"Dockerfile","Containerfile"}) and max(a.stat().st_size, b.stat().st_size) <= max_text_bytes:
                    lines = difflib.unified_diff(
                        a.read_text("utf-8", "replace").splitlines(),
                        b.read_text("utf-8", "replace").splitlines(),
                        fromfile=f"live/{path}", tofile=f"new/{path}", lineterm="",
                    )
                    all_lines = list(lines)
                    text_diffs[path] = "\n".join(all_lines[:400])
                    if len(all_lines)>400:
                        text_diffs[path] += '\n[Diff truncated. Open the exact source revision to review all code.]'
        for path in added:
            b = self.version_dir(app, new) / path
            if (Path(path).suffix.lower() in TEXT_SUFFIXES or Path(path).name in {"Dockerfile","Containerfile"}) and b.stat().st_size <= max_text_bytes:
                text_diffs[path] = "\n".join(
                    f"+{line}" for line in b.read_text("utf-8", "replace").splitlines()[:400]
                )
                if len(b.read_text('utf-8','replace').splitlines())>400:
                    text_diffs[path] += '\n[Diff truncated. Open the exact source revision to review all code.]'
        return {
            "added": [{"path": p, "bytes": new_files[p]} for p in added],
            "removed": [{"path": p, "bytes": old_files[p]} for p in removed],
            "changed": [{"path": p, "bytes": new_files[p]} for p in changed],
            "text_diffs": text_diffs,
        }

    @staticmethod
    def _same_contents(a: Path, b: Path) -> bool:
        # Source bundles can contain 200 MB binaries. Comparing two whole files
        # at once can exhaust the admin/deployer's memory just opening a diff.
        if a.stat().st_size != b.stat().st_size:
            return False
        with a.open('rb') as left, b.open('rb') as right:
            while True:
                chunk = left.read(64 * 1024)
                if chunk != right.read(64 * 1024):
                    return False
                if not chunk:
                    return True
