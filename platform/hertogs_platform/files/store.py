from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Link:
    rel_path: str
    filename: str
    expires_at: float


class LinkStore:
    """Expiring download links. Only SHA-256 hashes of tokens are stored."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                token_hash TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )

    def mint(self, rel_path: str, filename: str, ttl_seconds: int) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + ttl_seconds
        with self._lock:
            self._db.execute("DELETE FROM links WHERE expires_at < ?", (now,))
            self._db.execute(
                "INSERT INTO links (token_hash, rel_path, filename, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash(token), rel_path, filename, now, expires_at),
            )
        return token, expires_at

    def lookup(self, token: str) -> Link | None:
        with self._lock:
            row = self._db.execute(
                "SELECT rel_path, filename, expires_at FROM links "
                "WHERE token_hash = ? AND expires_at >= ?",
                (token_hash(token), time.time()),
            ).fetchone()
        if row is None:
            return None
        return Link(rel_path=row[0], filename=row[1], expires_at=row[2])
