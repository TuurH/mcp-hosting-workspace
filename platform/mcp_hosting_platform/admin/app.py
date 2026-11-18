"""Routes of the admin page."""

from __future__ import annotations

import hashlib
import asyncio
import hmac
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import jinja2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..common.access import AccessVerifier
from ..common.names import looks_like_app_name
from .config import AdminSettings
from .deployer_client import DeployerClient, DeployerError
from .filemanager import FileError, FileManager, OpenedFileResponse
from .security import AdminSecurityMiddleware, csrf_token

logger = logging.getLogger("mcp-hosting.admin")
HERE = Path(__file__).parent

STAGE_LABELS = {"dev": "Only you", "shared": "Shared (chosen people)", "public": "Public (anyone)"}


QUOTA_LABELS = {
    "max_dev_apps":"Maximum owner-only apps", "max_apps":"Maximum total apps",
    "max_site_mb":"Maximum unpacked source (MiB)", "max_upload_mb":"Maximum compressed upload (MiB)",
    "app_memory_mb":"Default component memory (MiB)", "app_cpus":"Default component CPU cores",
    "app_data_gb":"Default app storage budget (GiB)",
    "max_app_memory_mb":"Maximum requested component memory (MiB)",
    "max_app_cpus":"Maximum requested component CPU cores",
    "max_build_image_mb":"Maximum built image archive (MiB)",
    "max_data_export_mb":"Maximum app-data export (MiB)",
    "max_build_minutes":"Image build time limit (minutes)",
    "build_cache_app_gb":"Build cache per app, trimmed after each build (GiB)",
    "build_cache_total_gb":"All build caches together (GiB)",
    "build_min_free_gb":"Free space a build needs on the Docker disk (GiB)",
    "kept_revisions":"Newest revisions per app that keep their source"}

# Where on the app page each kind of ask is shown and decided.
ASK_SECTIONS = {"stage": "access", "start": "versions", "stop": "versions", "remove": "versions", "release": "versions",
                "rollback": "versions", "log_access": "log-access", "secret": "credentials",
                "capabilities": "capabilities", "development": "development", "data_export": "exports"}
OPERATION_LABELS = {"release": "Release", "preview": "Preview", "image-build": "Image build",
                    "development-command": "Development command", "development-snapshot": "Release snapshot",
                    "data-export": "Data export"}

def release_checklist(app: dict[str, Any]) -> list[dict[str, Any]]:
    """Explain known release prerequisites; authorization still runs in the deployer."""
    version = app.get('pending_version')
    manifest = app.get('manifests', {}).get(version)
    if app.get('kind') != 'runtime' or not manifest:
        return []
    from ..deployer.capabilities import within
    checks = []
    if manifest.get('dockerfile'):
        built = app.get('builds', {}).get(version, {}).get('built')
        checks.append(dict(label='Dockerfile image built', done=bool(built), target='image-builds'))
    checks.append(dict(label='Requested network and GPU access granted',
                       done=within(manifest.get('capabilities'), app.get('capabilities')), target='capabilities'))
    names = set(manifest.get('secrets', []))
    if names:
        values = {s['name'] for s in app.get('secrets', []) if s.get('has_value')}
        checks.append(dict(label='Required credential values stored', done=names <= values, target='credentials'))
        checks.append(dict(label='Credentials granted to this pending revision',
                           done=names <= set(app.get('grants', {}).get(version, [])), target='credential-grants'))
    return checks


def _fmt_time(value: Any) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))


def _fmt_bytes(value: Any) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _fmt_ago(value: Any) -> str:
    if not value:
        return "never"
    seconds = max(0, time.time() - float(value))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _fmt_duration(value: Any) -> str:
    seconds = int(float(value or 0))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def storage_view(app: dict[str, Any]) -> dict[str, Any] | None:
    """Used versus budget for the app page: numbers the platform measured itself."""
    runtime = app.get("runtime") or {}
    storage = runtime.get("storage") or {}
    limits = runtime.get("desired") or (app.get("manifests") or {}).get(app.get("live_version")) or {}
    budget = float(limits.get("storage_gb") or 0) * 1024**3
    used = storage.get("total_bytes")
    percent = round(used * 100 / budget) if budget and used is not None else None
    return {**storage, "budget_bytes": budget or None, "percent": percent,
            "level": "error" if percent is not None and percent >= 100 else "warn" if percent is not None and percent >= 80 else ""}


