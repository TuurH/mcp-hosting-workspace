"""Cloudflare Access JWT verification for platform services (the admin page)."""

from __future__ import annotations

from typing import Any, Callable

import jwt
from jwt import PyJWKClient

KeyResolver = Callable[[str], Any]


class AccessVerifier:
    def __init__(
        self,
        team_domain: str,
        audience: str,
        allowed_emails: tuple[str, ...],
        *,
        leeway_seconds: int = 60,
        key_resolver: KeyResolver | None = None,
    ) -> None:
        self.team_domain = team_domain.rstrip("/")
        self.audience = audience
        self.allowed_emails = tuple(email.strip().lower() for email in allowed_emails if email.strip())
        self.leeway_seconds = leeway_seconds
        self._key_resolver = key_resolver
        self._jwks = (
            PyJWKClient(f"{self.team_domain}/cdn-cgi/access/certs", cache_keys=True)
            if key_resolver is None and self.team_domain
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self.team_domain and self.audience and self.allowed_emails)

    def _key(self, token: str) -> Any:
        if self._key_resolver is not None:
            return self._key_resolver(token)
        if self._jwks is None:
            raise RuntimeError("Cloudflare Access is not configured")
        return self._jwks.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> dict[str, Any]:
        """Return the claims of a valid token for an allowed user, or raise."""
        if not self.configured:
            raise PermissionError("Cloudflare Access is not configured")
        claims = jwt.decode(
            token,
            self._key(token),
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.team_domain,
            leeway=self.leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
        email = str(claims.get("email") or "").strip().lower()
        if not email or email not in self.allowed_emails:
            raise PermissionError(f"user {email or '<no email>'} is not allowed")
        claims["email"] = email
        return claims
