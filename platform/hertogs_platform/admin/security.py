"""Owner login check, CSRF protection and security headers for the admin page.

Every app the AI builds lives on *.example.com, the same "site" as
admin.example.com, so browser SameSite rules do not separate them. State-changing
requests therefore need all of: a same-origin Sec-Fetch-Site (when sent), an
exact Origin match, and a synchronizer CSRF token tied to the owner.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..common.access import AccessVerifier
from .config import AdminSettings

logger = logging.getLogger("hertogs.admin.security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_COOKIE = "hertogs_admin_local"
LOCAL_SESSION_SECONDS = 8 * 3600
_LOCAL_ORIGIN = re.compile(r"^http://localhost(:\d{1,5})?$")

SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     b"connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    # Live updates merge the HTML into an existing document. Edge-injected email
    # placeholders would stay hidden because their decoder does not run again.
    (b"cache-control", b"no-store, no-transform"),
]


def csrf_token(settings: AdminSettings, email: str, day: int | None = None) -> str:
    day = int(time.time() // 86_400) if day is None else day
    digest = hmac.new(settings.csrf_key.encode(), f"{email}|{day}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def csrf_valid(settings: AdminSettings, email: str, supplied: str | None) -> bool:
    if not supplied:
        return False
    today = int(time.time() // 86_400)
    return any(hmac.compare_digest(supplied, csrf_token(settings, email, day)) for day in (today, today - 1))


class AdminSecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: AdminSettings, verifier: AccessVerifier) -> None:
        self.app = app
        self.settings = settings
        self.verifier = verifier
        self.local_token = ""
        self.local_link_used = False
        self.local_sessions: dict[str, float] = {}  # session id -> expiry
        if settings.local_dev_insecure:
            # Other containers (the AI's workspace too) can reach published host
            # ports, so local mode still needs a secret: a one-time link in the log.
            # It works once (until the container restarts) and starts a session of
            # its own, so the link in the log is worthless afterwards.
            self.local_token = settings.local_token or secrets.token_urlsafe(24)
            logger.warning("Local test mode. Open this link in your browser (it works once): "
                           "http://localhost:8088/local-login?token=%s", self.local_token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in {h for h, _ in SECURITY_HEADERS}]
                # Raw file downloads set their own stricter policy; keep it.
                own_csp = any(k.lower() == b"content-security-policy" for k, _ in message.get("headers", []))
                for name, value in SECURITY_HEADERS:
                    if name == b"content-security-policy" and own_csp:
                        headers += [(k, v) for k, v in message["headers"] if k.lower() == name]
                        continue
                    headers.append((name, value))
                message = dict(message, headers=headers)
            await send(message)

        request = Request(scope)
        if request.url.path == "/healthz":
            await PlainTextResponse("ok")(scope, receive, send)
            return

        problems = self.settings.problems()
        if problems:
            await PlainTextResponse(
                "The admin page is not configured yet: " + "; ".join(problems) + ". See docs/SETUP.md.",
                status_code=503,
            )(scope, receive, send_with_headers)
            return

        host = request.headers.get("host", "").split(":")[0].lower()
        if not self.settings.local_dev_insecure and host != self.settings.public_host:
            await PlainTextResponse("Unknown host", status_code=421)(scope, receive, send_with_headers)
            return

        if self.local_token and request.url.path == "/local-login":
            if self.local_link_used:
                await PlainTextResponse("This link was already used. Restart the admin container for a new one.",
                                        status_code=403)(scope, receive, send_with_headers)
            elif hmac.compare_digest(request.query_params.get("token", ""), self.local_token):
                self.local_link_used = True
                session = secrets.token_urlsafe(32)
                now = time.time()
                self.local_sessions = {k: v for k, v in self.local_sessions.items() if v > now}
                self.local_sessions[session] = now + LOCAL_SESSION_SECONDS
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(LOCAL_COOKIE, session, max_age=LOCAL_SESSION_SECONDS,
                                    httponly=True, samesite="strict", path="/")
                await response(scope, receive, send_with_headers)
            else:
                await PlainTextResponse("Wrong link", status_code=403)(scope, receive, send_with_headers)
            return

        email = self._owner(request)
        if email is None:
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send_with_headers)
            return

        if request.method not in SAFE_METHODS:
            problem = self._unsafe_request_problem(request, email)
            if problem:
                logger.warning("blocked %s %s: %s", request.method, request.url.path, problem)
                await PlainTextResponse(f"Blocked: {problem}", status_code=403)(scope, receive, send_with_headers)
                return

        scope.setdefault("state", {})["owner_email"] = email
        await self.app(scope, receive, send_with_headers)

    def _owner(self, request: Request) -> str | None:
        if self.settings.local_dev_insecure:
            supplied = request.cookies.get(LOCAL_COOKIE, "")
            expiry = self.local_sessions.get(supplied) if supplied else None
            if expiry is not None and expiry > time.time():
                return self.settings.owner_email
            logger.warning("local test mode: no valid login link cookie (see docker compose logs admin)")
            return None
        token = request.headers.get("cf-access-jwt-assertion")
        if not token:
            return None
        try:
            return self.verifier.verify(token)["email"]
        except Exception as exc:  # invalid, expired, wrong audience or user
            logger.warning("admin access denied: %s", exc)
            return None

    def _unsafe_request_problem(self, request: Request, email: str) -> str | None:
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site != "same-origin":
            return f"cross-site request ({fetch_site})"
        origin = request.headers.get("origin") or ""
        if self.settings.local_dev_insecure:
            origin_ok = bool(_LOCAL_ORIGIN.match(origin))
        else:
            origin_ok = origin == self.settings.origin
        if not origin_ok:
            return "missing or foreign Origin header"
        supplied = request.headers.get("x-csrf-token")
        if supplied is None:
            # Form posts carry the token as a query parameter added by the template,
            # so the body never has to be parsed here.
            supplied = request.query_params.get("_csrf")
        if not csrf_valid(self.settings, email, supplied):
            return "missing or invalid CSRF token"
        return None
