"""SQLite state of the deployer: the single source of truth for the platform."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("mcp-hosting.deployer.db")

STAGES = ("dev", "shared", "public")

DEFAULT_SETTINGS: dict[str, Any] = {
    "quotas": {
        "max_dev_apps": 10,
        "max_apps": 20,
        "max_site_mb": 200,
        "max_upload_mb": 200,
        # Runtime component defaults and AI request ceilings.
        "app_memory_mb": 256,
        "app_cpus": 0.5,
        "app_data_gb": 1,
        "max_app_memory_mb": 4096,
        "max_app_cpus": 2,
        "max_build_image_mb": 16384,
        "max_data_export_mb": 1024,
        # Image builds: time limit, cache per app (trimmed after each build),
        # all caches together, and the free space a build needs on the Docker disk.
        "max_build_minutes": 60,
        "build_cache_app_gb": 20,
        "build_cache_total_gb": 60,
        "build_min_free_gb": 40,
        # How many of the newest revisions of an app keep their source and manifest.
        "kept_revisions": 10,
    },
    "gaming": {
        "workspace_cpus": 2,
        "workspace_memory_gb": 6,
        "app_cpus": 0.5,
        "stop_grace_seconds": 15,
        "games": [],
    },
    "log_access_defaults": {"dev": "full", "shared": "masked", "public": "masked"},
    "workspace_gpu_allowed": True,
    "access_lockdown": False,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    email TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_name TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
    email TEXT NOT NULL REFERENCES people(email) ON DELETE CASCADE,
    PRIMARY KEY (group_name, email)
);
CREATE TABLE IF NOT EXISTS apps (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'dev',
    online INTEGER NOT NULL DEFAULT 1,
    live_version TEXT,
    pending_version TEXT,
    previous_version TEXT,
    log_access TEXT NOT NULL DEFAULT 'default',
    log_access_until REAL,
    auto_release INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    stage_since REAL,
    protected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_access (
    app TEXT NOT NULL REFERENCES apps(name) ON DELETE CASCADE,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('person', 'group')),
    principal TEXT NOT NULL,
    PRIMARY KEY (app, principal_type, principal)
);
CREATE TABLE IF NOT EXISTS versions (
    app TEXT NOT NULL REFERENCES apps(name) ON DELETE CASCADE,
    version_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    files INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (app, version_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS secrets (
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    ciphertext BLOB,
    bound INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (app, name)
);
CREATE TABLE IF NOT EXISTS runtime_versions (
    app TEXT NOT NULL REFERENCES apps(name) ON DELETE CASCADE,
    version_id TEXT NOT NULL,
    manifest TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(app,version_id)
);
CREATE TABLE IF NOT EXISTS runtime_grants (
    app TEXT NOT NULL REFERENCES apps(name) ON DELETE CASCADE,
    version_id TEXT NOT NULL,
    secret TEXT NOT NULL,
    PRIMARY KEY(app,version_id,secret)
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    app TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def recover_from_snapshot(path: Path, snapshot: Path) -> bool:
    """After a restore, fall back to the pre-backup snapshot if the live copy is damaged."""
    if not path.exists() or not snapshot.exists():
        return False
    try:
        conn = sqlite3.connect(path)
        try:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        ok = False
    if ok:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        damaged = Path(str(path) + suffix)
        if damaged.exists():
            damaged.replace(Path(f"{path}.damaged-{stamp}{suffix}"))
    path.write_bytes(snapshot.read_bytes())
    return True


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(apps)")}
        if "protected" not in columns:
            self._conn.execute("ALTER TABLE apps ADD COLUMN protected INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("UPDATE apps SET protected = 1 WHERE stage != 'dev'")
        if "stage_since" not in columns:  # databases from the first Phase 1 build
            self._conn.execute("ALTER TABLE apps ADD COLUMN stage_since REAL")
            self._conn.execute("UPDATE apps SET stage_since = created_at")
        if "environment" not in columns:
            self._conn.execute("ALTER TABLE apps ADD COLUMN environment TEXT NOT NULL DEFAULT 'development'")
            self._conn.execute("UPDATE apps SET environment='production' WHERE protected=1")

    # -- plumbing ------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # SQLite may already have rolled back (e.g. "database or disk is
                # full"); a second ROLLBACK would hide the real error.
                if self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        logger.exception("rollback failed")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def snapshot(self, destination: Path) -> None:
        """Consistent copy of the database (run by backup.ps1 before copying the volume).

        Also folds the WAL into the main file, so the live copy in the backup is
        complete unless a write happens during the next seconds.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".tmp")
        with self._lock:
            target = sqlite3.connect(tmp)
            try:
                self._conn.backup(target)
            finally:
                target.close()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tmp.replace(destination)

    # -- settings ----------------------------------------------------------------
    def get_setting(self, key: str) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        default = DEFAULT_SETTINGS.get(key)
        if row is None:
            return json.loads(json.dumps(default))
        value = json.loads(row["value"])
        if isinstance(default, dict) and isinstance(value, dict):
            merged = dict(default)
            merged.update(value)
            return merged
        return value

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # -- audit ---------------------------------------------------------------------
    def audit(self, actor: str, action: str, detail: Any = "") -> None:
        """Record an action. A row that cannot be written (a full disk, which the
        AI can cause) goes to the container log instead: the audit never stops
        the action itself, least of all one that narrows access or stops GPU work."""
        text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True)
        try:
            with self.tx() as conn:
                conn.execute(
                    "INSERT INTO audit (time, actor, action, detail) VALUES (?, ?, ?, ?)",
                    (time.time(), actor, action, text[:4000]),
                )
        except sqlite3.Error as exc:
            logger.error("audit row not written (%s): %s %s %s", exc, actor, action, text[:500])

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    # -- access snapshot (to undo a change whose routes fail) --------------------------
    def snapshot_access(self) -> dict[str, list[tuple]]:
        with self._lock:
            return {
                "apps": [tuple(r) for r in self._conn.execute(
                    "SELECT name, stage, online, stage_since, auto_release FROM apps")],
                "app_access": [tuple(r) for r in self._conn.execute(
                    "SELECT app, principal_type, principal FROM app_access")],
                "group_members": [tuple(r) for r in self._conn.execute(
                    "SELECT group_name, email FROM group_members")],
            }

    def restore_access(self, snapshot: dict[str, list[tuple]]) -> None:
        with self.tx() as conn:
            conn.executemany(
                "UPDATE apps SET stage = ?, online = ?, stage_since = ?, auto_release = ? WHERE name = ?",
                [(stage, online, since, auto, name) for name, stage, online, since, auto in snapshot["apps"]],
            )
            conn.execute("DELETE FROM app_access")
            conn.executemany("INSERT OR IGNORE INTO app_access (app, principal_type, principal) VALUES (?, ?, ?)",
                             snapshot["app_access"])
            conn.execute("DELETE FROM group_members")
            conn.executemany("INSERT OR IGNORE INTO group_members (group_name, email) VALUES (?, ?)",
                             snapshot["group_members"])

    # -- people and groups -----------------------------------------------------------
    def people(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT email, display_name, created_at FROM people ORDER BY email")
        groups = self.query("SELECT group_name, email FROM group_members")
        by_email: dict[str, list[str]] = {}
        for row in groups:
            by_email.setdefault(row["email"], []).append(row["group_name"])
        return [dict(row) | {"groups": sorted(by_email.get(row["email"], []))} for row in rows]

    def groups(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT name FROM groups ORDER BY name")
        members = self.query("SELECT group_name, email FROM group_members ORDER BY email")
        by_group: dict[str, list[str]] = {}
        for row in members:
            by_group.setdefault(row["group_name"], []).append(row["email"])
        return [{"name": row["name"], "members": by_group.get(row["name"], [])} for row in rows]

    def person_exists(self, email: str) -> bool:
        return self.query_one("SELECT 1 FROM people WHERE email = ?", (email,)) is not None

    def group_exists(self, name: str) -> bool:
        return self.query_one("SELECT 1 FROM groups WHERE name = ?", (name,)) is not None

    # -- apps --------------------------------------------------------------------------
    def app(self, name: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM apps WHERE name = ?", (name,))
        if row is None:
            return None
        app = dict(row)
        app["protected"] = bool(app["protected"])
        app["online"] = bool(app["online"])
        app["auto_release"] = bool(app["auto_release"])
        access = self.query(
            "SELECT principal_type, principal FROM app_access WHERE app = ? ORDER BY principal",
            (name,),
        )
        app["people"] = [r["principal"] for r in access if r["principal_type"] == "person"]
        app["groups"] = [r["principal"] for r in access if r["principal_type"] == "group"]
        return app

    def apps(self) -> list[dict[str, Any]]:
        names = [row["name"] for row in self.query("SELECT name FROM apps ORDER BY name")]
        return [app for app in (self.app(name) for name in names) if app]

    def allowed_emails(self, app: dict[str, Any], owner_email: str) -> list[str]:
        emails = {owner_email.lower()}
        emails.update(email.lower() for email in app.get("people", []))
        if app.get("groups"):
            placeholders = ",".join("?" * len(app["groups"]))
            rows = self.query(
                f"SELECT email FROM group_members WHERE group_name IN ({placeholders})",
                tuple(app["groups"]),
            )
            emails.update(row["email"].lower() for row in rows)
        return sorted(emails)

    def versions(self, app: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM versions WHERE app = ? ORDER BY created_at DESC", (app,)
        )
        return [dict(row) for row in rows]

    # -- requests ------------------------------------------------------------------------
    def request(self, request_id: int) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM requests WHERE id = ?", (request_id,))
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def requests(self, status: str | None = None, app: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if app:
            clauses.append("app = ?")
            params.append(app)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT 500", tuple(params))
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            items.append(item)
        return items
