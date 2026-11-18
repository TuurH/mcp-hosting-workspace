from __future__ import annotations

import base64
import os
import uuid

import pytest

from app.server import workspace_write_file


def unique(name: str) -> str:
    return f"write-tests/{uuid.uuid4().hex}/{name}"


def test_writes_text(workspace_root):
    path = unique("hello.txt")
    result = workspace_write_file(path, "héllo")
    assert result == {"path": path, "size_bytes": len("héllo".encode())}
    assert (workspace_root / path).read_text(encoding="utf-8") == "héllo"


def test_writes_base64(workspace_root):
    data = bytes(range(256))
    path = unique("blob.bin")
    workspace_write_file(path, base64.b64encode(data).decode(), encoding="base64")
    assert (workspace_root / path).read_bytes() == data


def test_accepts_wrapped_base64(workspace_root):
    data = bytes(range(256)) * 4
    path = unique("wrapped.bin")
    workspace_write_file(path, base64.encodebytes(data).decode(), encoding="base64")  # 76-char lines
    assert (workspace_root / path).read_bytes() == data


def test_rejects_invalid_base64():
    with pytest.raises(ValueError):
        workspace_write_file(unique("x.bin"), "not base64!!", encoding="base64")


def test_rejects_unknown_encoding():
    with pytest.raises(ValueError):
        workspace_write_file(unique("x.bin"), "abc", encoding="hex")


def test_enforces_size_limit():
    # conftest sets MAX_WRITE_FILE_BYTES=1024
    with pytest.raises(ValueError):
        workspace_write_file(unique("big.txt"), "x" * 1025)


def test_refuses_to_overwrite_unless_asked(workspace_root):
    path = unique("keep.txt")
    workspace_write_file(path, "one")
    with pytest.raises(FileExistsError):
        workspace_write_file(path, "two")
    workspace_write_file(path, "two", overwrite=True)
    assert (workspace_root / path).read_text() == "two"


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../../escape.txt"])
def test_rejects_paths_outside_workspace(path):
    with pytest.raises(ValueError):
        workspace_write_file(path, "x", overwrite=True)


def test_rejects_symlink_pointing_outside(workspace_root, tmp_path):
    link_dir = workspace_root / unique("")
    link_dir.mkdir(parents=True)
    os.symlink(tmp_path, link_dir / "out")
    with pytest.raises(ValueError):
        workspace_write_file(str((link_dir / "out" / "file.txt").relative_to(workspace_root)), "x")


def test_rejects_directory_target(workspace_root):
    path = unique("dir")
    (workspace_root / path).mkdir(parents=True)
    with pytest.raises(ValueError):
        workspace_write_file(path, "x", overwrite=True)


def test_rejects_workspace_root():
    with pytest.raises(ValueError):
        workspace_write_file(".", "x", overwrite=True)
