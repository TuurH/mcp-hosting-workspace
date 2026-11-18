from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from hertogs_platform.files.app import build_mint_app, build_public_app, open_workspace_file
from hertogs_platform.files.config import FilesSettings
from hertogs_platform.files.store import LinkStore

MINT_TOKEN = "m" * 40


@pytest.fixture
def env(tmp_path):
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    (root / "outputs").mkdir()
    (root / "outputs" / "report.txt").write_bytes(b"0123456789")
    (root / "outputs" / "page.html").write_text("<script>alert(1)</script>")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "outputs" / "escape.txt")
    settings = FilesSettings(
        workspace=root,
        data_dir=tmp_path / "data",
        public_files_url="https://files.example.com",
        mint_token=MINT_TOKEN,
        max_ttl_seconds=86_400,
    )
    store = LinkStore(settings.db_path)
    mint = TestClient(build_mint_app(settings, store))
    public = TestClient(build_public_app(settings, store), base_url="https://files.example.com")
    return root, settings, store, mint, public


def mint_link(mint: TestClient, path: str, token: str = MINT_TOKEN, **extra) -> httpx.Response:
    return mint.post(
        "/internal/links",
        json={"path": path, **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


def public_path(url: str) -> str:
    return url.removeprefix("https://files.example.com")


def test_mint_requires_token(env):
    _, _, _, mint, _ = env
    assert mint.post("/internal/links", json={"path": "outputs/report.txt"}).status_code == 401
    assert mint_link(mint, "outputs/report.txt", token="wrong" * 10).status_code == 401


@pytest.mark.parametrize(
    "path",
    ["../secret.txt", "/etc/passwd", "outputs", "outputs/missing.txt", "outputs/escape.txt", ""],
)
def test_mint_rejects_unsafe_or_missing_paths(env, path):
    _, _, _, mint, _ = env
    assert mint_link(mint, path).status_code in {400, 404}


def test_download_round_trip_and_headers(env):
    _, _, _, mint, public = env
    minted = mint_link(mint, "outputs/report.txt", ttl_seconds=600)
    assert minted.status_code == 200
    body = minted.json()
    assert body["size_bytes"] == 10
    assert body["ttl_seconds"] == 600
    assert body["download_url"].startswith("https://files.example.com/files/")

    response = public.get(public_path(body["download_url"]))
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in response.headers


def test_html_is_never_served_inline(env):
    _, _, _, mint, public = env
    url = mint_link(mint, "outputs/page.html").json()["download_url"]
    response = public.get(public_path(url))
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")


def test_head_and_ranges(env):
    _, _, _, mint, public = env
    path = public_path(mint_link(mint, "outputs/report.txt").json()["download_url"])

    head = public.head(path)
    assert head.status_code == 200
    assert head.headers["content-length"] == "10"
    assert head.content == b""

    partial = public.get(path, headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"

    suffix = public.get(path, headers={"Range": "bytes=-3"})
    assert suffix.status_code == 206
    assert suffix.content == b"789"

    assert public.get(path, headers={"Range": "bytes=50-60"}).status_code == 416


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_absurd_ranges_are_ignored_without_leaking(env):
    _, _, _, mint, public = env
    path = public_path(mint_link(mint, "outputs/report.txt").json()["download_url"])
    before = open_fds()
    for header in ("bytes=-" + "9" * 5000, "bytes=" + "1" * 30 + "-", "bytes=5-2"):
        response = public.get(path, headers={"Range": header})
        assert response.status_code in (200, 416)
        assert response.headers["x-content-type-options"] == "nosniff"
    assert open_fds() <= before


def test_if_range_mismatch_sends_the_whole_file(env):
    _, _, _, mint, public = env
    path = public_path(mint_link(mint, "outputs/report.txt").json()["download_url"])
    etag = public.head(path).headers["etag"]
    assert public.get(path, headers={"Range": "bytes=2-5", "If-Range": etag}).status_code == 206
    stale = public.get(path, headers={"Range": "bytes=2-5", "If-Range": '"old"'})
    assert stale.status_code == 200 and stale.content == b"0123456789"


def test_fifo_and_symlink_loop_are_refused_quickly(env):
    root, _, _, mint, _ = env
    os.mkfifo(root / "outputs" / "pipe")
    os.symlink("loop-b", root / "outputs" / "loop-a")
    os.symlink("loop-a", root / "outputs" / "loop-b")
    started = time.monotonic()
    assert mint_link(mint, "outputs/pipe").status_code == 404
    assert mint_link(mint, "outputs/loop-a").status_code == 404
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_file_explains_itself(env):
    root, _, _, mint, _ = env
    (root / "outputs" / "report.txt").chmod(0)
    response = mint_link(mint, "outputs/report.txt")
    assert response.status_code == 403
    assert "chmod" in response.json()["error"]


def test_mint_client_allowlist(env):
    root, settings, store, _, _ = env
    from dataclasses import replace

    limited = replace(settings, mint_allowed_clients=("10.204.5.0/28",))
    outsider = TestClient(build_mint_app(limited, store), client=("10.204.2.2", 5000))
    assert mint_link(outsider, "outputs/report.txt").status_code == 403
    assert outsider.get("/healthz").status_code == 200
    insider = TestClient(build_mint_app(limited, store), client=("10.204.5.9", 5000))
    assert mint_link(insider, "outputs/report.txt").status_code == 200


def test_ttl_is_clamped(env):
    _, _, _, mint, _ = env
    assert mint_link(mint, "outputs/report.txt", ttl_seconds=5).json()["ttl_seconds"] == 60
    assert mint_link(mint, "outputs/report.txt", ttl_seconds=10**9).json()["ttl_seconds"] == 86_400


def test_expired_link_is_404(env):
    _, _, store, mint, public = env
    path = public_path(mint_link(mint, "outputs/report.txt").json()["download_url"])
    store._db.execute("UPDATE links SET expires_at = ?", (time.time() - 1,))
    assert public.get(path).status_code == 404


def test_file_swapped_for_symlink_after_minting_is_404(env, tmp_path):
    root, _, _, mint, public = env
    path = public_path(mint_link(mint, "outputs/report.txt").json()["download_url"])
    (root / "outputs" / "report.txt").unlink()
    os.symlink(tmp_path / "secret.txt", root / "outputs" / "report.txt")
    assert public.get(path).status_code == 404


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/mcp"),
        ("GET", "/healthz"),
        ("POST", "/internal/links"),
        ("GET", "/files/short/x.txt"),
        ("GET", "/files/" + "A" * 43 + "/x.txt"),
        ("POST", "/files/" + "A" * 43 + "/x.txt"),
    ],
)
def test_public_app_only_serves_valid_tokens(env, method, path):
    _, _, _, _, public = env
    assert public.request(method, path).status_code == 404


def test_mint_app_has_no_download_route(env):
    _, _, _, mint, _ = env
    assert mint.get("/files/" + "A" * 43 + "/x.txt").status_code == 404


def test_store_keeps_only_token_hashes(env):
    _, settings, store, mint, _ = env
    url = mint_link(mint, "outputs/report.txt").json()["download_url"]
    token = url.split("/files/")[1].split("/")[0]
    rows = store._db.execute("SELECT token_hash FROM links").fetchall()
    assert all(token not in row[0] for row in rows)


def test_open_workspace_file_rejects_absolute_and_nul(env):
    root, *_ = env
    for bad in ("/etc/passwd", "outputs/\x00x", ""):
        with pytest.raises(OSError):
            open_workspace_file(root, bad)


def test_settings_require_long_mint_token(tmp_path):
    with pytest.raises(RuntimeError):
        FilesSettings(workspace=tmp_path, data_dir=tmp_path, mint_token="short").validate()


def test_workspace_export_uses_files_service(env, monkeypatch, workspace_root):
    """End to end: the workspace's create_download mints through the files service."""
    from app import downloads

    _, settings, store, _, _ = env
    shared = FilesSettings(
        workspace=workspace_root,
        data_dir=settings.data_dir,
        public_files_url="https://files.example.com",
        mint_token=downloads.settings.files_mint_token,
        max_ttl_seconds=86_400,
    )
    mint = TestClient(build_mint_app(shared, store))
    public = TestClient(build_public_app(shared, store), base_url="https://files.example.com")

    def fake_post(url, json, headers, timeout, trust_env):
        assert trust_env is False
        assert url == f"{downloads.settings.files_mint_url}/internal/links"
        return mint.post("/internal/links", json=json, headers=headers)

    monkeypatch.setattr(downloads.httpx, "post", fake_post)
    target = workspace_root / "outputs" / "export-test.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("exported")

    result = downloads.create_download("outputs/export-test.txt", ttl_seconds=120)
    assert result["path"] == "outputs/export-test.txt"
    response = public.get(public_path(result["download_url"]))
    assert response.status_code == 200
    assert response.content == b"exported"
