"""Owner-managed credentials for new workspace commands, never a management read API."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

from .db import Database

# Do not let credentials change the interpreter, service authentication, or shell setup.
RESERVED = {'PATH', 'HOME', 'SHELL', 'BASH_ENV', 'ENV', 'WORKSPACE', 'WORKSPACE_ROOT',
            'VIRTUAL_ENV', 'IFS', 'CONTROL_FILE', 'WORKSPACE_SECRETS_FILE'}
PREFIXES = ('LD_', 'DYLD_', 'PYTHON', 'DEPLOYER_', 'FILES_', 'CF_ACCESS_', 'NVIDIA_',
            'MCP_', 'PUBLIC_', 'XDG_', 'PIP_', 'UV_', 'HF_', 'TORCH_')


def validate_name(name: str) -> str:
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', name):
        raise ValueError('Use an uppercase environment name, such as API_TOKEN.')
    if name in RESERVED or name.startswith(PREFIXES):
        raise ValueError('That name is reserved for workspace configuration.')
    return name


class WorkspaceCredentials:
    scope = '@workspace'

    def __init__(self, db: Database, key: str, directory: Path):
        self.db, self.directory = db, directory
        self.fernet = Fernet(key.encode()) if key else None
        self.lock = threading.RLock()

    def names(self) -> list[dict]:
        return [dict(row) for row in self.db.query(
            'SELECT name, updated_at FROM secrets WHERE app = ? ORDER BY name', (self.scope,))]

    def publish(self) -> None:
        """Atomic file rotation; no token is stored in job descriptions or process arguments."""
        with self.lock:
            values = {}
            rows = self.db.query('SELECT name, ciphertext FROM secrets WHERE app = ?', (self.scope,))
            if rows and self.fernet is None:
                raise ValueError('Workspace encryption key is not configured.')
            for row in rows:
                validate_name(row['name'])
                values[row['name']] = self.fernet.decrypt(row['ciphertext']).decode()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.directory / '.environment.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump(values, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.directory / 'environment.json')

    def set(self, name: str, value: str, actor: str) -> None:
        validate_name(name)
        if not self.fernet:
            raise ValueError('Workspace encryption key is not configured.')
        if not value or len(value.encode()) > 16 * 1024 or '\0' in value:
            raise ValueError('Enter a nonempty token up to 16 KB, without null characters.')
        with self.lock:
            with self.db.tx() as conn:
                conn.execute('INSERT INTO secrets (app,name,ciphertext,bound,updated_at) VALUES (?,?,?,1,?) '
                             'ON CONFLICT(app,name) DO UPDATE SET ciphertext=excluded.ciphertext, '
                             'updated_at=excluded.updated_at',
                             (self.scope, name, self.fernet.encrypt(value.encode()), time.time()))
            self.publish()
            self.db.audit(actor, 'workspace.token.set', {'name': name})

    def delete(self, name: str, actor: str) -> None:
        validate_name(name)
        with self.lock:
            with self.db.tx() as conn:
                conn.execute('DELETE FROM secrets WHERE app=? AND name=?', (self.scope, name))
            self.publish()
            self.db.audit(actor, 'workspace.token.delete', {'name': name})