def summarize_request(item: dict[str, Any], domain: str) -> dict[str, Any]:
    """A description computed by the platform, never taken from the AI's text."""
    app, kind = item.get("app"), item.get("kind")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    host = f"{app}.{domain}" if app else "-"
    risk, text, confirm = "low", "", None
    if kind == "stage":
        stage = payload.get("stage")
        version = payload.get("live_version")
        about = f" (version {version}, the one live now; open it before you approve)" if version else ""
        if stage == "public":
            risk, confirm = "high", host
            text = f"Make {host} PUBLIC: anyone on the internet can open it, without logging in{about}."
        elif stage == "shared":
            who = ", ".join(_strings(payload.get("people")) + [f"group {g}" for g in _strings(payload.get("groups"))]) \
                or "nobody extra"
            risk = "medium"
            text = f"Share {host} with: {who} (plus you){about}."
        else:
            text = f"Make {host} visible only to you."
    elif kind == "release":
        risk = "medium"
        text = f"Release a new version ({payload.get('version', '?')}) of {host}. Review the changes first."
    elif kind == "start":
        risk = "medium"
        text = f"Bring {host} back online with its current visibility."
    elif kind == "stop":
        risk = "medium"
        text = f"Take {host} offline and stop its runtime components."
    elif kind == "remove":
        risk = "medium"
        text = f"Delete {host} and all its versions."
    elif kind == "log_access":
        text = f"Give the AI '{payload.get('mode')}' access to the visitor logs of {host} (full access lasts 24 hours)."
    elif kind == "secret":
        risk = "medium"
        text = f"Store a key named {payload.get('name')} for {host}. Code in that app can read it."
    elif kind == 'capabilities':
        risk,confirm='high',app
        text=f"Grant network/GPU capabilities to {host}. Changing grants takes the live app offline. Review the exact policy below."
    elif kind == 'development':
        risk='medium'
        text=f"Allow mutable development and package installation for {host}, based on revision {payload.get('version')}. Production data and keys stay separate. Review network/GPU grants below."
    elif kind == 'data_export':
        risk,confirm='high',app
        text=f"Stop {host} temporarily and export /data paths {', '.join(payload.get('paths',[]))}, up to {payload.get('max_bytes')} bytes, into the main workspace inbox. Selected data may contain sensitive values."
    elif kind == 'rollback':
        risk = 'medium'
        how = ('Its image is kept: the code is swapped in seconds.' if payload.get('role') == 'previous' else
               'It is rebuilt from its kept source and then released; that takes minutes.' if payload.get('image') == 'removed, rebuild needed' else
               'It is released again like a new version.')
        text = (f"Go back to revision {payload.get('version')} of {host}. {how} "
                "Code only: app data is not rolled back and stays as the current version left it.")
    else:
        risk, text = "high", "Unknown kind of request: reject it."
    return {"risk": risk, "text": text, "confirm": confirm}


