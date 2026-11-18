"""Per-operation log files: what an operation is doing right now.

One text file per operation on the deployer volume. The operation appends phase
lines; for image builds a follower thread appends the build worker's output while
it runs. A log is capped: up to CAP bytes are written as they arrive, after that
only the newest TAIL bytes are remembered and the file is rewritten at the end as
"first HEAD bytes, a marker, last TAIL bytes".
"""
from __future__ import annotations

import collections
import re
import threading
import time
from pathlib import Path

CAP = 5 * 1024 * 1024
HEAD = 1024 * 1024
TAIL = 4 * 1024 * 1024
KEEP_PER_APP = 20
MARKER = '\n[... middle of the log removed: it was larger than 5 MB ...]\n'
_ID = re.compile(r'[0-9a-f]{32}')


class _Writer:
    def __init__(self, path: Path) -> None:
        self.path, self.written, self.total = path, 0, 0
        self.recent: collections.deque[bytes] = collections.deque()
        self.recent_bytes = 0
        self.lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            self.total += len(data)
            self.recent.append(data)
            self.recent_bytes += len(data)
            while self.recent_bytes - len(self.recent[0]) >= TAIL:
                self.recent_bytes -= len(self.recent.popleft())
            if self.written >= CAP:
                return
            room = CAP - self.written
            piece = data[:room]
            if len(data) > room:
                piece += b'\n[log is larger than 5 MB; the last part is added when the operation ends]\n'
            try:
                with self.path.open('ab') as handle:
                    handle.write(piece)
            except OSError:
                pass  # a full disk must never stop the operation itself
            self.written += len(data[:room])

    def close(self) -> bool:
        """Rewrite an overflowed log as head + marker + tail. True when it was cut."""
        with self.lock:
            if self.total <= CAP:
                return False
            tail = b''.join(self.recent)[-TAIL:]
            try:
                head = self.path.read_bytes()[:HEAD]
                self.path.write_bytes(head + MARKER.encode() + tail)
            except OSError:
                pass
            return True


class OperationLogs:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._writers: dict[str, _Writer] = {}
        self._lock = threading.Lock()

    def _path(self, operation: str) -> Path:
        if not _ID.fullmatch(operation):
            raise ValueError('Invalid operation id.')
        return self.directory / (operation + '.log')

    def _writer(self, operation: str) -> _Writer:
        with self._lock:
            writer = self._writers.get(operation)
            if writer is None:
                self.directory.mkdir(parents=True, exist_ok=True)
                writer = self._writers[operation] = _Writer(self._path(operation))
            return writer

    def line(self, operation: str, text: str) -> None:
        """A line written by the platform itself, with the time in front."""
        try:
            self._writer(operation).append((time.strftime('%H:%M:%S ') + text.rstrip('\n') + '\n').encode('utf-8', 'replace'))
        except (OSError, ValueError):
            pass

    def raw(self, operation: str, data: bytes) -> None:
        try:
            self._writer(operation).append(data)
        except (OSError, ValueError):
            pass

    def close(self, operation: str) -> bool:
        with self._lock:
            writer = self._writers.pop(operation, None)
        return writer.close() if writer else False

    def read(self, operation: str, offset: int = 0, limit: int = 256 * 1024) -> dict:
        path = self._path(operation)
        try:
            size = path.stat().st_size
        except OSError:
            return {'text': '', 'offset': 0, 'next_offset': 0, 'size': 0}
        offset = max(0, min(int(offset), size))
        with path.open('rb') as handle:
            handle.seek(offset)
            data = handle.read(max(1, min(int(limit), 1024 * 1024)))
        return {'text': data.decode('utf-8', 'replace'), 'offset': offset, 'next_offset': offset + len(data), 'size': size}

    def tail(self, operation: str, lines: int = 50, max_bytes: int = 16 * 1024) -> str:
        path = self._path(operation)
        try:
            size = path.stat().st_size
            with path.open('rb') as handle:
                handle.seek(max(0, size - max_bytes))
                text = handle.read(max_bytes).decode('utf-8', 'replace')
        except OSError:
            return ''
        return '\n'.join(text.splitlines()[-lines:])

    def remove(self, operation: str) -> None:
        try:
            if operation not in self._writers:
                self._path(operation).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def exists(self, operation: str) -> bool:
        try:
            return self._path(operation).is_file()
        except ValueError:
            return False

    def prune(self, keep: set[str]) -> None:
        """Remove log files of operations that are no longer listed."""
        try:
            for path in self.directory.glob('*.log'):
                if path.stem not in keep and path.stem not in self._writers:
                    path.unlink(missing_ok=True)
        except OSError:
            pass
