"""Rehearse database migration on a consistent temporary copy; never change the source.

Run in the test image with the production data volume mounted read-only:
  python scripts/check-upgrade.py /live/deployer.sqlite3
No credential values, manifests or request contents are printed.
"""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'platform'))
from mcp_hosting_platform.deployer.db import Database
from mcp_hosting_platform.deployer.capabilities import permissions


def main(source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='workspace-upgrade-') as directory:
        snapshot = Path(directory) / 'deployer.sqlite3'
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as live:
            with sqlite3.connect(snapshot) as copy:
                live.backup(copy)
        with sqlite3.connect(snapshot) as before:
            tables = [r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            original = {name: before.execute('SELECT * FROM "'+name+'" ORDER BY rowid').fetchall() for name in tables}
        db = Database(snapshot)
        try:
            # Migration may add columns; all pre-existing data must stay identical.
            for name, rows in original.items():
                migrated = [tuple(row)[:len(rows[0])] for row in db.query('SELECT * FROM "'+name+'" ORDER BY rowid')] if rows else []
                assert migrated == rows, f'Migration changed existing rows in {name}'
            for app in db.apps():
                saved = db.get_setting('ai-permissions:'+app['name'])
                if saved is not None:
                    checked = permissions(saved)
                    for key in ('call_read', 'call_write', 'call_destructive'):
                        assert checked[key] == saved.get(key, False)
            assert db.query_one('PRAGMA integrity_check')[0] == 'ok'
            assert db.query('PRAGMA foreign_key_check') == []
            active = db.query_one("SELECT COUNT(*) FROM operations WHERE status IN ('queued','running')")[0]
            print(json.dumps({'migration':'passed on a consistent temporary copy', 'source_modified':False,
                              'apps':len(db.apps()), 'active_operations':active,
                              'quotas':{key:db.get_setting('quotas')[key] for key in
                                        ('kept_revisions','max_build_minutes','build_cache_app_gb','build_cache_total_gb','build_min_free_gb')},
                              'new_action_permissions':'off unless already explicitly granted'}))
        finally:
            db._conn.close()


if __name__ == '__main__':
    main(Path(sys.argv[1]))
