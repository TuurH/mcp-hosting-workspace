"""Extract untrusted tar archives (uploaded by the AI) without escaping the target."""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CHUNK = 1024 * 1024
# Long names and PAX headers are read into memory by tarfile before any member
# is returned, so they are capped while being parsed: each one, and all of them
# together. Global PAX headers (copied into every later member) and sparse files
# (whose maps tarfile reads into memory without a limit) are refused.
MAX_METADATA_BYTES = 64 * 1024
MAX_METADATA_TOTAL = 1024 * 1024
_METADATA_TYPES = (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
_REFUSED_TYPES = (tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_SPARSE)


class UnsafeArchive(ValueError):
    pass


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, tarfile_obj):  # type: ignore[override]
        # Plain HeaderError (not EOFHeaderError, which tarfile treats as a quiet
        # end of the archive).
        if self.type in _REFUSED_TYPES:
            raise tarfile.HeaderError("global PAX headers and sparse files are not allowed")
        if self.type in _METADATA_TYPES:
            if self.size > MAX_METADATA_BYTES:
                raise tarfile.HeaderError("metadata header too large")
            total = getattr(tarfile_obj, "_hertogs_metadata", 0) + self.size
            if total > MAX_METADATA_TOTAL:
                raise tarfile.HeaderError("too much metadata in the archive")
            tarfile_obj._hertogs_metadata = total
        return super()._proc_member(tarfile_obj)

    # PAX sparse formats 0.0, 0.1 and 1.0: tarfile calls these on the PAX header
    # before returning the member (1.0 reads its map from the member's data).
    def _proc_gnusparse_00(self, *args, **kwargs):  # type: ignore[override]
        raise tarfile.HeaderError("sparse files are not allowed")

    _proc_gnusparse_01 = _proc_gnusparse_00
    _proc_gnusparse_10 = _proc_gnusparse_00


@dataclass(frozen=True)
class ExtractResult:
    files: int
    total_bytes: int
    digest: str  # sha256 over (path, sha256(content)) of every file, sorted by path


def _check_member_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchive(f"bad entry name {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise UnsafeArchive(f"absolute path {name!r}")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise UnsafeArchive(f"path escapes the archive root: {name!r}")
    if ".git" in parts:
        raise UnsafeArchive(".git folders are not allowed")
    return PurePosixPath(*parts)


def extract_untrusted(
    archive_path: Path,
    target: Path,
    *,
    max_files: int = 20_000,
    max_total_bytes: int = 500 * 1024 * 1024,
    max_file_bytes: int = 200 * 1024 * 1024,
) -> ExtractResult:
    """Extract a .tar or .tar.gz file into a new directory, streaming file by file.

    Only regular files and directories are accepted: symlinks, hard links,
    devices and FIFOs make the whole archive invalid. Permissions are normalised
    (0644 files, 0755 directories); ownership is not restored. On any error the
    caller should delete ``target``.
    """
    target.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, bytes] = {}
    total = 0
    count = 0
    try:
        archive = tarfile.open(archive_path, mode="r:*", tarinfo=_BoundedTarInfo)
    except (tarfile.TarError, OSError) as exc:
        raise UnsafeArchive(f"not a tar archive: {exc}") from exc

    try:
        with archive:
            while True:
                member = archive.next()
                if member is None:
                    break
                # tarfile keeps every member it has read; drop them as we go so
                # memory does not grow with the number of entries.
                archive.members.clear()
                count += 1
                if count > max_files:
                    raise UnsafeArchive(f"too many entries (more than {max_files})")
                relative = _check_member_name(member.name)
                destination = target / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    # Also refuses continuation entries (sparse ones never get here).
                    raise UnsafeArchive(f"only regular files and folders are allowed: {member.name!r}")
                if member.size > max_file_bytes:
                    raise UnsafeArchive(f"{member.name!r} is larger than {max_file_bytes} bytes")
                total += member.size
                if total > max_total_bytes:
                    raise UnsafeArchive(f"the site is larger than {max_total_bytes // (1024 * 1024)} MB")
                key = str(relative)
                if key in hashes:
                    raise UnsafeArchive(f"duplicate entry {key!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                digest = hashlib.sha256()
                # O_NOFOLLOW + O_EXCL: nothing pre-existing or linked can be written through.
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                os.fchmod(fd, 0o644)  # readable by the static server whatever the umask
                written = 0
                with os.fdopen(fd, "wb") as handle:
                    while source is not None:
                        chunk = source.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > member.size:
                            raise UnsafeArchive(f"{member.name!r} holds more data than its header says")
                        digest.update(chunk)
                        handle.write(chunk)
                hashes[key] = digest.digest()
    except UnsafeArchive:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        # Broken archive, or entries that clash (a file and a folder with the same name).
        raise UnsafeArchive(f"invalid archive: {exc}") from exc

    combined = hashlib.sha256()
    for key in sorted(hashes):
        combined.update(key.encode() + b"\0" + hashes[key])
    for root, dirs, _files in os.walk(target):
        for name in dirs:
            os.chmod(os.path.join(root, name), stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    os.chmod(target, 0o755)
    return ExtractResult(files=len(hashes), total_bytes=total, digest=combined.hexdigest())
