from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import jwt
from jwt import PyJWKClient
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, settings as default_settings

logger = logging.getLogger(__name__)

KeyResolver = Callable[[str], Any]


class CloudflareAccessMiddleware:
    """Validate Cloudflare Access JWTs at the origin and fail closed.

    Cloudflare Access at the edge is the primary gate for mcp.example.com. This
    check guards against misrouted traffic reaching the server another way (a
    wrong tunnel route, another container on a shared network). It does not
    protect against code running inside this container: the AI shell has sudo
    here and could change this file.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings | None = None,
        key_resolver: KeyResolver | None = None,
    ) -> None:
        self.app = app
        self.settings = settings or default_settings
        self._key_resolver = key_resolver
        self._jwk_client: PyJWKClient | None = None
        if key_resolver is None and self.settings.cf_access_team_domain:
            self._jwk_client = PyJWKClient(
                f"{self.settings.cf_access_team_domain}/cdn-cgi/access/certs",
                cache_keys=True,
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        settings = self.settings
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        path = scope.get("path", "")

        # The health check reveals nothing sensitive and must work for the
        # container healthcheck, which calls 127.0.0.1 from inside.
        if path == "/healthz":
            await self.app(scope, receive, send)
            return

        if settings.local_dev_insecure:
            await self.app(scope, receive, send)
            return

        if settings.locked:
            await JSONResponse(
                {"error": "MCP endpoint is locked: Cloudflare Access is not configured on the server"},
                status_code=503,
            )(scope, receive, send)
            return

        host = headers.get("host", "").split(":", 1)[0].lower()
        if host != settings.public_mcp_host:
            await PlainTextResponse("Unknown host", status_code=421)(scope, receive, send)
            return

        token = headers.get("cf-access-jwt-assertion")
        if not token:
            await JSONResponse(
                {"error": "Missing Cloudflare Access JWT"}, status_code=401
            )(scope, receive, send)
            return

        try:
            claims = await asyncio.to_thread(self._validate_token, token)
        except Exception as exc:  # JWT libraries raise several subclasses.
            logger.warning("Cloudflare Access JWT validation failed: %s", exc)
            await JSONResponse(
                {"error": "Invalid Cloudflare Access JWT"}, status_code=403
            )(scope, receive, send)
            return

        # Service-token JWTs carry no email; they are never accepted here.
        email = str(claims.get("email") or "").strip().lower()
        if not email or email not in settings.cf_access_allowed_emails:
            logger.warning("Rejected Cloudflare Access identity %r", email or "<no email>")
            await JSONResponse({"error": "User is not allowed"}, status_code=403)(
                scope, receive, send
            )
            return

        scope.setdefault("state", {})["cf_access_claims"] = claims
        await self.app(scope, receive, send)

    def _signing_key(self, token: str) -> Any:
        if self._key_resolver is not None:
            return self._key_resolver(token)
        if self._jwk_client is None:
            raise RuntimeError("Cloudflare JWKS client is not configured")
        return self._jwk_client.get_signing_key_from_jwt(token).key

    def _validate_token(self, token: str) -> dict[str, Any]:
        return jwt.decode(
            token,
            self._signing_key(token),
            algorithms=["RS256"],
            audience=self.settings.cf_access_aud,
            issuer=self.settings.cf_access_team_domain,
            leeway=self.settings.jwt_leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
