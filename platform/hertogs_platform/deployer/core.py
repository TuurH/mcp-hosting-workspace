"""Platform operations shared by the narrow (AI) and full (owner) APIs.

Every method that widens exposure is only reachable from the full API; the
narrow API files a request instead. All state changes are audit-logged.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..common.names import name_problem
from . import pomerium
from .config import DeployerSettings
from .db import STAGES, Database
from .sites import Sites

logger = logging.getLogger("hertogs.deployer")

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
_GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# What the AI can ask the owner for. Each ask is shown on the app's own page, in
# the section it belongs to. A pending version is not an ask: it waits on the app
# page by itself. Stopping an app needs no ask (the AI may always do that), and
# deleting a protected app is the owner's own action. Asks of the retired kinds
# "release", "stop" and "remove" filed by an older build can still be decided.
REQUEST_KINDS = ("stage", "start", "log_access", "secret", "capabilities", "development", "data_export", "rollback")
DATA_CHANGES = ("none", "compatible", "irreversible")


class PlatformError(ValueError):
    """A request that cannot be fulfilled (shown to the caller)."""


class RouteError(RuntimeError):
    """Pomerium misbehaved after a change that let more people in. ``undone`` says
    whether the change was rolled back (inside ``_widening``) or kept."""

    undone = False


class Platform:
    def __init__(self, settings: DeployerSettings, db: Database, sites: Sites) -> None:
        from .workspace import WorkspaceCredentials
        self.workspace_credentials = WorkspaceCredentials(db, settings.secrets_key, settings.data_dir / "workspace-secrets")
        self.settings = settings
        self.db = db
        self.sites = sites
        self.lock = threading.RLock()
        self._fernet = Fernet(settings.secrets_key.encode()) if settings.secrets_key else None
        self.site_problems: dict[str, str] = {}   # app -> why its files cannot be served
        self.route_problems: list[str] = []       # last routing check that failed
        # Hosts whose wider database access is not (yet) proven safe: host -> (retry at, why).
        self._held: dict[str, tuple[float, str]] = {}
        self._last_probe = 0.0
        self._write_failed = False
        self._probe_now = False                      # check after a write that failed before
        self.runtime = None
        self._pending_restore: dict[str, Any] | None = None  # narrower config whose write failed

    # ------------------------------------------------------------------ helpers
    def _app_or_error(self, name: str) -> dict[str, Any]:
        app = self.db.app(name)
        if app is None:
            raise PlatformError(f"no app named '{name}'")
        return app

    def _reserved(self) -> frozenset[str]:
        return frozenset(self.settings.extra_reserved)

    def describe(self, app: dict[str, Any]) -> dict[str, Any]:
        name = app["name"]
        held=bool(app['kind']=='runtime' and self.runtime and app['live_version'] and
                  (self.runtime.manifest(name,app['live_version']) or {}).get('capabilities',{}).get('gpu') and self.runtime.gpu_blocked())
        access_held=bool(app['kind']=='runtime' and self.runtime and self.runtime.access_held(self.runtime.manifest(name,app['live_version'])))
        return {
            "name": name,
            "kind": app["kind"],
            "stage": app["stage"],
            "environment": app["environment"],
            "ai_permissions": self.ai_permissions(app),
            "protected": app["protected"],
            "effective_visibility": "owner only (emergency lock)" if self.db.get_setting("access_lockdown") else app["stage"],
            "lifecycle": "held for emergency access lock" if access_held and app['online'] else "held for gaming" if held and app['online'] else "online" if app["online"] else "offline",
            "held_for_gaming": held,
            "online": app["online"],
            "url": f"https://{self.settings.host_for(name)}",
            "preview_url": f"https://{self.settings.preview_host_for(name)}" if app["pending_version"] else None,
            "live_version": app["live_version"],
            "pending_version": app["pending_version"],
            "previous_version": app["previous_version"],
            "people": app["people"],
            "groups": app["groups"],
            "log_access": self.effective_log_access(app),
            "auto_release": app["auto_release"],
            "updated_at": app["updated_at"],
            "problem": self.site_problems.get(name),
        }

    def ai_permissions(self, app):
        from .capabilities import AI_ACTIONS
        saved = self.db.get_setting('ai-permissions:' + app['name'])
        if saved is not None:
            # Permissions added by a later build are off until the owner turns them on.
            return {**{key: False for key in AI_ACTIONS}, **saved}
        autonomous = not app['protected'] and app['stage'] == 'dev'
        return {'install_packages': False, 'run_tests': True, 'restart': autonomous,
                'deploy': app['kind'] == 'static' and (autonomous or app['auto_release'] and app['stage'] != 'public'),
                'delete': autonomous, 'call_read': False, 'call_write': False, 'call_destructive': False}

    def app_url(self, name: str, section: str = "") -> str:
        """The app's page on the admin site: the one place where the owner decides."""
        return f"{self.settings.admin_url}/apps/{name}" + (f"#{section}" if section else "")

    def data_change(self, name: str, version: str | None) -> str | None:
        return self.db.get_setting(f"data-change:{name}:{version}") if version else None

    def record_data_change(self, name: str, version: str, value: str) -> None:
        """What the AI said about app data when it uploaded this revision (unverified)."""
        if value:
            if value not in DATA_CHANGES:
                raise PlatformError("data_change must be none, compatible or irreversible")
            self.db.set_setting(f"data-change:{name}:{version}", value)

    def revisions(self, name: str) -> list[dict[str, Any]]:
        """Every kept revision with its role and what going back to it takes."""
        app = self._app_or_error(name)
        roles = {app["live_version"]: "live", app["pending_version"]: "pending", app["previous_version"]: "previous"}
        result = []
        for row in self.db.versions(name):
            version = row["version_id"]
            manifest = self.runtime.manifest(name, version) if app["kind"] == "runtime" and self.runtime else None
            if app["kind"] == "runtime" and not manifest:
                continue  # the raw upload a revision was made from
            role = roles.get(version, "older")
            item = {"version": version, "role": role, "created_at": row["created_at"], "note": row["note"],
                    "data_change": self.data_change(name, version) or "not stated"}
            if manifest is None:
                item.update(image="none (static files)", going_back="instant")
            elif "dockerfile" in manifest:
                built = bool(self.db.get_setting(f"built-image:{name}:{version}"))
                rebuildable = self.db.get_setting(f"build-network:{name}:{version}") is not None
                item.update(image="kept" if built else "removed, rebuild needed" if rebuildable else "not built",
                            going_back="instant" if built and role == "previous" else
                            "rebuild from the kept source, then release (minutes)" if rebuildable or built else
                            "upload it again as a new version")
            elif manifest.get("image_source"):
                kept = role != "older"
                item.update(image="kept" if kept else "removed, cannot be rebuilt (development snapshot)",
                            going_back="instant" if role == "previous" else "release it again" if kept else "not possible")
            else:
                item.update(image="uses an installed base image", going_back="instant" if role == "previous" else "release it again")
            result.append(item)
        return result

    def set_policy(self, name, environment, permissions, actor):
        from .capabilities import ENVIRONMENTS, permissions as validate
        with self.lock:
            self._app_or_error(name)
            if environment not in ENVIRONMENTS:
                raise PlatformError('Environment must be development, staging or production.')
            clean = validate(permissions)
            with self.db.tx() as conn:
                conn.execute('UPDATE apps SET environment=?,updated_at=? WHERE name=?', (environment,time.time(),name))
                conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', ('ai-permissions:'+name,json.dumps(clean)))
            if not clean['install_packages'] and self.runtime:
                self.runtime.development.stop(name)
            self.db.audit(actor,'app.policy',{'app':name,'environment':environment,'permissions':clean})
            return self.describe(self._app_or_error(name))

    def routing_report(self) -> dict[str, Any]:
        import hashlib
        def digest(config: Any) -> str | None:
            return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() if config else None
        desired, _ = pomerium.render(self.settings, self.db)
        applied = pomerium.read_config(self.settings.pomerium_config)
        checked = pomerium.read_config(self._checked_path())
        return {"desired":digest(desired), "applied":digest(applied), "last_checked":digest(checked),
                "in_sync":digest(desired)==digest(applied),
                "anonymous_check_current":digest(applied)==digest(checked) and bool(checked),
                "authenticated_audience_verified":False,
                "held_hosts":sorted(self._held), "problems":self.route_problems}

    # ------------------------------------------------------------------ logs
    def log_salt(self) -> str:
        """Key for the nicknames in masked logs. Random, stored here, never given to the AI."""
        salt = self.db.get_setting("log_salt")
        if salt:  # the usual case: no platform lock, so pages never wait behind long work
            return str(salt)
        with self.lock:
            salt = self.db.get_setting("log_salt")
            if not salt:
                salt = secrets.token_hex(32)
                self.db.set_setting("log_salt", salt)
            return str(salt)

    def log_window_start(self, app: dict[str, Any]) -> float:
        """Visitor lines before this moment are always masked for the AI.

        Full access that comes with Dev covers only the time since the app became
        Dev (or was created), so moving a Public app back to Dev does not reveal
        its earlier visitors. Full access the owner grants explicitly covers all.
        """
        if app["log_access"] == "full" and (app["log_access_until"] or 0) >= time.time():
            return 0.0
        return max(float(app["created_at"] or 0), float(app.get("stage_since") or 0))

    def effective_log_access(self, app: dict[str, Any]) -> str:
        mode = app["log_access"]
        if mode == "full" and (app["log_access_until"] or 0) < time.time():
            mode = "default"
        if mode == "default":
            mode = self.db.get_setting("log_access_defaults").get(app["stage"], "masked")
        return mode

    # ------------------------------------------------------------------ routes
    def sync_sites(self) -> None:
        wanted: dict[str, tuple[str, str]] = {}
        for app in self.db.apps():
            if app["kind"] != "static":
                continue
            if app["online"] and app["live_version"]:
                wanted[self.settings.host_for(app["name"])] = (app["name"], app["live_version"])
            if app["pending_version"]:
                wanted[self.settings.preview_host_for(app["name"])] = (app["name"], app["pending_version"])
        problems: dict[str, str] = {}
        for host, (name, version) in wanted.items():
            if not self.sites.version_dir(name, version).is_dir():
                # One app with missing files (e.g. after a partial restore) must not
                # stop every other route from updating. Its host then answers 404.
                problems[name] = f"the files of version {version} are missing; deploy it again"
                logger.error("%s: %s", name, problems[name])
                self.sites.point(host, None, None)
                continue
            if self.sites.pointed_version(host) != version:
                self.sites.point(host, name, version)
        self.site_problems = problems
        self.sites.ensure_platform_page(
            self.settings.canary_host,
            "<!doctype html><title>canary</title><p>canary ok</p>\n",
        )
        for host in self.sites.hosts():
            if host not in wanted and host != self.settings.canary_host:
                self.sites.point(host, None, None)

    def apply_routes(self, actor: str = "system", probe: bool = True, *,
                     baseline: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sync sites, rewrite Pomerium's config from the database and check it.

        One rule for every caller: a host only ever admits someone the file on
        disk did not after a check of it passed. A host that widens but cannot
        be checked now (``probe=False``, Pomerium not reachable) keeps its old
        access and is tried again by the next call. When the check fails, the
        widened hosts go back to admitting only the people both versions admit
        (narrowings stay); if the caller's own change widened them (``baseline``
        is the access before it, rendered from the database), RouteError is
        raised and the caller undoes it (see ``_widening``); otherwise the
        database was already wider (for example after a restore) and those
        hosts are held back and tried again ten minutes later. Every other change
        is still written.
        """
        with self.lock:
            if self._pending_restore is not None:
                # A narrower config whose write failed: put it back before anything else.
                self._write_routes(actor, self._pending_restore)
                self._pending_restore = None
            on_disk = pomerium.read_config(self.settings.pomerium_config)
            # The last config known safe. Normally the file on disk; after a
            # restart in the middle of checking a wider config, the narrower one
            # recorded before that check.
            reference = pomerium.read_config(self._checked_path()) or on_disk
            owner = self.settings.owner_email
            sync_error = None
            try:
                self.sync_sites()
            except Exception as exc:  # e.g. a full or read-only sites volume
                sync_error = f"updating the site files failed: {exc}"
                logger.exception("sync_sites failed")
            config, expectations = pomerium.render(self.settings, self.db)
            own = pomerium.widened_hosts(baseline, config, owner) if baseline is not None else set()
            touched = pomerium.changed_hosts(baseline, config) if baseline is not None else set()
            requested = bool(own)
            now = time.monotonic()
            # Held hosts whose wider access the database no longer asks for are settled.
            asked = pomerium.widened_hosts(reference, config, owner)
            self._held = {host: held for host, held in self._held.items() if held[0] > now and host in asked}
            # Held hosts keep their previous access, except those the caller is
            # changing now: those are written and checked at once.
            masked = set(self._held) - touched
            if masked:
                config = pomerium.without_widening(config, reference, owner, hosts=masked)
                expectations = [e for e in expectations if e.host not in masked]
            widened = pomerium.widened_hosts(reference, config, owner)
            if widened and not probe:
                # Nothing checks this write: no host widens until a check passes.
                config = pomerium.without_widening(config, reference, owner, hosts=widened)
                self._hold(widened, now, "waiting for a routing check")
                widened = set()
            if sync_error and (requested or widened):
                self.route_problems = [sync_error]
                raise RouteError(sync_error)
            if widened:
                # Record the narrower config first, so a restart during the check
                # falls back to it instead of keeping the wider access unchecked.
                self._save_checked(actor, pomerium.without_widening(config, reference, owner, hosts=widened),
                                   required=True)
            changed = self._write_routes(actor, config)
            problems: list[str] = [sync_error] if sync_error else []
            reachable = False
            # A failed check stays on the admin page until a later check passes;
            # while it is there, the routes are checked again every 10 minutes.
            recheck = bool(self.route_problems) and now - self._last_probe >= 600
            probed = probe and (changed or recheck or self._probe_now or bool(widened))
            if probed:
                self._probe_now = False
                self._last_probe = now
                checked, reachable = pomerium.probe(
                    self.settings, expectations, timeout=self.settings.probe_timeout_seconds
                )
                problems += checked
            if sync_error or (probed and reachable):
                self.route_problems = problems
            if probed and reachable and not problems:
                for host in touched:  # checked now: no longer held
                    self._held.pop(host, None)
                self._save_checked(actor, config)
            elif not widened:
                self._save_checked(actor, config)  # nothing new admitted: safe as written
            if problems and reachable:
                if widened:
                    self._narrow_back(actor, config, reference, widened)
                    self.db.audit(actor, "routes.check_failed",
                                  {"problems": problems[:20], "held_back": sorted(widened)[:20]})
                    if requested:
                        raise RouteError("; ".join(problems))  # the caller undoes its change
                    message = "routes out of sync with the database: " + "; ".join(problems)
                    self._hold(widened, now + 600, message)
                    logger.error("%s", message)
                elif requested:
                    self.db.audit(actor, "routes.check_failed", {"problems": problems[:20]})
                    raise RouteError("; ".join(problems))
                else:
                    self.db.audit(actor, "routes.check_failed", {"problems": problems[:20]})
                    logger.error("routing check failed (config kept, access was not widened): %s", problems)
            elif widened and probed and not reachable:
                # Pomerium cannot check it yet: keep the old access for these hosts.
                self._narrow_back(actor, config, reference, widened)
                self._hold(widened, now, "waiting for a routing check (Pomerium not reachable)")
                logger.warning("Pomerium not reachable; %s keep their previous access", sorted(widened))
            elif problems and not sync_error:
                logger.warning("Pomerium not reachable yet; config written: %s", problems)
            held_messages = sorted({message for _, message in self._held.values()})
            for message in held_messages:
                if message not in self.route_problems:
                    self.route_problems = [message, *self.route_problems]
            return {"changed": changed, "problems": problems}

    def _hold(self, hosts: set[str], retry_at: float, message: str) -> None:
        for host in hosts:
            self._held[host] = (retry_at, message)

    def _narrow_back(self, actor: str, config: dict[str, Any], reference: dict[str, Any] | None,
                     hosts: set[str]) -> None:
        restore = pomerium.without_widening(config, reference, self.settings.owner_email, hosts=hosts)
        self._pending_restore = restore  # retried first if this write fails
        self._write_routes(actor, restore)
        self._pending_restore = None
        self._save_checked(actor, restore)

    def _checked_path(self) -> Path:
        path = self.settings.pomerium_config
        return path.with_name(f".{path.name}.checked")

    def _save_checked(self, actor: str, config: dict[str, Any], required: bool = False) -> None:
        """Record the last config known safe. Before a wider config is written
        this must succeed (``required``); afterwards a failure only means later
        calls check again."""
        try:
            pomerium.write_config(self._checked_path(), config)
        except Exception as exc:
            if required:
                message = f"writing the routes failed: {exc}"
                self.route_problems = [message]
                self._write_failed = True
                raise RouteError(message) from exc
            logger.warning("could not record the checked routes: %s", exc)

    def _write_routes(self, actor: str, config: dict[str, Any]) -> bool:
        """Write Pomerium's config. A failure (for example a full disk) is shown on
        the admin page until a later write succeeds, and raised as RouteError:
        the database change that asked for it is kept unless it widened access
        (see ``_widening``); the reconcile loop tries again every minute."""
        try:
            changed = pomerium.write_config(self.settings.pomerium_config, config)
        except Exception as exc:
            message = f"writing the routes failed: {exc}"
            self.route_problems = [message]
            self._write_failed = True
            try:
                self.db.audit(actor, "routes.write_failed", {"reason": str(exc)[:1000]})
            except Exception:  # the same full disk
                logger.exception("could not record the failed route write")
            raise RouteError(message) from exc
        if self._write_failed:
            # The failure stays shown until a check of what is now written passes
            # (in this call, or the next one that may probe).
            self._write_failed = False
            self._probe_now = True
        return changed

    def _widening(self, actor: str, change: Any) -> None:
        """Run a change that may let more people in; undo it if anything fails
        and it did let more people in. A change that only narrows access stays
        in the database even when writing the routes fails (the reconcile loop
        writes them later)."""
        with self.lock:
            snapshot = self.db.snapshot_access()
            baseline, _ = pomerium.render(self.settings, self.db)
            widens = True  # until known otherwise
            try:
                change()
                rendered, _ = pomerium.render(self.settings, self.db)
                widens = pomerium.is_widening(baseline, rendered, self.settings.owner_email)
                self.apply_routes(actor, baseline=baseline)
            except Exception as exc:
                plain = isinstance(exc, (RouteError, PlatformError))
                if not widens:
                    if plain:
                        raise  # a failed write already shows on the admin page
                    message = f"the change was saved, but the routes were not updated: {exc}"
                    self.route_problems = [message]
                    raise RouteError(message) from exc
                self.db.restore_access(snapshot)
                try:
                    self.apply_routes(actor, probe=False)
                except Exception:
                    logger.exception("could not rewrite the routes after undoing a change")
                self.db.audit(actor, "routes.undone", {"reason": str(exc)[:1000]})
                error = exc if plain else RouteError(f"writing the routes failed: {exc}")
                if isinstance(error, RouteError):
                    error.undone = True
                if error is exc:
                    raise
                raise error from exc

    def repair_versions(self) -> None:
        """At startup: point apps away from versions whose files are gone (restores)."""
        with self.lock:
            for app in self.db.apps():
                if app["kind"] != "static":
                    continue
                name = app["name"]
                exists = {v: bool(v) and self.sites.version_dir(name, v).is_dir()
                          for v in (app["live_version"], app["pending_version"], app["previous_version"])}
                live, pending, previous = app["live_version"], app["pending_version"], app["previous_version"]
                if live and not exists[live]:
                    live, previous = (previous, None) if previous and exists[previous] else (None, None)
                if pending and not exists[pending]:
                    pending = None
                if previous and not exists.get(previous, False):
                    previous = None
                if (live, pending, previous) == (app["live_version"], app["pending_version"], app["previous_version"]):
                    continue
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE apps SET live_version = ?, pending_version = ?, previous_version = ?, "
                        "updated_at = ? WHERE name = ?", (live, pending, previous, time.time(), name))
                    if pending is None and app["pending_version"]:
                        conn.execute(
                            "UPDATE requests SET status = 'superseded', decided_at = ? "
                            "WHERE app = ? AND kind = 'release' AND status = 'pending'", (time.time(), name))
                self.db.audit("system", "app.repair", {
                    "app": name, "before": [app["live_version"], app["pending_version"], app["previous_version"]],
                    "after": [live, pending, previous]})
                logger.warning("%s: missing site files; versions now live=%s pending=%s", name, live, pending)

    # ------------------------------------------------------------------ deploy
    def deploy_version(self, name: str, archive: Path, note: str = "", actor: str = "ai",
                       data_change: str = "") -> dict[str, Any]:
        if data_change and data_change not in DATA_CHANGES:
            raise PlatformError("data_change must be none, compatible or irreversible")
        problem = name_problem(name, self._reserved())
        if problem:
            raise PlatformError(problem)
        quotas = self.db.get_setting("quotas")
        with self.lock:
            existing = self.db.app(name)
            if existing and existing["kind"] != "static":
                raise PlatformError("This name belongs to a runtime app.")
            if existing is None:
                apps = self.db.apps()
                if len(apps) >= int(quotas["max_apps"]):
                    raise PlatformError(f"the app limit ({quotas['max_apps']}) is reached")
                if sum(1 for a in apps if a["stage"] == "dev") >= int(quotas["max_dev_apps"]):
                    raise PlatformError(f"the Dev app limit ({quotas['max_dev_apps']}) is reached")
            version, result = self.sites.store_version(
                name, archive, max_total_bytes=int(quotas["max_site_mb"]) * 1024 * 1024
            )
            now = time.time()
            outcome = "live"
            with self.db.tx() as conn:
                if existing is None:
                    conn.execute(
                        "INSERT INTO apps (name, kind, stage, live_version, created_at, updated_at) "
                        "VALUES (?, 'static', 'dev', ?, ?, ?)",
                        (name, version, now, now),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO versions (app, version_id, created_at, files, bytes, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, version, now, result.files, result.total_bytes, note[:500]),
                )
                if existing is None:
                    pass
                elif version == existing["live_version"]:
                    outcome = "unchanged"
                elif self.ai_permissions(existing)['deploy']:
                    conn.execute(
                        "UPDATE apps SET previous_version = live_version, live_version = ?, "
                        "pending_version = NULL, updated_at = ? WHERE name = ?",
                        (version, now, name),
                    )
                    conn.execute(
                        "UPDATE requests SET status = 'superseded', decided_at = ? "
                        "WHERE app = ? AND kind = 'release' AND status = 'pending'",
                        (now, name),
                    )
                else:
                    outcome = "pending"
                    conn.execute(
                        "UPDATE apps SET pending_version = ?, updated_at = ? WHERE name = ?",
                        (version, now, name),
                    )
                    # The pending version itself is what waits for the owner, on the app page.
                    conn.execute(
                        "UPDATE requests SET status = 'superseded', decided_at = ? "
                        "WHERE app = ? AND kind = 'release' AND status = 'pending'",
                        (now, name),
                    )
            self.record_data_change(name, version, data_change)
            self.db.audit(actor, "app.deploy", {"app": name, "version": version, "outcome": outcome})
            self.apply_routes(actor)
            self._prune(name)
            app = self._app_or_error(name)
        info = self.describe(app)
        info.update({"version": version, "outcome": outcome, "files": result.files, "bytes": result.total_bytes})
        if outcome == "pending":
            info["approval_url"] = self.app_url(name, "versions")
            info["next_step"] = "The owner reviews and releases this version on the app page (approval_url)."
        return info

    def _prune(self, name: str) -> None:
        app = self.db.app(name)
        if not app:
            return
        keep = {v for v in (app["live_version"], app["pending_version"], app["previous_version"]) if v}
        # An upload may replace pending while its build is already running.
        # Preserve that operation's source and image until it has finished.
        for operation in self.db.query("SELECT result FROM operations WHERE app=? AND status IN ('queued','running')", (name,)):
            version = json.loads(operation['result'] or '{}').get('version')
            if version:
                keep.add(version)
        development=self.db.get_setting('development:'+name)
        if development and development.get('version'): keep.add(development['version'])
        # The newest revisions keep their source and manifest (not their image or
        # containers), so an older one can be restored: submitted again and rebuilt.
        count = max(0, int(self.db.get_setting("quotas").get("kept_revisions", 10)))
        if app["kind"] == "runtime":
            # Count restorable revisions, not their intermediate raw uploads.
            retain = {row['version_id'] for row in self.db.query(
                'SELECT v.version_id FROM versions v JOIN runtime_versions r '
                'ON r.app=v.app AND r.version_id=v.version_id WHERE v.app=? '
                'ORDER BY v.created_at DESC,v.version_id LIMIT ?', (name,count))}
        else:
            retain = {row['version_id'] for row in self.db.query(
                'SELECT version_id FROM versions WHERE app=? ORDER BY created_at DESC,version_id LIMIT ?', (name,count))}
        if app["kind"] == "runtime" and self.runtime:
            self.runtime.prune(name, keep, retain)
        removed = self.sites.prune(name, keep | retain)
        if removed:
            with self.db.tx() as conn:
                conn.executemany(
                    "DELETE FROM versions WHERE app = ? AND version_id = ?",
                    [(name, version) for version in removed],
                )
                conn.executemany("DELETE FROM settings WHERE key = ?",
                                 [(f"{prefix}:{name}:{version}",) for version in removed
                                  for prefix in ("data-change", "build-network")])

    def restore_revision(self, name: str, version: str, actor: str) -> dict[str, Any]:
        """Go back to a kept revision. Code only: app data is never rolled back.

        The previous revision still has its image, so that is a swap. An older
        one is made pending again, rebuilt when it has a Dockerfile, and released
        like any new version."""
        with self.lock:
            app = self._app_or_error(name)
            if version == app["live_version"]:
                raise PlatformError("that revision is live already")
            written_by = app["live_version"]
            data = f"App data was not changed; it was last written by revision {written_by}."
            if version == app["previous_version"]:
                self.rollback(name, actor, expected_version=version)
                return {"outcome": "rolled back", "version": version, "rebuilt": False,
                        "what_happened": f"The code went back to revision {version}. Its image was kept, so nothing "
                                         f"was rebuilt. {data}"}
            if version not in {row["version_id"] for row in self.db.versions(name)} or \
                    not self.sites.version_dir(name, version).is_dir():
                raise PlatformError("that revision is no longer kept")
            if app["kind"] == "runtime":
                try:
                    result = self.runtime.restore(name, version, actor)
                except PlatformError:
                    raise
                except ValueError as exc:
                    raise PlatformError(str(exc)) from exc
                return {**result, "data": data}
            with self.db.tx() as conn:
                conn.execute("UPDATE apps SET pending_version = ?, updated_at = ? WHERE name = ?",
                             (version, time.time(), name))
            self.db.audit(actor, "app.restore", {"app": name, "version": version})
            self.release(name, actor, expected_version=version)
            return {"outcome": "released", "version": version, "rebuilt": False,
                    "what_happened": f"Revision {version} is live again. {data}"}

    # ------------------------------------------------------------------ owner actions
    def release(self, name: str, actor: str, expected_version: str | None = None) -> dict[str, Any]:
        """Make the pending version live. The owner's page passes the version they
        reviewed, so a newer upload arriving in between is never released unseen."""
        with self.lock:
            app = self._app_or_error(name)
            if not app["pending_version"]:
                raise PlatformError("there is no pending version")
            if expected_version is not None and expected_version != app["pending_version"]:
                raise PlatformError("a newer version arrived after you looked; review it before releasing")
            if not self.sites.version_dir(name, app["pending_version"]).is_dir():
                raise PlatformError("the pending version's files are missing; ask for a new upload")
            if app["kind"] == "runtime":
                if not self.runtime:
                    raise PlatformError("Runtime controller is unavailable.")
                return self.runtime.enqueue(name, app["pending_version"])
            now = time.time()
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE apps SET previous_version = live_version, live_version = pending_version, "
                    "pending_version = NULL, updated_at = ? WHERE name = ?",
                    (now, name),
                )
                conn.execute(
                    "UPDATE requests SET status = 'approved', decided_at = ?, decided_by = ? "
                    "WHERE app = ? AND kind = 'release' AND status = 'pending'",
                    (now, actor, name),
                )
            self.db.audit(actor, "app.release", {"app": name, "version": app["pending_version"]})
            self.apply_routes(actor)
            self._prune(name)
            return self.describe(self._app_or_error(name))

    def rollback(self, name: str, actor: str, expected_version: str | None = None) -> dict[str, Any]:
        with self.lock:
            app = self._app_or_error(name)
            if not app["previous_version"]:
                raise PlatformError("there is no previous version")
            if expected_version is not None and expected_version != app["previous_version"]:
                raise PlatformError("the versions changed after you looked; reload the page")
            if not self.sites.version_dir(name, app["previous_version"]).is_dir():
                raise PlatformError("the previous version's files are missing")
            if app["kind"] == "runtime":
                self.runtime.refuse_during_release(name)
            if app["kind"] == "runtime" and app["online"]:
                with self.runtime.exclusive(name):
                    self.runtime.stop(name, app["live_version"])
                    try:
                        self.runtime.resume(name, app["previous_version"])
                    except Exception:
                        self.runtime.stop(name, app["previous_version"])
                        self.runtime.resume(name, app["live_version"])
                        raise
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE apps SET live_version = previous_version, previous_version = live_version, "
                    "updated_at = ? WHERE name = ?",
                    (time.time(), name),
                )
            self.db.audit(actor, "app.rollback", {"app": name, "to": app["previous_version"]})
            self.apply_routes(actor)
            return self.describe(self._app_or_error(name))

    def discard_pending(self, name: str, actor: str, expected_version: str | None = None) -> dict[str, Any]:
        with self.lock:
            app = self._app_or_error(name)
            if expected_version is not None and expected_version != app["pending_version"]:
                raise PlatformError("The pending version changed; reload and review it.")
            if app["kind"] == "runtime" and app["pending_version"]:
                self.runtime.refuse_during_release(name)
                self.runtime.stop(name, app["pending_version"])
            now = time.time()
            with self.db.tx() as conn:
                conn.execute("UPDATE apps SET pending_version = NULL, updated_at = ? WHERE name = ?", (now, name))
                conn.execute(
                    "UPDATE requests SET status = 'rejected', decided_at = ?, decided_by = ? "
                    "WHERE app = ? AND kind = 'release' AND status = 'pending'",
                    (now, actor, name),
                )
            self.db.audit(actor, "app.discard_pending", {"app": name, "version": app["pending_version"]})
            self.apply_routes(actor)
            self._prune(name)
            return self.describe(self._app_or_error(name))

    def _validate_audience(self, people: list[str], groups: list[str]) -> tuple[list[str], list[str]]:
        people = sorted({p.strip().lower() for p in people if p.strip()})
        groups = sorted({g.strip().lower() for g in groups if g.strip()})
        for email in people:
            if not self.db.person_exists(email):
                raise PlatformError(f"{email} is not in the people list; add them on the admin page first")
        for group in groups:
            if not self.db.group_exists(group):
                raise PlatformError(f"group '{group}' does not exist")
        return people, groups

    def set_stage(self, name: str, stage: str, people: list[str], groups: list[str], actor: str,
                  expected_live_version: str | None = None) -> dict[str, Any]:
        """Change who can see the app. ``expected_live_version`` is the live version
        the owner saw ("" for none); a different live version is refused, so a
        newer upload is never shared or published unseen."""
        if stage not in STAGES:
            raise PlatformError(f"stage must be one of {', '.join(STAGES)}")
        people, groups = self._validate_audience(people, groups)
        with self.lock:
            app = self._app_or_error(name)
            if expected_live_version is not None and expected_live_version != (app["live_version"] or ""):
                raise PlatformError("the live version changed after you looked; review it and try again")

            def change() -> None:
                now = time.time()
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE apps SET stage = ?, updated_at = ?, "
                        "stage_since = CASE WHEN stage = ? THEN stage_since ELSE ? END, "
                        "auto_release = CASE WHEN ? = 'public' THEN 0 ELSE auto_release END, "
                        "protected = CASE WHEN '" + stage + "' != 'dev' THEN 1 ELSE protected END WHERE name = ?",
                        (stage, now, stage, now, stage, name))
                    conn.execute("DELETE FROM app_access WHERE app = ?", (name,))
                    if stage == "shared":
                        conn.executemany(
                            "INSERT INTO app_access (app, principal_type, principal) VALUES (?, 'person', ?)",
                            [(name, email) for email in people],
                        )
                        conn.executemany(
                            "INSERT INTO app_access (app, principal_type, principal) VALUES (?, 'group', ?)",
                            [(name, group) for group in groups],
                        )
                self.db.audit(actor, "app.stage", {"app": name, "from": app["stage"], "stage": stage,
                                                   "people": people, "groups": groups})

            if app["stage"] == "shared" and stage == "shared":
                # Removing people must stick even if adding others fails the check:
                # first narrow to the people who stay, then widen to the new list.
                keep_people = [p for p in app["people"] if p in people]
                keep_groups = [g for g in app["groups"] if g in groups]
                if (keep_people, keep_groups) != (app["people"], app["groups"]):
                    with self.db.tx() as conn:
                        conn.execute("DELETE FROM app_access WHERE app = ?", (name,))
                        conn.executemany(
                            "INSERT INTO app_access (app, principal_type, principal) VALUES (?, 'person', ?)",
                            [(name, email) for email in keep_people])
                        conn.executemany(
                            "INSERT INTO app_access (app, principal_type, principal) VALUES (?, 'group', ?)",
                            [(name, group) for group in keep_groups])
                    self.db.audit(actor, "app.audience_narrowed", {"app": name, "people": keep_people,
                                                                    "groups": keep_groups})
                    try:
                        self.apply_routes(actor)
                    except RouteError:
                        # The removal is kept (and shown); the rest of the change below
                        # checks itself, and is undone if its own write fails too.
                        logger.warning("routes after removing people from %s are not written yet", name)

            self._widening(actor, change)
            return self.describe(self._app_or_error(name))

    def set_online(self, name: str, online: bool, actor: str) -> dict[str, Any]:
        with self.lock:
            current = self._app_or_error(name)
            if online and actor != 'data-export completion' and self.db.query_one(
                "SELECT id FROM operations WHERE app=? AND kind='data-export' AND status IN ('queued','running')", (name,)):
                raise PlatformError('A consistent data export is running. Wait before bringing this app online.')
            if current["kind"] == "runtime":
                if not online:
                    self.runtime.stop(name)
                    self.db.set_setting('runtime_preview:'+name, None)

            def change() -> None:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE apps SET online = ?, updated_at = ? WHERE name = ?",
                        (1 if online else 0, time.time(), name),
                    )
                self.db.audit(actor, "app.online" if online else "app.offline", {"app": name})

            try:
                if current['kind']=='runtime' and online and current['live_version']:
                    self.runtime.refuse_during_release(name)
                    with self.runtime.exclusive(name):
                        self.runtime.start(name, current['live_version'])
                self._widening(actor, change)
            except Exception:
                # Starting workers is an effect even when the rendered audience
                # does not widen (for example while a route is held).
                if current['kind']=='runtime' and online and not current['online']:
                    with self.db.tx() as conn:
                        conn.execute('UPDATE apps SET online=0, updated_at=? WHERE name=?',(time.time(),name))
                raise
            finally:
                if current['kind']=='runtime' and not self._app_or_error(name)['online']:
                    self.runtime.stop(name)
            return self.describe(self._app_or_error(name))

    def ai_start(self, name: str) -> dict[str, Any]:
        """The AI may bring back a Dev app; for any other stage it can only ask."""
        with self.lock:
            app = self._app_or_error(name)
            if self.ai_permissions(app)['restart']:
                return self.set_online(name, True, "ai")
            request = self.file_request(name, "start", {}, "AI asked to bring the app back online")
            return {"status": "approval needed", "request": request}

    def ai_stop(self, name: str) -> dict[str, Any]:
        """The AI may take an app offline with the owner's start/stop permission
        (on by default for unprotected apps only the owner can open)."""
        with self.lock:
            app = self._app_or_error(name)
            if self.ai_permissions(app)['restart']:
                return self.set_online(name, False, "ai")
            raise PlatformError("The owner has not allowed the AI to start or stop this app. The owner can take it "
                                f"offline on {self.app_url(name, 'versions')}.")

    def ai_remove(self, name: str) -> dict[str, Any]:
        """The AI may delete an app only with the owner's delete permission."""
        with self.lock:
            app = self._app_or_error(name)
            if self.ai_permissions(app)['delete']:
                self.remove_app(name, "ai")
                return {"removed": name}
            raise PlatformError("The owner has not allowed the AI to delete this app. The owner can delete it "
                                f"at the bottom of {self.app_url(name)}.")

    def update_app_settings(self, name: str, actor: str, *, log_access: str | None = None,
                            auto_release: bool | None = None, protected: bool | None = None) -> dict[str, Any]:
        with self.lock:
            app = self._app_or_error(name)
            updates, params = [], []
            if protected is not None:
                if not protected and app["stage"] != "dev":
                    raise PlatformError("Only an owner-only app may be made development.")
                updates.append("protected = ?")
                params.append(int(protected))
            if log_access is not None:
                if log_access not in ("default", "off", "stats", "masked", "full"):
                    raise PlatformError("log_access must be default, off, stats, masked or full")
                updates += ["log_access = ?", "log_access_until = ?"]
                params += [log_access, time.time() + 86_400 if log_access == "full" else None]
            if auto_release is not None:
                if auto_release and (app["kind"] != "static" or app["stage"] == "public"):
                    raise PlatformError("auto-release is only allowed for static apps that are not Public")
                updates.append("auto_release = ?")
                params.append(1 if auto_release else 0)
            if updates:
                with self.db.tx() as conn:
                    conn.execute(
                        f"UPDATE apps SET {', '.join(updates)}, updated_at = ? WHERE name = ?",
                        (*params, time.time(), name),
                    )
                self.db.audit(actor, "app.settings", {"app": name, "log_access": log_access, "auto_release": auto_release})
            return self.describe(self._app_or_error(name))

    def remove_app(self, name: str, actor: str) -> None:
        with self.lock:
            current = self._app_or_error(name)
            if current["kind"] == "runtime":
                self.runtime.refuse_during_release(name)
                with self.runtime.exclusive(name):
                    self.runtime.remove(name)
            with self.db.tx() as conn:
                conn.execute("DELETE FROM apps WHERE name = ?", (name,))
                for key in ('ai-permissions:','capabilities:','development:','development-inventory:','runtime-resources:'):
                    conn.execute('DELETE FROM settings WHERE key=?',(key+name,))
                conn.execute("DELETE FROM secrets WHERE app = ?", (name,))
                conn.execute(
                    "UPDATE requests SET status = 'superseded', decided_at = ? WHERE app = ? AND status = 'pending'",
                    (time.time(), name),
                )
            self.sites.remove_app(name)
            self.db.audit(actor, "app.remove", {"app": name})
            self.apply_routes(actor)

    def kill_switch(self, actor: str) -> int:
        """Emergency owner-only access overlay. Retain production and sharing state."""
        with self.lock:
            self.db.set_setting("access_lockdown", True)
            if self.runtime: self.runtime.hold_service_ports()
            count = sum(a["stage"] != "dev" for a in self.db.apps())
            self.db.audit(actor, "platform.killswitch", {"apps": count})
            self.apply_routes(actor)
            return count

    def restore_visibility(self, actor: str) -> None:
        with self.lock:
            self.db.set_setting("access_lockdown", False)
            try:
                self.apply_routes(actor)
            except Exception:
                self.db.set_setting("access_lockdown", True)
                self.apply_routes(actor)
                raise
            self.db.audit(actor, "platform.visibility.restore")

    # ------------------------------------------------------------------ people
    def add_person(self, email: str, display_name: str, actor: str) -> None:
        email = email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise PlatformError("that is not a valid email address")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO people (email, display_name, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET display_name = excluded.display_name",
                (email, display_name.strip()[:100], time.time()),
            )
        self.db.audit(actor, "people.add", {"email": email})

    def remove_person(self, email: str, actor: str) -> None:
        email = email.strip().lower()
        with self.lock:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM people WHERE email = ?", (email,))
                conn.execute("DELETE FROM app_access WHERE principal_type = 'person' AND principal = ?", (email,))
            self.db.audit(actor, "people.remove", {"email": email})
            self.apply_routes(actor)

    def add_group(self, name: str, actor: str) -> None:
        name = name.strip().lower()
        if not _GROUP_RE.match(name):
            raise PlatformError("group names are lowercase letters, digits, '-' and '_'")
        with self.db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO groups (name, created_at) VALUES (?, ?)", (name, time.time()))
        self.db.audit(actor, "groups.add", {"group": name})

    def remove_group(self, name: str, actor: str) -> None:
        with self.lock:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM groups WHERE name = ?", (name,))
                conn.execute("DELETE FROM app_access WHERE principal_type = 'group' AND principal = ?", (name,))
            self.db.audit(actor, "groups.remove", {"group": name})
            self.apply_routes(actor)

    def set_group_member(self, group: str, email: str, member: bool, actor: str) -> None:
        email = email.strip().lower()
        with self.lock:
            if not self.db.group_exists(group) or not self.db.person_exists(email):
                raise PlatformError("unknown group or person")

            def change() -> None:
                with self.db.tx() as conn:
                    if member:
                        conn.execute(
                            "INSERT OR IGNORE INTO group_members (group_name, email) VALUES (?, ?)", (group, email)
                        )
                    else:
                        conn.execute("DELETE FROM group_members WHERE group_name = ? AND email = ?", (group, email))
                self.db.audit(actor, "groups.member", {"group": group, "email": email, "member": member})

            self._widening(actor, change)

    # ------------------------------------------------------------------ secrets
    def secret_names(self, app: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT name, bound, updated_at, ciphertext IS NOT NULL AS has_value FROM secrets "
            "WHERE app = ? ORDER BY name",
            (app,),
        )
        return [
            {"name": r["name"], "bound": bool(r["bound"]), "has_value": bool(r["has_value"]), "updated_at": r["updated_at"]}
            for r in rows
        ]

    def set_secret(self, app: str, name: str, value: str, actor: str) -> None:
        if self._fernet is None:
            raise PlatformError("DEPLOYER_SECRETS_KEY is not configured")
        if not _SECRET_NAME_RE.match(name):
            raise PlatformError("secret names are UPPER_CASE letters, digits and '_'")
        if not value:
            raise PlatformError("the value is empty")
        self._app_or_error(app)
        token = self._fernet.encrypt(value.encode())
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO secrets (app, name, ciphertext, bound, updated_at) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(app, name) DO UPDATE SET ciphertext = excluded.ciphertext, bound = 1, "
                "updated_at = excluded.updated_at",
                (app, name, token, time.time()),
            )
        self.db.audit(actor, "secret.set", {"app": app, "name": name})

    def delete_secret(self, app: str, name: str, actor: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM secrets WHERE app = ? AND name = ?", (app, name))
        self.db.audit(actor, "secret.delete", {"app": app, "name": name})

    def secret_value(self, app: str, name: str) -> str | None:
        """Only used when starting an app container (Phase 2). Never exposed by an API."""
        if self._fernet is None:
            return None
        row = self.db.query_one("SELECT ciphertext FROM secrets WHERE app = ? AND name = ?", (app, name))
        if row is None or row["ciphertext"] is None:
            return None
        try:
            return self._fernet.decrypt(row["ciphertext"]).decode()
        except InvalidToken:
            return None

    # ------------------------------------------------------------------ requests
    def revision(self, app: str) -> str:
        import hashlib
        state = self._app_or_error(app)
        fields = {k: state[k] for k in ("live_version", "pending_version", "stage", "online", "protected", "people", "groups", "auto_release")}
        fields.update(environment=state['environment'], permissions=self.ai_permissions(state),
                      capabilities=self.db.get_setting('capabilities:'+app), development=self.db.get_setting('development:'+app))
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def file_request(self, app: str | None, kind: str, payload: dict[str, Any], note: str) -> dict[str, Any]:
        if kind not in REQUEST_KINDS:
            raise PlatformError(f"request kind must be one of {', '.join(REQUEST_KINDS)}")
        if app is not None:
            self._app_or_error(app)
        if app is None:
            raise PlatformError("a request needs an app")
        # Only the validated fields are stored: the admin page shows what approving
        # does from these, never from anything else the AI sent.
        if kind in ('capabilities', 'development', 'data_export'):
            if not self.runtime:
                raise PlatformError('Runtime controller is unavailable.')
            payload = self.runtime.validate_request(app, kind, payload)
        elif kind == "stage":
            if payload.get("stage") not in STAGES:
                raise PlatformError(f"payload.stage must be one of {', '.join(STAGES)}")
            people = payload.get("people") or []
            groups = payload.get("groups") or []
            if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
                raise PlatformError("payload.people must be a list of email addresses")
            if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                raise PlatformError("payload.groups must be a list of group names")
            people, groups = self._validate_audience(people, groups)
            if payload["stage"] != "shared":
                people, groups = [], []
            # The version that is live now: approving publishes exactly this one.
            live = self.db.app(app)["live_version"]  # type: ignore[index]
            payload = {"stage": payload["stage"], "people": people, "groups": groups, "live_version": live}
        elif kind == "secret":
            if not isinstance(payload.get("name"), str) or not _SECRET_NAME_RE.match(payload["name"]):
                raise PlatformError("payload.name must be an UPPER_CASE secret name")
            payload = {"name": payload["name"]}
        elif kind == "log_access":
            if payload.get("mode") not in ("full", "masked", "stats", "off"):
                raise PlatformError("payload.mode must be full, masked, stats or off")
            payload = {"mode": payload["mode"]}
        elif kind == "rollback":
            wanted = payload.get("version")
            current = self.db.app(app) or {}
            if wanted == "previous":
                wanted = current.get("previous_version")
            known = {item["version"]: item for item in self.revisions(app)}
            if not isinstance(wanted, str) or wanted not in known or wanted in (current.get("live_version"), current.get("pending_version")):
                raise PlatformError("payload.version must be a kept revision that is not live or pending "
                                    "(see revisions in app_status), or \"previous\"")
            if known[wanted]["going_back"] in ("not possible", "upload it again as a new version"):
                raise PlatformError(f"revision {wanted} cannot be restored: its image is {known[wanted]['image']}")
            payload = {"version": wanted, "role": known[wanted]["role"], "image": known[wanted]["image"],
                       "going_back": known[wanted]["going_back"]}
        else:
            payload = {}
        with self.lock:
            payload["revision"] = self.revision(app)
            pending = self.db.requests(status="pending")
            if len(pending) >= 10:
                raise PlatformError("there are already 10 pending requests; wait for the owner")
            with self.db.tx() as conn:
                cursor = conn.execute(
                    "INSERT INTO requests (app, kind, payload, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (app, kind, json.dumps(payload), note[:2000], time.time()),
                )
                request_id = cursor.lastrowid
        self.db.audit("ai", "request.file", {"id": request_id, "app": app, "kind": kind})
        return {"id": request_id, "status": "pending", "approval_url": self.app_url(app, f"ask-{request_id}"),
                "next_step": "The owner applies or dismisses this on the app page (approval_url)."}

    def decide_request(self, request_id: int, approve: bool, actor: str, value: str | None = None) -> dict[str, Any]:
        with self.lock:  # one decision at a time
            request = self.db.request(request_id)
            if request is None or request["status"] != "pending":
                raise PlatformError("no such pending request")
            if approve:
                app, payload = request["app"], request["payload"]
                kind = request["kind"]
                if kind == "stage" and "live_version" not in payload:
                    # Filed by an older build that did not record the live version.
                    with self.db.tx() as conn:
                        conn.execute("UPDATE requests SET status = 'superseded', decided_at = ?, decided_by = ? "
                                     "WHERE id = ?", (time.time(), actor, request_id))
                    raise PlatformError(
                        "this request does not say which version it is for, so it was cancelled; "
                        "look at the current version and set the stage on the app's page, or ask the AI "
                        "to request again")
                if kind != "release" and payload.get("revision") != self.revision(app):
                    with self.db.tx() as conn:
                        conn.execute("UPDATE requests SET status='superseded',decided_at=?,decided_by=? WHERE id=?",
                                     (time.time(), actor, request_id))
                    raise PlatformError("The app changed after this request. Review it and request again.")
                try:
                    if kind in ('capabilities', 'development', 'data_export'):
                        self.runtime.approve_request(app, kind, payload, actor)
                    elif kind == "stage":
                        current = self._app_or_error(app)
                        if "live_version" in payload and current["live_version"] != payload["live_version"]:
                            with self.db.tx() as conn:
                                conn.execute("UPDATE requests SET status = 'superseded', decided_at = ?, decided_by = ? "
                                             "WHERE id = ?", (time.time(), actor, request_id))
                            raise PlatformError(
                                "the app was changed after this request was filed, so it was cancelled; "
                                "look at the current version and ask the AI to request again")
                        self.set_stage(app, payload["stage"], payload.get("people", []), payload.get("groups", []), actor,
                                       expected_live_version=payload["live_version"] or "")
                    elif kind == "release":
                        # Approving releases exactly the version the request was filed for.
                        if not payload.get("version"):
                            # Filed by an older build that did not record the version.
                            with self.db.tx() as conn:
                                conn.execute("UPDATE requests SET status = 'superseded', decided_at = ?, decided_by = ? "
                                             "WHERE id = ?", (time.time(), actor, request_id))
                            raise PlatformError(
                                "this request does not say which version it is for, so it was cancelled; "
                                "review the pending version on the app's page and release it there")
                        self.release(app, actor, expected_version=str(payload["version"]))
                    elif kind == "start":
                        self.set_online(app, True, actor)
                    elif kind == "stop":
                        self.set_online(app, False, actor)
                    elif kind == "remove":
                        self.remove_app(app, actor)
                    elif kind == "log_access":
                        self.update_app_settings(app, actor, log_access=payload["mode"])
                    elif kind == "rollback":
                        self.restore_revision(app, str(payload["version"]), actor)
                    elif kind == "secret":
                        if not value:
                            raise PlatformError("enter the secret value to approve this request")
                        self.set_secret(app, payload["name"], value, actor)
                except RouteError as exc:
                    if not exc.undone:
                        # The change was made (only the routes are behind): the
                        # request is decided, so it cannot be rejected later.
                        with self.db.tx() as conn:
                            conn.execute("UPDATE requests SET status = 'approved', decided_at = ?, decided_by = ? "
                                         "WHERE id = ?", (time.time(), actor, request_id))
                        self.db.audit(actor, "request.approve", {"id": request_id, "routes": str(exc)[:500]})
                    raise
            # The action itself may already have closed this request (release marks it
            # approved, remove supersedes the app's open requests); record the decision.
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                    ("approved" if approve else "rejected", time.time(), actor, request_id),
                )
            self.db.audit(actor, "request.approve" if approve else "request.reject", {"id": request_id})
            return self.db.request(request_id) or {}
