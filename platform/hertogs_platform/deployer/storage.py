"""Storage use, measured in the background.

Asking Docker for volume sizes takes many seconds on a large disk. Pages never
wait for it: a thread measures every INTERVAL seconds and on request, and pages
read the last result. Requests to refresh are merged into one running scan.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time

logger = logging.getLogger('hertogs.storage')
INTERVAL = 600
WARN, FULL, REARM = 80, 100, 5   # percent of the budget; warn again only after dropping REARM points below


class StorageMeter:
    def __init__(self, runtime) -> None:
        self.runtime, self.db, self.docker = runtime, runtime.db, runtime.docker
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self._volumes: dict[str, int] = {}
        self._cache_owners: dict[str, str | None] = {}
        self._development: dict[str, int | None] = {}
        self.measured_at: float | None = None
        self.error: str | None = None
        self.seconds: float | None = None
        self.disk: dict | None = None

    # -- measuring -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def refresh(self, wait: bool = False) -> bool:
        """Start a scan unless one is running. Returns True when this call started it."""
        with self._lock:
            started = self._done.is_set()
            if started:
                self._done.clear()
                threading.Thread(target=self._measure, daemon=True, name='storage-meter').start()
        if wait:
            self._done.wait(120)
        return started

    def _measure(self) -> None:
        began = time.monotonic()
        try:
            if not self.docker:
                raise RuntimeError('Docker is unavailable')
            volumes = self.docker.volume_usage()
            self._volumes = {v['Name']: int((v.get('UsageData') or {}).get('Size', -1)) for v in volumes}
            self._cache_owners = {v['Name']:(v.get('Labels') or {}).get('be.hertogs.buildcache') for v in volumes}
            self.measured_at, self.error = time.time(), None
            if hasattr(self.docker, 'writable_size'):
                for app in self.db.apps():
                    name = app['name']
                    try:
                        self._development[name] = self.docker.writable_size('hertogs-dev-'+name)
                    except Exception as exc:
                        if getattr(exc, 'status', None) == 404:
                            self._development[name] = 0
                        else:
                            self.error = 'Development storage measurement failed: '+str(exc)[:200]
        except Exception as exc:  # keep the previous values
            self.error = str(exc)[:300]
            logger.warning('storage measurement failed: %s', exc)
        try:
            usage = shutil.disk_usage(self.runtime.platform.settings.data_dir)
            self.disk = {'total_bytes': usage.total, 'free_bytes': usage.free}
        except OSError:
            self.disk = None
        self.seconds = round(time.monotonic() - began, 1)
        try:
            self._warn()
        except Exception:
            logger.exception('storage warnings failed')
        self._done.set()

    def pause(self) -> float:
        """Seconds until the next automatic scan: a slow disk is measured less often,
        so that measuring never takes more than about a twentieth of the time."""
        return max(INTERVAL, 20 * (self.seconds or 0))

    def loop(self, stop: threading.Event) -> None:
        self.refresh(wait=True)
        while not stop.wait(self.pause()):
            self.refresh(wait=True)
            try:
                self.maintenance()
            except Exception:
                logger.exception('daily storage maintenance failed')

    def maintenance(self) -> None:
        """Once a day, enforce cache budgets and clean recorded older images.
        Active apps are skipped so maintenance cannot interrupt their work."""
        now = time.time()
        if now - float(self.db.get_setting('storage-maintained-at') or 0) < 86400:
            return
        self.db.set_setting('storage-maintained-at', now)
        for app in self.db.apps():
            try:
                with self.runtime.platform.lock, self.runtime.lock:
                    if self.db.query_one("SELECT id FROM operations WHERE app=? AND status IN ('queued','running')", (app['name'],)):
                        continue
                    with self.runtime.exclusive(app['name']):
                        self.runtime.platform._prune(app['name'])
            except Exception as exc:  # busy right now, or Docker refused: tomorrow again
                logger.warning('daily maintenance skipped %s: %s', app['name'], exc)
        try:
            self.runtime.image_builds._make_room('')
        except ValueError as exc:  # little free space: the caches are gone; the next build says so
            logger.warning('daily maintenance: %s', exc)

    # -- reading -------------------------------------------------------------
    def state(self) -> dict:
        return {'measured_at': self.measured_at, 'running': self.running, 'error': self.error,
                'seconds': self.seconds, 'disk': self.disk}

    def volume(self, name: str) -> int | None:
        """Bytes, or None when unknown (never measured, missing, or Docker could not size it)."""
        size = self._volumes.get(name)
        return size if size is not None and size >= 0 else None

    def app(self, name: str) -> dict:
        """What an app's storage number is made of. Unknown parts are None, never 0."""
        versions = [row['version_id'] for row in self.db.query('SELECT version_id FROM runtime_versions WHERE app=?', (name,))]
        kept = self.db.query_one('SELECT COALESCE(SUM(bytes),0) AS total FROM versions WHERE app=?', (name,))
        prefix = 'hertogs-app-' + name
        parts = {
            'data_bytes': self.volume(prefix + '-data'),
            'source_volume_bytes': self._sum(f'{prefix}-{v}-source' for v in versions),
            'preview_data_bytes': self._sum(f'{prefix}-{v}-preview-data' for v in versions),
            'kept_sources_bytes': int(kept['total']) if kept else 0,
            'build_cache_bytes': self.volume(prefix + '-buildcache'),
            'development_bytes': self._development.get(name),
        }
        counted = [parts[key] for key in ('data_bytes', 'source_volume_bytes', 'preview_data_bytes', 'kept_sources_bytes', 'development_bytes')]
        total = sum(value for value in counted if value is not None)
        return {**parts, 'total_bytes': total if self.measured_at else None,
                'counts_toward_budget': ['data_bytes', 'source_volume_bytes', 'preview_data_bytes', 'kept_sources_bytes', 'development_bytes'],
                'not_measured': ['component logs', 'images (see Settings)'],
                'partial': bool(self.error) or self.measured_at is None,
                **self.state()}

    def _sum(self, names) -> int | None:
        sizes = [self.volume(name) for name in names]
        known = [size for size in sizes if size is not None]
        return sum(known) if known else None

    def summary(self, name: str) -> dict:
        """Used against budget, for lists of apps."""
        used, budget = self.app(name)['total_bytes'], self.budget(name)
        return {'used_bytes': used, 'budget_bytes': int(budget) if budget else None,
                'percent': round(used * 100 / budget) if budget and used is not None else None}

    def cache_volumes(self) -> dict[str, int]:
        return {name:max(0,size) for name,size in self._volumes.items()
                if name.startswith('hertogs-app-') and name.endswith('-buildcache') and
                self._cache_owners.get(name) == name[len('hertogs-app-'):-len('-buildcache')]}

    # -- warnings ------------------------------------------------------------
    def budget(self, name: str) -> float | None:
        """The storage budget of a runtime app in bytes, or None."""
        app = self.db.app(name)
        if not app or app['kind'] != 'runtime':
            return None
        limits = self.db.get_setting('runtime-resources:' + name) or (
            self.runtime.manifest(name, app['live_version']) if app['live_version'] else None)
        return float(limits['storage_gb']) * 1024**3 if limits and limits.get('storage_gb') else None

    def _warn(self) -> None:
        """Record a warning once when an app crosses 80% or 100% of its budget."""
        if self.measured_at is None:
            return
        for app in self.db.apps():
            budget = self.budget(app['name'])
            used = self.app(app['name'])['total_bytes']
            if not budget or used is None:
                continue
            percent = used * 100 / budget
            key = 'storage-warned:' + app['name']
            warned = int(self.db.get_setting(key) or 0)
            level = FULL if percent >= FULL else WARN if percent >= WARN else 0
            if level > warned:
                self.db.set_setting(key, level)
                self.db.audit('system', 'storage.warning', {'app': app['name'], 'percent': round(percent), 'level': level,
                                                            'used_bytes': used, 'budget_bytes': int(budget)})
            elif warned and percent < (warned if warned < FULL else FULL) - REARM and level < warned:
                self.db.set_setting(key, level)
