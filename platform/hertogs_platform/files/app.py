from __future__ import annotations

import hmac
import ipaddress
import logging
import mimetypes
import os
import re
import stat
from collections.abc import AsyncIterator
from email.utils import formatdate
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import FilesSettings
from .store import LinkStore

logger = logging.getLogger("hertogs.files")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
# At most 19 digits: anything longer is not a real offset (and int() of
# thousands of digits is slow and raises).
_RANGE_RE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")
_CHUNK = 1024 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def safe_filename(value: str | None, fallback: str = "download") -> str:
    name = Path(value or fallback).name.strip().replace("\x00", "")
    name = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    return name[:240] or fallback


def open_workspace_file(root: Path, rel_path: str) -> tuple[int, os.stat_result]:
    """Open a regular file inside root without following an escape via symlinks.

    The path is resolved, opened with O_NOFOLLOW, and the opened descriptor's real
    path is checked again, so swapping a component for a symlink between the check
    and the open cannot reach files outside the workspace.
    """
    if not rel_path or "\x00" in rel_path or rel_path.startswith("/"):
        raise FileNotFoundError(rel_path)
    try:
        candidate = (root / rel_path).resolve(strict=True)
    except RuntimeError as exc:  # symlink loop (Python < 3.13)
        raise FileNotFoundError(rel_path) from exc
    if not candidate.is_relative_to(root):
        raise FileNotFoundError(rel_path)
    # O_NONBLOCK: opening a FIFO must not block the event loop; it is rejected below.
    fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError(rel_path)
        real = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if not real.is_relative_to(root):
            raise FileNotFoundError(rel_path)
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Return (start, end_inclusive), None for a full response, or "invalid" for 416."""
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None  # Multiple or malformed ranges: serve the whole file.
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0:
            return "invalid"
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        end = min(end, size - 1)
    if start >= size or start > end:
        return "invalid"
    return start, end


async def _stream(handle: BinaryIO, start: int, length: int) -> AsyncIterator[bytes]:
    # The file object (not a bare fd) is closed by the garbage collector even if
    # the client disconnects before this generator ever runs.
    try:
        offset = start
        remaining = length
        while remaining > 0:
            data = await anyio.to_thread.run_sync(os.pread, handle.fileno(), min(_CHUNK, remaining), offset)
            if not data:
                break
            offset += len(data)
            remaining -= len(data)
            yield data
    finally:
        handle.close()


def _content_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def build_public_app(settings: FilesSettings, store: LinkStore) -> Starlette:
    """files.example.com: GET/HEAD /files/<token>/<name> only; everything else is 404."""

    def not_found() -> Response:
        return PlainTextResponse("Not found", status_code=404, headers=SECURITY_HEADERS)

    async def download(request: Request) -> Response:
        token = request.path_params["token"]
        if not _TOKEN_RE.match(token):
            return not_found()
        link = store.lookup(token)
        if link is None:
            return not_found()
        try:
            fd, info = open_workspace_file(settings.workspace, link.rel_path)
        except (OSError, ValueError):
            return not_found()
        handle = os.fdopen(fd, "rb", buffering=0)
        try:
            return _download_response(request, link.rel_path, link.filename, handle, info)
        except BaseException:
            handle.close()
            raise

    def _download_response(request: Request, rel_path: str, filename: str, handle: BinaryIO,
                           info: os.stat_result) -> Response:
        size = info.st_size
        headers = dict(SECURITY_HEADERS)
        headers["Content-Disposition"] = _content_disposition(filename)
        headers["Accept-Ranges"] = "bytes"
        # Validators let browsers and download managers resume safely.
        etag = f'"{info.st_ino:x}-{size:x}-{info.st_mtime_ns:x}"'
        last_modified = formatdate(info.st_mtime, usegmt=True)
        headers["ETag"] = etag
        headers["Last-Modified"] = last_modified

        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if if_range and if_range not in (etag, last_modified):
            range_header = None  # the file changed: send all of it
        requested = _parse_range(range_header, size)
        if requested == "invalid":
            handle.close()
            headers["Content-Range"] = f"bytes */{size}"
            return Response(status_code=416, headers=headers)
        if requested is None:
            start, length, status_code = 0, size, 200
        else:
            start, end = requested
            length = end - start + 1
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)

        client = request.headers.get("cf-connecting-ip", "-")
        logger.info("download %s path=%s bytes=%d client=%s", request.method, rel_path, length, client)

        if request.method == "HEAD":
            handle.close()
            return Response(status_code=status_code, headers=headers, media_type="application/octet-stream")
        return StreamingResponse(
            _stream(handle, start, length),
            status_code=status_code,
            headers=headers,
            media_type="application/octet-stream",
        )

    async def fallback(_: Request) -> Response:
        return not_found()

    return Starlette(
        routes=[
            Route("/files/{token:str}/{filename:path}", download, methods=["GET", "HEAD"]),
            Route("/{rest:path}", fallback, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        ]
    )


def build_mint_app(settings: FilesSettings, store: LinkStore) -> Starlette:
    """Internal port for the workspace: mint links with a bearer token.

    cloudflared shares a network with this container, so minting is also limited
    to client addresses in FILES_MINT_ALLOWED_CLIENTS (the files-mint subnet).
    """
    networks = [ipaddress.ip_network(net, strict=False) for net in settings.mint_allowed_clients]

    def client_allowed(request: Request) -> bool:
        if not networks:
            return True
        try:
            address = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            return False
        return any(address in network for network in networks)

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(supplied.encode(), settings.mint_token.encode())

    async def mint(request: Request) -> Response:
        if not client_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body: Any = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        rel_path = body.get("path")
        if not isinstance(rel_path, str) or len(rel_path) > 4096:
            return JSONResponse({"error": "path must be a string"}, status_code=400)
        try:
            ttl = int(body.get("ttl_seconds", 3600))
        except (TypeError, ValueError):
            return JSONResponse({"error": "ttl_seconds must be an integer"}, status_code=400)
        ttl = max(60, min(ttl, settings.max_ttl_seconds))

        try:
            fd, info = open_workspace_file(settings.workspace, rel_path)
        except PermissionError:
            return JSONResponse(
                {"error": "the files service (uid 1000) cannot read this file; run "
                          "'sudo chmod a+r' on it (and a+x on its folders) or 'sudo chown ai' it"},
                status_code=403,
            )
        except (OSError, ValueError):
            return JSONResponse({"error": "not a regular file inside /workspace"}, status_code=404)
        os.close(fd)

        filename = safe_filename(body.get("filename") or Path(rel_path).name)
        token, expires_at = store.mint(rel_path, filename, ttl)
        logger.info("minted link path=%s ttl=%d", rel_path, ttl)
        return JSONResponse(
            {
                "path": rel_path,
                "filename": filename,
                "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "size_bytes": info.st_size,
                "expires_at_unix": expires_at,
                "ttl_seconds": ttl,
                "download_url": f"{settings.public_files_url}/files/{token}/{quote(filename)}",
            }
        )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/internal/links", mint, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )
