"""Render Pomerium's config from the deployer state, write it atomically, probe it.

The config is written as JSON, which is valid YAML, so no YAML library is needed.
Pomerium watches the file and hot-reloads routes. After every write the
deployer probes each host through Pomerium (no cookie, no redirects followed)
and restores the previous file when Pomerium answers wrongly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import DeployerSettings
from .db import Database

logger = logging.getLogger("hertogs.deployer.pomerium")

# Headers a client could send to impersonate someone towards an app. Pomerium
# renames a forged X-Pomerium-Jwt-Assertion itself, but passes these through.
SPOOFABLE_IDENTITY_HEADERS = [
    "X-Pomerium-Claim-Email",
    "X-Pomerium-Claim-User",
    "X-Pomerium-Claim-Name",
    "X-Pomerium-Claim-Sub",
    "X-Pomerium-Claim-Groups",
    "X-Forwarded-Email",
    "X-Forwarded-User",
    "X-Auth-Request-Email",
    "X-Auth-Request-User",
    "Remote-User",
    "Remote-Email",
    "X-Hertogs-User",
]

STATIC_MARKER_HEADER = "x-hertogs-static"


@dataclass(frozen=True)
class Expectation:
    host: str
    kind: str  # "private", "public" or "absent"


def _route(settings: DeployerSettings, host: str, *, emails: list[str] | None, upstream: str | None = None) -> dict[str, Any]:
    route: dict[str, Any] = {
        "from": f"https://{host}",
        "to": [upstream or settings.static_url],
        "preserve_host_header": True,
        "remove_request_headers": SPOOFABLE_IDENTITY_HEADERS,
    }
    if upstream:
        route['allow_websockets'] = True
        route["set_response_headers"] = {"X-Hertogs-Runtime": "1"}
    if emails is None:
        route["allow_public_unauthenticated_access"] = True
    else:
        route["policy"] = [{"allow": {"or": [{"email": {"is": email}} for email in emails]}}]
        # Never let Cloudflare (or a browser) cache what sits behind a login.
        route.setdefault("set_response_headers", {}).update({"Cache-Control": "private, no-store"})
    return route


def render(settings: DeployerSettings, db: Database) -> tuple[dict[str, Any], list[Expectation]]:
    owner = [settings.owner_email]
    routes = [_route(settings, settings.canary_host, emails=owner)]
    expectations = [Expectation(settings.canary_host, "private")]

    lockdown = db.get_setting("access_lockdown")
    for app in db.apps():
        if app['kind']=='runtime' and app['live_version'] and lockdown:
            row=db.query_one('SELECT manifest FROM runtime_versions WHERE app=? AND version_id=?',(app['name'],app['live_version']))
            if row and json.loads(row['manifest']).get('capabilities',{}).get('network',{}).get('ingress'):
                app={**app,'online':False}
        if app['kind']=='runtime' and app['live_version'] and (db.get_setting('gaming_state') or {}).get('state','off')!='off':
            row=db.query_one('SELECT manifest FROM runtime_versions WHERE app=? AND version_id=?',(app['name'],app['live_version']))
            if row and json.loads(row['manifest']).get('capabilities',{}).get('gpu'):
                app={**app,'online':False}
        host = settings.host_for(app["name"])
        preview = settings.preview_host_for(app["name"])
        def upstream(version: str | None, is_preview: bool = False) -> str | None:
            if app["kind"] == "static":
                return None
            row = db.query_one("SELECT manifest FROM runtime_versions WHERE app=? AND version_id=?", (app["name"], version))
            if not row:
                return ""
            manifest = json.loads(row["manifest"])
            web = next((c for c in manifest["components"] if c["kind"] == "web"), None)
            if not web:
                return ""
            from ..common.names import runtime_gateway_name
            return f"http://{runtime_gateway_name(app['name'],version,is_preview)}:{web['port']}"
        target = upstream(app["live_version"])
        if app["online"] and app["live_version"] and target != "":
            public = app["stage"] == "public" and not lockdown
            emails = None if public else (db.allowed_emails(app, settings.owner_email) if app["stage"] == "shared" and not lockdown else owner)
            routes.append(_route(settings, host, emails=emails, upstream=target))
            expectations.append(Expectation(host, ("runtime" if target else "public") if public else "private"))
        else:
            expectations.append(Expectation(host, "absent"))
        target = upstream(app["pending_version"], True)
        ready = app["kind"] == "static" or db.get_setting("runtime_preview:"+app["name"]) == app["pending_version"]
        if app["pending_version"] and ready and target != "":
            routes.append(_route(settings, preview, emails=owner, upstream=target))
            expectations.append(Expectation(preview, "private"))
        else:
            expectations.append(Expectation(preview, "absent"))

    config = {
        "address": ":80",
        "insecure_server": True,
        "authenticate_service_url": f"https://{settings.auth_host}",
        "databroker_storage_type": "file",
        "databroker_storage_connection_string": "file:///data/databroker",
        # Pages on an app's host could otherwise read the visitor's signed identity
        # from /.pomerium/jwt. (Pages can still ask visitors for anything.)
        "runtime_flags": {"pomerium_jwt_endpoint": False, "product_analytics": False},
        "routes": routes,
    }
    return config, expectations


PUBLIC = "public"


def access_map(config: dict[str, Any] | None) -> dict[str, Any]:
    """host -> "public" or the frozenset of emails a route admits."""
    result: dict[str, Any] = {}
    for route in (config or {}).get("routes", []):
        host = str(route.get("from", "")).removeprefix("https://")
        if route.get("allow_public_unauthenticated_access"):
            result[host] = PUBLIC
            continue
        emails = set()
        for policy in route.get("policy", []):
            for rule in policy.get("allow", {}).get("or", []):
                email = rule.get("email", {}).get("is")
                if email:
                    emails.add(str(email).lower())
        result[host] = frozenset(emails)
    return result


def is_widening(old: dict[str, Any] | None, new: dict[str, Any], owner: str) -> bool:
    """True when the new config lets in someone the old one did not (the owner aside)."""
    before, after = access_map(old), access_map(new)
    owner = owner.lower()
    for host, admitted in after.items():
        previous = before.get(host, frozenset())
        if admitted == PUBLIC:
            if previous != PUBLIC:
                return True
        elif previous != PUBLIC and admitted - previous - {owner}:
            return True
    return False


def _host(route: dict[str, Any]) -> str:
    return str(route.get("from", "")).removeprefix("https://")


def widened_hosts(old: dict[str, Any] | None, new: dict[str, Any], owner: str) -> set[str]:
    """Hosts whose route in ``new`` lets in someone ``old`` did not (the owner aside)."""
    old_routes = {_host(r): r for r in (old or {}).get("routes", [])}
    return {_host(route) for route in new.get("routes", [])
            if is_widening({"routes": [old_routes[_host(route)]]} if _host(route) in old_routes else None,
                           {"routes": [route]}, owner)}


def changed_hosts(old: dict[str, Any] | None, new: dict[str, Any]) -> set[str]:
    """Hosts whose access differs between the two configs (added, removed or changed)."""
    before, after = access_map(old), access_map(new)
    return {host for host in set(before) | set(after) if before.get(host) != after.get(host)}


def _narrowed(new_route: dict[str, Any], old_route: dict[str, Any] | None, owner: str) -> dict[str, Any] | None:
    """``new_route`` admitting only people ``old_route`` admitted too (and the
    owner); None when the host had no route."""
    if old_route is None:
        return None
    old_access = access_map({"routes": [old_route]})[_host(old_route)]
    new_access = access_map({"routes": [new_route]})[_host(new_route)]
    if new_access == PUBLIC:
        return old_route  # anything is narrower than public
    keep = sorted((new_access & old_access) | {owner.lower()})
    route = {k: v for k, v in new_route.items() if k != "allow_public_unauthenticated_access"}
    route["policy"] = [{"allow": {"or": [{"email": {"is": email}} for email in keep]}}]
    return route


def without_widening(new: dict[str, Any], old: dict[str, Any] | None, owner: str,
                     hosts: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
    """``new``, except that every host (of ``hosts``, or any) whose access would
    widen compared with ``old`` admits only the people both admit (or has no
    route, if it had none). Every narrowing in ``new`` stays."""
    old_routes = {_host(r): r for r in (old or {}).get("routes", [])}
    widened = widened_hosts(old, new, owner)
    routes = []
    for route in new.get("routes", []):
        host = _host(route)
        if host not in widened or (hosts is not None and host not in hosts):
            routes.append(route)
            continue
        narrowed = _narrowed(route, old_routes.get(host), owner)
        if narrowed is not None:
            routes.append(narrowed)
    return {**new, "routes": routes}


def read_config(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_config(path: Path, config: dict[str, Any]) -> bool:
    """Atomically replace the config file. Returns False when nothing changed."""
    text = json.dumps(config, indent=2) + "\n"
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (path.parent / (path.name + ".prev")).write_text(path.read_text())
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def restore_previous(path: Path) -> bool:
    previous = path.parent / (path.name + ".prev")
    if not previous.exists():
        return False
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(previous.read_text())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def check(settings: DeployerSettings, client: httpx.Client, expectation: Expectation,
          timeout: float = 2.0) -> str | None:
    """Return a problem description, or None when the host behaves as expected."""
    try:
        response = client.get(
            f"{settings.pomerium_url}/",
            headers={"Host": expectation.host, "X-Forwarded-Proto": "https"},
            follow_redirects=False,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return f"unreachable: {exc.__class__.__name__}"
    from_static = STATIC_MARKER_HEADER in response.headers
    location = response.headers.get("location", "")
    if expectation.kind == "private":
        if response.status_code in (302, 303, 307) and f"//{settings.auth_host}" in location:
            return None
        return f"expected a login redirect, got {response.status_code}"
    if expectation.kind == "runtime":
        if response.headers.get("x-hertogs-runtime") == "1" and response.status_code < 500:
            return None
        return f"expected the runtime, got {response.status_code}"
    if expectation.kind == "public":
        # 404 is fine (a site without index.html); 403 means the files are unreadable.
        if from_static and (response.status_code < 400 or response.status_code == 404):
            return None
        return f"expected the site, got {response.status_code} (location {location or '-'})"
    if response.status_code == 404 and not from_static:
        return None
    return f"expected 404 for a removed host, got {response.status_code}"


def probe(settings: DeployerSettings, expectations: list[Expectation], timeout: float) -> tuple[list[str], bool]:
    """Poll until every host behaves as expected or the timeout passes.

    Returns (problems, reachable). ``reachable`` is False when Pomerium never
    answered at all (still starting), which is not a reason to roll back.
    """
    # The whole check stays within ``timeout`` (MCP clients give up after 55 s),
    # even when Pomerium accepts connections but never answers.
    deadline = time.monotonic() + timeout
    problems: list[str] = []
    reachable = False
    with httpx.Client() as client:
        while True:
            problems = []
            for expectation in expectations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    problems.append(f"{expectation.host}: not checked (time limit)")
                    continue
                problem = check(settings, client, expectation, timeout=min(2.0, remaining))
                if problem is None:
                    reachable = True
                    continue
                if not problem.startswith("unreachable"):
                    reachable = True
                problems.append(f"{expectation.host}: {problem}")
            if not problems or time.monotonic() >= deadline:
                return problems, reachable
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