def build_app(settings: AdminSettings, verifier: AccessVerifier | None = None,
              deployer: DeployerClient | None = None, files: FileManager | None = None) -> Starlette:
    verifier = verifier or AccessVerifier(settings.team_domain, settings.audience, (settings.owner_email,))
    deployer = deployer or DeployerClient(settings.deployer_socket)
    files = files or FileManager(settings.workspace, int(settings.max_upload_gb * 1024**3))
    try:
        files.clean_stale_uploads()
    except (FileError, OSError) as exc:
        logger.warning("could not clean old uploads: %s", exc)

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["time"] = _fmt_time
    env.filters["bytes"] = _fmt_bytes
    env.filters["ago"] = _fmt_ago
    env.filters["duration"] = _fmt_duration
    env.globals["STAGE_LABELS"] = STAGE_LABELS
    env.globals["QUOTA_LABELS"] = QUOTA_LABELS
    env.globals["OPERATION_LABELS"] = OPERATION_LABELS

    # Page polling must not repeatedly walk a large research workspace.
    workspace_storage_cache: tuple[float, dict[str, Any]] | None = None
    workspace_storage_lock = asyncio.Lock()

    async def workspace_storage() -> dict[str, Any]:
        nonlocal workspace_storage_cache
        async with workspace_storage_lock:
            if workspace_storage_cache is None or time.monotonic() - workspace_storage_cache[0] >= 60:
                data = await run_in_threadpool(files.storage_usage)
                workspace_storage_cache = (time.monotonic(), data)
            return workspace_storage_cache[1]


    def owner(request: Request) -> str:
        return request.scope["state"]["owner_email"]

    def flash_signature(msg: str, err: str) -> str:
        # Messages ride in the redirect URL; the signature stops a link from an
        # app from putting its own text on the admin page.
        digest = hmac.new(settings.csrf_key.encode(), f"flash|{msg}|{err}".encode(), hashlib.sha256)
        return digest.hexdigest()[:32]

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        email = owner(request)
        token = csrf_token(settings, email)
        msg = request.query_params.get("msg", "")
        err = request.query_params.get("err", "")
        if (msg or err) and not hmac.compare_digest(request.query_params.get("sig", ""), flash_signature(msg, err)):
            msg = err = ""
        context.update(
            owner=email, csrf=token, domain=settings.base_domain,
            csrf_q="_csrf=" + quote(token),
            msg=msg or None, err=err or None,
            path_now=request.url.path,
            live_page=template in {"dashboard.html", "app.html", "workspace.html", "people.html", "settings.html", "gpu.html", "audit.html", "logs.html"},
        )
        return HTMLResponse(env.get_template(template).render(**context))

    def back(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        params = {k: v for k, v in {"msg": msg, "err": err}.items() if v}
        if params:
            params["sig"] = flash_signature(msg or "", err or "")
        separator = "&" if "?" in url else "?"
        return RedirectResponse(url + (separator + urlencode(params) if params else ""), status_code=303)

    async def call(request: Request, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return await deployer.call(method, path, owner(request), **kwargs)

    def page_error(request: Request, exc: DeployerError | None = None) -> HTMLResponse:
        # Pages reached by a link never repeat text from that link (an app could
        # link here with a made-up name to put its own words on the admin page).
        message = str(exc) if exc else "No such app."
        if message.startswith("no app named"):
            message = "No such app."
        return render(request, "error.html", message=message)

    # ------------------------------------------------------------------ pages
    async def dashboard(request: Request) -> Response:
        try:
            status = await call(request, "GET", "/v1/status")
            apps = (await call(request, "GET", "/v1/apps"))["apps"]
        except DeployerError as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "dashboard.html", status=status, apps=apps)

    async def app_page(request: Request) -> Response:
        name = request.path_params["name"]
        if not looks_like_app_name(name):
            return page_error(request)
        try:
            app = await call(request, "GET", f"/v1/apps/{quote(name)}")
            people = await call(request, "GET", "/v1/people")
        except DeployerError as exc:
            return page_error(request, exc)
        # What the AI asked for, shown in the section of the page where it is decided.
        asks: dict[str, list[dict[str, Any]]] = {}
        for item in app.get("pending_requests", []):
            if item.get("kind") == "release":
                continue  # an old-style release request: the pending version itself is shown instead
            try:
                item["summary"] = summarize_request({**item, "app": name}, settings.base_domain)
            except Exception:  # one malformed ask must not hide the page
                logger.exception("cannot describe request %s", item.get("id"))
                item["summary"] = {"risk": "high", "text": "This ask cannot be shown: dismiss it.", "confirm": None}
            asks.setdefault(ASK_SECTIONS.get(item.get("kind"), "versions"), []).append(item)
        operations = app.get("operations", [])
        # The one that is working now; else the one that has waited longest.
        active = next((op for op in operations if op.get("status") == "running"), None) or \
            next((op for op in reversed(operations) if op.get("status") == "queued"), None)
        return render(request, "app.html", app=app, people=people["people"], groups=people["groups"],
                      release_checks=release_checklist(app), asks=asks, active_operation=active,
                      ask_items=[item for items in asks.values() for item in items],
                      storage=storage_view(app) if app.get("kind") == "runtime" else None)

    async def app_diff(request: Request) -> Response:
        name = request.path_params["name"]
        if not looks_like_app_name(name):
            return page_error(request)
        try:
            wanted = request.query_params.get("to")
            diff = await call(request, "GET", f"/v1/apps/{quote(name)}/diff", params={"to": wanted} if wanted else None)
            app = await call(request, "GET", f"/v1/apps/{quote(name)}")
        except DeployerError as exc:
            return page_error(request, exc)
        return render(request, "diff.html", name=name, diff=diff, release_checks=release_checklist(app))

    async def app_source(request: Request) -> Response:
        name = request.path_params['name']
        if not looks_like_app_name(name):
            return page_error(request)
        params = {key:request.query_params[key] for key in ('version','path','offset') if key in request.query_params}
        try:
            source = await call(request,'GET',f'/v1/apps/{quote(name)}/source',params=params)
        except DeployerError as exc:
            return page_error(request,exc)
        return render(request,'source.html',name=name,source=source)

    async def app_operations(request: Request) -> Response:
        name = request.path_params['name']
        if not looks_like_app_name(name):
            return JSONResponse({'error':'No such app.'},status_code=404)
        try:
            return JSONResponse(await call(request,'GET',f'/v1/apps/{quote(name)}/operations'))
        except DeployerError as exc:
            return JSONResponse({'error':str(exc)},status_code=502)

    async def app_operation_log(request: Request) -> Response:
        name, operation = request.path_params['name'], request.path_params['operation']
        if not looks_like_app_name(name) or not operation.isalnum():
            return JSONResponse({'error':'No such operation.'},status_code=404)
        try:
            return JSONResponse(await call(request,'GET',f'/v1/apps/{quote(name)}/operations/{quote(operation)}/log',
                                           params={'offset':request.query_params.get('offset','0')}))
        except DeployerError as exc:
            return JSONResponse({'error':str(exc)},status_code=502)

    async def app_operation_log_file(request: Request) -> Response:
        """The whole log as plain text (it is capped at about 5 MB by the deployer)."""
        name, operation = request.path_params['name'], request.path_params['operation']
        if not looks_like_app_name(name) or not operation.isalnum():
            return Response('Not found', status_code=404)
        parts, offset = [], 0
        try:
            for _ in range(40):
                piece = await call(request,'GET',f'/v1/apps/{quote(name)}/operations/{quote(operation)}/log',params={'offset':offset})
                parts.append(piece.get('text',''))
                if piece.get('next_offset',0) <= offset or piece.get('next_offset',0) >= piece.get('size',0):
                    break
                offset = piece['next_offset']
        except DeployerError as exc:
            return Response(str(exc), status_code=502, media_type='text/plain')
        headers = {"Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff",
                   "Content-Disposition": f'inline; filename="{name}-{operation[:8]}.log"'}
        return Response(''.join(parts), media_type='text/plain; charset=utf-8', headers=headers)

    async def storage_api(request: Request) -> Response:
        name = request.query_params.get('app', '')
        if name and not looks_like_app_name(name):
            return JSONResponse({'error':'No such app.'},status_code=404)
        try:
            data = await call(request, request.method, '/v1/storage', params={'app':name} if name else None)
        except DeployerError as exc:
            return JSONResponse({'error':str(exc)},status_code=502)
        if name and isinstance(data.get('app'), dict):
            app_storage = data['app']
            data['text'] = {key: (_fmt_bytes(app_storage[key]) if app_storage.get(key) is not None else 'not measured yet')
                            for key in ('data_bytes','source_volume_bytes','kept_sources_bytes','build_cache_bytes','total_bytes')}
        data['measured'] = _fmt_ago(data.get('measured_at')) if data.get('measured_at') else 'not measured yet'
        return JSONResponse(data)

    async def notices_api(request: Request) -> Response:
        after = request.query_params.get('after', '')
        if after and not after.isdigit():
            return JSONResponse({'error':'bad cursor'},status_code=400)
        try:
            return JSONResponse(await call(request,'GET','/v1/notices',params={'after':after} if after else None, timeout=10.0))
        except DeployerError as exc:
            return JSONResponse({'error':str(exc)},status_code=502)

    async def images_page(request: Request) -> Response:
        try:
            report = await call(request, "GET", "/v1/images", timeout=150.0)
            status = await call(request, "GET", "/v1/status")
        except DeployerError as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "images.html", report=report, status=status)

    async def images_cleanup(request: Request) -> Response:
        form = await request.form()
        action = str(form.get("action", ""))
        if action not in ("untagged", "image", "host-cache"):
            return back("/images", err="Unknown action.")
        try:
            result = await call(request, "POST", "/v1/images", json={"action": action, "image": str(form.get("image", ""))},
                                timeout=600.0)
        except DeployerError as exc:
            return back("/images", err=str(exc))
        if action == "host-cache":
            return back("/images", msg=f"Host build cache cleared: {_fmt_bytes(result.get('freed_bytes'))} freed.")
        kept = f" {result['kept']} could not be removed (in use)." if result.get("kept") else ""
        return back("/images", msg=f"Removed {len(result.get('removed', []))} image(s).{kept}")

    async def app_logs(request: Request) -> Response:
        name = request.path_params["name"]
        if not looks_like_app_name(name):
            return page_error(request)
        try:
            logs = await call(request, "GET", f"/v1/apps/{quote(name)}/logs", params={"lines": 300})
        except DeployerError as exc:
            return page_error(request, exc)
        return render(request, "logs.html", name=name, logs=logs)

    async def requests_page(request: Request) -> Response:
        # The separate requests page is gone: every ask is shown on its app's page,
        # and the dashboard lists what is waiting. Old links land on the dashboard.
        return RedirectResponse("/", status_code=303)

    async def people_page(request: Request) -> Response:
        try:
            data = await call(request, "GET", "/v1/people")
        except DeployerError as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "people.html", people=data["people"], groups=data["groups"])

    async def settings_page(request: Request) -> Response:
        try:
            data = await call(request, "GET", "/v1/settings")
            status = await call(request, "GET", "/v1/status")
        except DeployerError as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "settings.html", settings=data, status=status)

    async def workspace_page(request: Request) -> Response:
        try:
            tokens = await call(request, "GET", "/v1/workspace/tokens")
            resources = await call(request, "GET", "/v1/workspace/resources")
            storage = await workspace_storage()
        except DeployerError as exc:
            return page_error(request, exc)
        return render(request, "workspace.html", tokens=tokens["tokens"], resources=resources, storage=storage)

    async def workspace_token(request: Request) -> Response:
        form = await request.form()
        name = str(form.get("name", "")).strip()
        try:
            await call(request, "PUT", f"/v1/workspace/tokens/{quote(name, safe='')}",
                       json={"value": str(form.get("value", ""))})
        except DeployerError as exc:
            return back("/workspace", err=str(exc))
        return back("/workspace", msg="Token saved. New commands and jobs can use it.")

    async def workspace_token_delete(request: Request) -> Response:
        try:
            await call(request, "DELETE", f"/v1/workspace/tokens/{quote(request.path_params['secret'], safe='')}")
        except DeployerError as exc:
            return back("/workspace", err=str(exc))
        return back("/workspace", msg="Token removed from new commands. Stop existing jobs to revoke their copy.")

    async def workspace_resources(request: Request) -> Response:
        form = await request.form()
        try:
            await call(request, "PUT", "/v1/workspace/resources", json=dict(form))
        except DeployerError as exc:
            return back("/workspace", err=str(exc))
        return back("/workspace", msg="Workspace limits saved.")

    async def audit_page(request: Request) -> Response:
        try:
            data = await call(request, "GET", "/v1/audit")
        except DeployerError as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "audit.html", audit=data["audit"])

    # ------------------------------------------------------------------ app actions
    async def app_stage(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        stage = str(form.get("stage", ""))
        if stage == "public" and str(form.get("confirm", "")).strip() != f"{name}.{settings.base_domain}":
            return back(f"/apps/{quote(name)}", err=f"Type {name}.{settings.base_domain} to make it public.")
        # The version shown on the page: a stage change never applies to an upload
        # that arrived after the owner looked.
        body = {"stage": stage, "people": form.getlist("people"), "groups": form.getlist("groups"),
                "live_version": str(form.get("live_version", ""))}
        try:
            await call(request, "POST", f"/v1/apps/{quote(name)}/stage", json=body)
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg=f"{name} is now {STAGE_LABELS.get(stage, stage)}.")

    async def app_action(request: Request) -> Response:
        name, action = request.path_params["name"], request.path_params["action"]
        if action not in ("release", "rollback", "discard", "start", "stop", "preview", 'restart', 'restore', 'build-cache'):
            return back(f"/apps/{quote(name)}", err="unknown action")
        body = None
        if action in ("release", "rollback", "discard", "preview", 'restart', 'restore'):
            form = await request.form()
            body = {"version": str(form.get("version", ""))}
        elif action == 'build-cache':
            body = {}
        try:
            result = await call(request, "POST", f"/v1/apps/{quote(name)}/{action}", json=body)
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        if action == 'build-cache':
            return back(f"/apps/{quote(name)}#image-builds", msg="Build cache deleted. The next build of this app starts without it.")
        if result.get("what_happened"):
            return back(f"/apps/{quote(name)}", msg=str(result["what_happened"]) + " " + str(result.get("data", "")))
        return back(f"/apps/{quote(name)}", msg=("Operation queued. Its progress and log are shown at the top of this page." if result.get("operation_id") else f"Done: {action}."))

    async def app_settings(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        body = {"log_access": str(form.get("log_access", "default")), "auto_release": form.get("auto_release") == "on", "protected": form.get("protected") == "on"}
        try:
            await call(request, "POST", f"/v1/apps/{quote(name)}/settings", json=body)
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg="Settings saved.")

    async def app_remove(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        if str(form.get("confirm", "")).strip() != name:
            return back(f"/apps/{quote(name)}", err=f"Type {name} to delete it.")
        try:
            await call(request, "DELETE", f"/v1/apps/{quote(name)}")
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back("/", msg=f"{name} deleted.")

    async def app_resources(request: Request) -> Response:
        form = await request.form()
        name = request.path_params["name"]
        try:
            await call(request, "POST", f"/v1/apps/{quote(name)}/resources", json=dict(form))
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg="Container limits saved. Storage remains a budget.")

    async def app_grants(request: Request) -> Response:
        form = await request.form()
        name = request.path_params["name"]
        try:
            result = await call(request, "PUT", f"/v1/apps/{quote(name)}/grants",
                       json={"version":str(form.get("version", "")),"secrets":form.getlist("secrets")})
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg=('Grants saved. The live app was taken offline; bring it online after reviewing its required credentials.' if result.get('taken_offline') else 'Credential grants saved for this exact version.'))

    async def app_policy(request: Request) -> Response:
        form=await request.form()
        name=request.path_params['name']
        from ..deployer.capabilities import AI_ACTIONS
        body={'revision':str(form.get('revision','')),'environment':form.get('environment'),
              'permissions':{key:form.get(key)=='on' for key in AI_ACTIONS}}
        try: await call(request,'POST',f'/v1/apps/{quote(name)}/policy',json=body)
        except DeployerError as exc: return back(f'/apps/{quote(name)}',err=str(exc))
        return back(f'/apps/{quote(name)}',msg='Environment and AI permissions saved.')

    async def app_extension(request: Request) -> Response:
        import json
        form=await request.form()
        name,action=request.path_params['name'],request.path_params['action']
        if action not in ('capabilities','development','image-build','data-export'):
            return back(f'/apps/{quote(name)}',err='Unknown capability action.')
        body={'revision':str(form.get('revision','')),'version':str(form.get('version',''))}
        try:
            if action=='capabilities':
                if form.get('revoke')=='1': body.update(version='',capabilities={})
                elif form.get('policy'): body.update(version='',capabilities=json.loads(str(form['policy'])))
            elif action in ('development','image-build'):
                policy={'dns':form.get('dns')=='on','tcp':[],'udp':[]}
                for protocol in ('tcp','udp'):
                    ports=str(form.get(protocol,'')).strip()
                    if ports:
                        policy[protocol]=[{'destination':'public','ports':[] if ports=='all' else [int(p.strip()) for p in ports.split(',')]}]
                if action=='image-build': body.update(network=policy, no_cache=form.get('no_cache')=='on', refresh_base=form.get('refresh_base')=='on')
                else: body.update(action=form.get('action','grant'),capabilities={'gpu':form.get('gpu')=='on','network':policy},
                                  outbound=[p.strip() for p in str(form.get('outbound','')).splitlines() if p.strip()])
            else:
                body.update(paths=[p.strip() for p in str(form.get('paths','')).splitlines() if p.strip()],
                            max_bytes=int(form.get('max_mb','100'))*1024**2,stop_for_consistency=form.get('stop')=='on')
            result=await call(request,'POST',f'/v1/apps/{quote(name)}/{action}',json=body)
        except (DeployerError,ValueError) as exc: return back(f'/apps/{quote(name)}',err=str(exc))
        message = ('Operation queued. Its progress and log are shown at the top of this page.' if result.get('operation_id') else
                   'Capabilities saved. The live app is offline; bring it online after reviewing its grants.' if result.get('taken_offline') else
                   'Network and GPU grants saved.' if action == 'capabilities' else
                   'Development stopped. Installed packages and files are retained.' if body.get('action') == 'stop' else
                   'Package permission revoked; development is stopped.' if body.get('action') == 'revoke' else
                   'Development started.' if body.get('action') == 'start' else 'Development permissions saved.')
        return back(f'/apps/{quote(name)}',msg=message)

    async def app_secret(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        secret = str(form.get("name", "")).strip()
        try:
            await call(request, "PUT", f"/v1/apps/{quote(name)}/secrets/{quote(secret)}",
                       json={"value": str(form.get("value", ""))})
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg=f"Key {secret} saved. Restart a running app to apply the changed value.")

    async def app_secret_delete(request: Request) -> Response:
        name, secret = request.path_params["name"], request.path_params["secret"]
        try:
            await call(request, "DELETE", f"/v1/apps/{quote(name)}/secrets/{quote(secret)}")
        except DeployerError as exc:
            return back(f"/apps/{quote(name)}", err=str(exc))
        return back(f"/apps/{quote(name)}", msg=f"Key {secret} deleted from storage. Stop or revoke live grants to remove a running app's existing copy.")

    async def decide(request: Request) -> Response:
        request_id = int(request.path_params["request_id"])
        form = await request.form()
        approve = form.get("decision") == "approve"
        # The stored request decides where to go back to and what must be typed:
        # nothing of that is taken from the form.
        try:
            pending = (await call(request, "GET", "/v1/requests", params={"status": "pending"}))["requests"]
        except DeployerError as exc:
            return back("/", err=str(exc))
        item = next((r for r in pending if r["id"] == request_id), None)
        if item is None:
            return back("/", err="That ask is no longer waiting.")
        app_name = str(item.get("app") or "")
        page = f"/apps/{quote(app_name)}" if looks_like_app_name(app_name) else "/"
        if approve:
            expected = summarize_request(item, settings.base_domain)["confirm"]
            if expected and str(form.get("confirm", "")).strip() != expected:
                return back(page, err=f"Type {expected} to apply this.")
        body = {"approve": approve, "value": str(form.get("value", "")) or None}
        try:
            await call(request, "POST", f"/v1/requests/{request_id}/decide", json=body)
        except DeployerError as exc:
            return back(page, err=str(exc))
        if approve and item.get("kind") == "remove":
            page = "/"
        return back(page, msg="Applied." if approve else "Dismissed.")

    async def people_change(request: Request) -> Response:
        form = await request.form()
        body = {key: str(value) for key, value in form.items()}
        if "member" in body:
            body["member"] = body["member"] == "1"
        try:
            await call(request, "POST", "/v1/people", json=body)
        except DeployerError as exc:
            return back("/people", err=str(exc))
        return back("/people", msg="Saved.")

    async def settings_change(request: Request) -> Response:
        key = request.path_params["key"]
        form = await request.form()
        if key == "workspace_gpu_allowed":
            value: Any = form.get("value") == "on"
        else:
            value = {}
            for field, raw in form.items():
                if field == "games":
                    value["games"] = [line.strip() for line in str(raw).splitlines() if line.strip()]
                else:
                    value[field] = str(raw)
        try:
            await call(request, "PUT", f"/v1/settings/{quote(key)}", json={"value": value})
        except DeployerError as exc:
            return back("/settings", err=str(exc))
        return back("/settings", msg="Settings saved.")

    async def gaming(request: Request) -> Response:
        action = request.path_params["action"]
        try:
            await call(request, "POST", f"/v1/gaming/{action}", json={"source": "manual"})
        except DeployerError as exc:
            return back("/", err=str(exc))
        return back("/", msg=f"Gaming mode {action}.")

    async def gpu_page(request: Request) -> Response:
        try:
            report = await call(request, "GET", "/v1/gpu", timeout=90.0)
        except DeployerError as exc:
            return page_error(request, exc)
        return render(request, "gpu.html", report=report)

    async def gpu_kill(request: Request) -> Response:
        form = await request.form()
        if form.get("all") == "1":
            body: dict[str, Any] = {"all": True}
        else:
            try:
                body = {"pid": int(str(form.get("pid", "")))}
            except ValueError:
                return back("/gpu", err="Choose a process to kill.")
        try:
            result = await call(request, "POST", "/v1/gpu/kill", json=body, timeout=90.0)
        except DeployerError as exc:
            return back("/gpu", err=str(exc))
        if body.get("all"):
            return back("/gpu", msg=f"Killed {result.get('count', 0)} GPU process(es).")
        return back("/gpu", msg=f"Killed process {body['pid']}.")

    async def restore_visibility(request: Request) -> Response:
        try:
            await call(request, "POST", "/v1/visibility/restore")
        except DeployerError as exc:
            return back("/", err=str(exc))
        return back("/", msg="Saved visibility restored.")

    async def killswitch(request: Request) -> Response:
        form = await request.form()
        if str(form.get("confirm", "")).strip().lower() != "make everything private":
            return back("/", err="Type 'make everything private' to use the kill switch.")
        try:
            result = await call(request, "POST", "/v1/killswitch")
        except DeployerError as exc:
            return back("/", err=str(exc))
        return back("/", msg=f"{result.get('apps_reset', 0)} app(s) are only visible to you again.")

    # ------------------------------------------------------------------ files
    async def files_page(request: Request) -> Response:
        try:
            listing = await run_in_threadpool(files.listing, request.query_params.get("path", ""))
        except (FileError, OSError) as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "files.html", listing=listing, max_upload_gb=settings.max_upload_gb)

    async def file_view(request: Request) -> Response:
        try:
            info = await run_in_threadpool(files.describe, request.query_params.get("path", ""))
        except (FileError, OSError) as exc:
            return render(request, "error.html", message=str(exc))
        return render(request, "file_view.html", info=info)

    async def file_raw(request: Request) -> Response:
        try:
            handle, info, filename, media, inline = await run_in_threadpool(
                files.open_raw, request.query_params.get("path", ""), request.query_params.get("inline") == "1")
        except (FileError, OSError):
            return Response("Not found", status_code=404)
        headers = {
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        }
        return OpenedFileResponse(handle, info, filename=filename, media_type=media, inline=inline, headers=headers)

    async def file_op(request: Request) -> Response:
        op = request.path_params["op"]
        form = await request.form()
        here = str(form.get("here", ""))
        try:
            if op == "mkdir":
                await run_in_threadpool(files.mkdir, here, str(form.get("name", "")))
            elif op == "rename":
                await run_in_threadpool(files.rename, str(form.get("path", "")), str(form.get("name", "")))
            elif op == "delete":
                await run_in_threadpool(files.delete, str(form.get("path", "")))
            elif op == "empty-trash":
                await run_in_threadpool(files.empty_trash)
            else:
                raise FileError("unknown operation")
        except (FileError, OSError) as exc:
            return back(f"/files?path={quote(here)}", err=str(exc))
        return back(f"/files?path={quote(here)}", msg="Done.")

    async def upload_start(request: Request) -> Response:
        try:
            body = await request.json()
            result = await run_in_threadpool(files.start_upload, str(body.get("dir", "")),
                                             str(body.get("name", "")), int(body.get("size", 0)))
        except (FileError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result)

    async def upload_status(request: Request) -> Response:
        try:
            return JSONResponse(files.upload_status(request.path_params["upload_id"]))
        except (FileError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def upload_chunk(request: Request) -> Response:
        try:
            offset = int(request.query_params.get("offset", "-1"))
            data = await request.body()
            received = await run_in_threadpool(files.append_chunk, request.path_params["upload_id"], offset, data)
        except (FileError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"received": received})

    async def upload_finish(request: Request) -> Response:
        try:
            body = await request.json()
            path = await run_in_threadpool(files.finish_upload, request.path_params["upload_id"],
                                           bool(body.get("overwrite")))
        except (FileError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"path": path})

    async def upload_cancel(request: Request) -> Response:
        try:
            files.cancel_upload(request.path_params["upload_id"])
        except FileError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"cancelled": True})

    routes = [
        Route("/", dashboard),
        Route("/workspace", workspace_page),
        Route("/workspace/tokens", workspace_token, methods=["POST"]),
        Route("/workspace/tokens/{secret}/delete", workspace_token_delete, methods=["POST"]),
        Route("/workspace/resources", workspace_resources, methods=["POST"]),
        Route("/apps/{name}", app_page),
        Route("/apps/{name}/diff", app_diff),
        Route('/apps/{name}/source', app_source),
        Route('/apps/{name}/operations', app_operations),
        Route('/apps/{name}/operations/{operation}/log', app_operation_log),
        Route('/apps/{name}/operations/{operation}/log.txt', app_operation_log_file),
        Route('/api/storage', storage_api, methods=["GET", "POST"]),
        Route('/api/notices', notices_api),
        Route('/images', images_page),
        Route('/images/cleanup', images_cleanup, methods=["POST"]),
        Route("/apps/{name}/logs", app_logs),
        Route("/apps/{name}/stage", app_stage, methods=["POST"]),
        Route("/apps/{name}/settings", app_settings, methods=["POST"]),
        Route("/apps/{name}/remove", app_remove, methods=["POST"]),
        Route("/apps/{name}/resources", app_resources, methods=["POST"]),
        Route("/apps/{name}/grants", app_grants, methods=["POST"]),
        Route('/apps/{name}/policy',app_policy,methods=['POST']),
        Route('/apps/{name}/extend/{action}',app_extension,methods=['POST']),
        Route("/apps/{name}/secrets", app_secret, methods=["POST"]),
        Route("/apps/{name}/secrets/{secret}/delete", app_secret_delete, methods=["POST"]),
        Route("/apps/{name}/do/{action}", app_action, methods=["POST"]),
        Route("/requests", requests_page),
        Route("/requests/{request_id:int}/decide", decide, methods=["POST"]),
        Route("/people", people_page),
        Route("/people", people_change, methods=["POST"]),
        Route("/settings", settings_page),
        Route("/settings/{key}", settings_change, methods=["POST"]),
        Route("/gaming/{action}", gaming, methods=["POST"]),
        Route("/killswitch", killswitch, methods=["POST"]),
        Route("/visibility/restore", restore_visibility, methods=["POST"]),
        Route("/gpu", gpu_page),
        Route("/gpu/kill", gpu_kill, methods=["POST"]),
        Route("/audit", audit_page),
        Route("/files", files_page),
        Route("/files/view", file_view),
        Route("/files/raw", file_raw),
        Route("/files/do/{op}", file_op, methods=["POST"]),
        Route("/api/uploads", upload_start, methods=["POST"]),
        Route("/api/uploads/{upload_id}", upload_status),
        Route("/api/uploads/{upload_id}", upload_chunk, methods=["PUT"]),
        Route("/api/uploads/{upload_id}", upload_cancel, methods=["DELETE"]),
        Route("/api/uploads/{upload_id}/finish", upload_finish, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=HERE / "static"), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(AdminSecurityMiddleware, settings=settings, verifier=verifier)])
