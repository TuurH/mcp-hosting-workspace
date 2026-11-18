"""Workspace file manager for the admin page.

Everything stays inside /workspace: paths are resolved and checked, nothing is
written through a symlink, and deletes move items to /workspace/.trash.
Uploads arrive in chunks (Cloudflare limits a request to 100 MB) and are
assembled in /workspace/.uploads before being moved into place.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from starlette.responses import FileResponse

TEXT_PREVIEW_BYTES = 1024 * 1024
INLINE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".gif": "image/gif", ".webp": "image/webp"}
_UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")
_BAD_NAME = re.compile(r"[\x00-\x1f/\\]")
MAX_CHUNK = 16 * 1024 * 1024


class FileError(ValueError):
    pass


class OpenedFileResponse(FileResponse):
    """Keep the validated inode open until the response is done, including ranges."""
    def __init__(self, handle, info, *, filename, media_type, inline, headers):
        self.handle = handle
        super().__init__(f'/proc/self/fd/{handle.fileno()}', stat_result=info, filename=filename,
                         media_type=media_type, headers=headers,
                         content_disposition_type='inline' if inline else 'attachment')

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float


class FileManager:
    def __init__(self, root: Path, max_upload_bytes: int) -> None:
        self.root = root.resolve()
        self.trash = self.root / ".trash"
        self.uploads = self.root / ".uploads"
        self.max_upload_bytes = max_upload_bytes

    def storage_usage(self) -> dict[str, Any]:
        total = 0
        partial = False
        deadline = time.monotonic() + 2
        try:
            for directory, folders, files, parent in os.fwalk(self.root, follow_symlinks=False):
                if time.monotonic() > deadline:
                    partial = True
                    break
                for name in files:
                    if time.monotonic() > deadline:
                        partial = True
                        break
                    try:
                        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISREG(entry.st_mode):
                            total += entry.st_size
                    except OSError:
                        partial = True
            disk = shutil.disk_usage(self.root)
            return {"used": total, "partial": partial, "disk_free": disk.free, "disk_total": disk.total}
        except OSError:
            return {"used": total, "partial": True, "disk_free": None, "disk_total": None}

    # ------------------------------------------------------------------ paths
    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        relative = (relative or "").strip().lstrip("/")
        if "\x00" in relative:
            raise FileError("invalid path")
        candidate = (self.root / relative).resolve(strict=False)
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise FileError("path is outside /workspace")
        if must_exist and not candidate.exists():
            raise FileError("not found")
        return candidate

    def resolve_entry(self, relative: str) -> Path:
        """The entry itself (a symlink stays a symlink); its folder must be inside /workspace."""
        relative = (relative or "").strip().strip("/")
        if not relative or "\x00" in relative:
            raise FileError("invalid path")
        lexical = self.root / relative
        parent = lexical.parent.resolve(strict=True)
        if parent != self.root and not parent.is_relative_to(self.root):
            raise FileError("path is outside /workspace")
        entry = parent / lexical.name
        if not entry.exists() and not entry.is_symlink():
            raise FileError("not found")
        return entry

    def rel(self, path: Path) -> str:
        return "" if path == self.root else str(path.relative_to(self.root))

    @staticmethod
    def check_name(name: str) -> str:
        name = name.strip()
        if not name or name in (".", "..") or _BAD_NAME.search(name) or len(name) > 255:
            raise FileError("invalid name")
        return name

    @contextlib.contextmanager
    def _dir_fd(self, path: Path) -> Iterator[int]:
        """Pin every directory component; a concurrent symlink swap fails closed."""
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.relative_to(self.root).parts:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
            yield fd
        finally:
            os.close(fd)

    def _open_file(self, path: Path):
        with self._dir_fd(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise FileError('not a regular file')
            return os.fdopen(fd, 'rb'), info
        except BaseException:
            os.close(fd)
            raise

    # ------------------------------------------------------------------ reading
    def listing(self, relative: str) -> dict[str, Any]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise FileError("not a folder")
        entries = []
        with self._dir_fd(directory) as parent, os.scandir(parent) as it:
            for item in it:
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                entries.append(Entry(item.name, self.rel(directory / item.name), is_dir,
                                     0 if is_dir else info.st_size, info.st_mtime))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        crumbs = []
        if directory != self.root:
            accumulated = Path()
            for part in directory.relative_to(self.root).parts:
                accumulated = accumulated / part
                crumbs.append({"name": part, "path": str(accumulated)})
        return {
            "path": self.rel(directory),
            "parent": self.rel(directory.parent) if directory != self.root else None,
            "crumbs": crumbs,
            "entries": entries,
        }

    def describe(self, relative: str) -> dict[str, Any]:
        path = self.resolve(relative)
        if path.is_dir():
            with self._dir_fd(path) as directory:
                info = os.fstat(directory)
            handle = None
        else:
            handle, info = self._open_file(path)
        suffix = path.suffix.lower()
        result: dict[str, Any] = {
            "path": self.rel(path), "name": path.name, "size": info.st_size,
            "modified": info.st_mtime, "is_dir": stat.S_ISDIR(info.st_mode),
            "kind": "other", "text": None, "truncated": False,
        }
        if stat.S_ISDIR(info.st_mode):
            return result
        with handle:
            if suffix in INLINE_IMAGE_TYPES:
                result["kind"] = "image"
                return result
            head = handle.read(TEXT_PREVIEW_BYTES)
        if b"\x00" not in head[:8192]:
            try:
                result["text"] = head.decode("utf-8")
                result["kind"] = "text"
                result["truncated"] = info.st_size > TEXT_PREVIEW_BYTES
            except UnicodeDecodeError:
                pass
        return result

    def raw(self, relative: str, inline: bool) -> tuple[Path, str, bool]:
        """Return (path, media type, inline?) for a download. Only raster images go inline."""
        path = self.resolve(relative)
        if not path.is_file():
            raise FileError("not a file")
        media = INLINE_IMAGE_TYPES.get(path.suffix.lower())
        if inline and media:
            return path, media, True
        return path, "application/octet-stream", False

    def open_raw(self, relative: str, inline: bool):
        path, media, inline = self.raw(relative, inline)
        handle, info = self._open_file(path)
        return handle, info, path.name, media, inline

    # ------------------------------------------------------------------ changes
    def mkdir(self, relative_dir: str, name: str) -> str:
        parent = self.resolve(relative_dir)
        target = parent / self.check_name(name)
        with self._dir_fd(parent) as directory:
            os.mkdir(target.name, dir_fd=directory)
        return self.rel(target)

    def rename(self, relative: str, new_name: str) -> str:
        source = self.resolve_entry(relative)
        if source == self.root:
            raise FileError("cannot rename /workspace")
        target = source.parent / self.check_name(new_name)
        with self._dir_fd(source.parent) as directory:
            try:
                os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileError("an item with that name already exists")
            os.rename(source.name, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        return self.rel(target)

    # .trash and .uploads live in the AI-writable workspace. They are always
    # opened without following symlinks and used through that directory handle,
    # so swapping them for a link never makes the admin page touch other files.
    @contextlib.contextmanager
    def _own_dir(self, path: Path) -> Iterator[int]:
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            pass
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise FileError(f"/workspace/{path.name} is not a normal folder; remove it and try again") from exc
        try:
            yield fd
        finally:
            os.close(fd)

    def delete(self, relative: str) -> str:
        """Move to /workspace/.trash/<timestamp>-<name> (a rename: nothing is copied)."""
        path = self.resolve_entry(relative)
        if path in (self.root, self.trash, self.uploads) or self.root / ".mcp" == path:
            raise FileError("this folder cannot be deleted here")
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{path.name}"[:255]
        with self._own_dir(self.trash) as trash, self._dir_fd(path.parent) as parent:
            try:
                os.rename(path.name, name, src_dir_fd=parent, dst_dir_fd=trash)
            except OSError as exc:
                raise FileError(f"cannot move it to the trash ({exc.strerror})") from exc
        return f".trash/{name}"

    def empty_trash(self) -> int:
        count = 0
        with self._own_dir(self.trash) as trash:
            with os.scandir(trash) as entries:
                names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
            for name, is_dir in names:
                if is_dir:
                    shutil.rmtree(name, dir_fd=trash, ignore_errors=True)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name, dir_fd=trash)
                count += 1
        return count

    # ------------------------------------------------------------------ uploads
    @staticmethod
    def _upload_names(upload_id: str) -> tuple[str, str]:
        if not _UPLOAD_ID.match(upload_id):
            raise FileError("invalid upload id")
        return f"{upload_id}.part", f"{upload_id}.json"

    @staticmethod
    def _read_meta(uploads: int, meta: str) -> dict[str, Any]:
        try:
            fd = os.open(meta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=uploads)
        except FileNotFoundError as exc:
            raise FileError("unknown upload") from exc
        except OSError as exc:
            raise FileError("upload metadata is damaged") from exc
        with os.fdopen(fd) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise FileError('upload metadata is not a regular file')
            try:
                info = json.loads(handle.read(4096))
            except ValueError as exc:
                raise FileError("upload metadata is damaged") from exc
        if not isinstance(info, dict) or not isinstance(info.get("size"), int):
            raise FileError("upload metadata is damaged")
        return info

    @staticmethod
    def _part_size(uploads: int, part: str) -> int:
        try:
            info = os.stat(part, dir_fd=uploads, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise FileError("unknown upload") from exc
        if not stat.S_ISREG(info.st_mode):
            raise FileError("upload data is damaged")
        return info.st_size

    def start_upload(self, relative_dir: str, name: str, size: int) -> dict[str, Any]:
        directory = self.resolve(relative_dir)
        if not directory.is_dir():
            raise FileError("not a folder")
        name = self.check_name(name)
        if size < 0 or size > self.max_upload_bytes:
            raise FileError(f"files up to {self.max_upload_bytes // 1024**3} GB can be uploaded")
        if shutil.disk_usage(self.root).free < size + 1024**3:
            raise FileError("not enough free disk space")
        upload_id = secrets.token_hex(16)
        part, meta = self._upload_names(upload_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with self._own_dir(self.uploads) as uploads:
            os.close(os.open(part, flags, 0o644, dir_fd=uploads))
            with os.fdopen(os.open(meta, flags, 0o644, dir_fd=uploads), "w") as handle:
                handle.write(json.dumps({"dir": self.rel(directory), "name": name, "size": size,
                                         "started": time.time()}))
        return {"upload_id": upload_id, "received": 0, "chunk_size": 8 * 1024 * 1024}

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        part, meta = self._upload_names(upload_id)
        with self._own_dir(self.uploads) as uploads:
            info = self._read_meta(uploads, meta)
            return {"upload_id": upload_id, "received": self._part_size(uploads, part), **info}

    def append_chunk(self, upload_id: str, offset: int, data: bytes) -> int:
        part, meta = self._upload_names(upload_id)
        if len(data) > MAX_CHUNK:
            raise FileError("chunk too large")
        with self._own_dir(self.uploads) as uploads:
            info = self._read_meta(uploads, meta)
            current = self._part_size(uploads, part)
            if offset != current:
                raise FileError(f"expected offset {current}")
            if current + len(data) > info["size"]:
                raise FileError("more data than announced")
            fd = os.open(part, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=uploads)
            with os.fdopen(fd, "ab") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise FileError('upload data is not a regular file')
                handle.write(data)
        return current + len(data)

    def finish_upload(self, upload_id: str, overwrite: bool) -> str:
        part, meta = self._upload_names(upload_id)
        with self._own_dir(self.uploads) as uploads:
            info = self._read_meta(uploads, meta)
            if self._part_size(uploads, part) != info["size"]:
                raise FileError("upload is incomplete")
            # The metadata sits in the AI-writable workspace: check it again.
            name = self.check_name(str(info.get("name", "")))
            directory = self.resolve(str(info.get("dir", "")))
            if not directory.is_dir():
                raise FileError("the target folder is gone")
            target = directory / name
            if target.parent != directory or not (directory == self.root or directory.is_relative_to(self.root)):
                raise FileError("invalid target")
            with self._dir_fd(directory) as destination:
                try:
                    existing = os.stat(name, dir_fd=destination, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not overwrite:
                        raise FileError("a file with that name exists (choose overwrite)")
                    if not stat.S_ISREG(existing.st_mode):
                        raise FileError("cannot overwrite a folder or a link")
                try:
                    os.rename(part, name, src_dir_fd=uploads, dst_dir_fd=destination)
                except OSError as exc:
                    raise FileError(f"cannot move the upload into place ({exc.strerror})") from exc
            with contextlib.suppress(FileNotFoundError):
                os.unlink(meta, dir_fd=uploads)
        return self.rel(target)

    def cancel_upload(self, upload_id: str) -> None:
        part, meta = self._upload_names(upload_id)
        with self._own_dir(self.uploads) as uploads:
            for name in (part, meta):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=uploads)

    def clean_stale_uploads(self, max_age_seconds: int = 2 * 86_400) -> None:
        cutoff = time.time() - max_age_seconds
        with self._own_dir(self.uploads) as uploads:
            with os.scandir(uploads) as entries:
                for entry in entries:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff and not entry.is_dir(follow_symlinks=False):
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(entry.name, dir_fd=uploads)


def guess_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
