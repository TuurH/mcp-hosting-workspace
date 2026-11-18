from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import CloudflareAccessMiddleware
from app.config import Settings

TEAM = "https://team.cloudflareaccess.com"
AUD = "aud-mcp"
OWNER = "owner@mcp-hosting.test"
HOST = "mcp.example.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        workspace=tmp_path,
        public_mcp_host=HOST,
        cf_access_enabled=True,
        cf_access_team_domain=TEAM,
        cf_access_aud=AUD,
        cf_access_allowed_emails=(OWNER,),
        local_dev_insecure=False,
        jwt_leeway_seconds=60,
    )
    values.update(overrides)
    return Settings(**values)


def make_token(key=_KEY, **claims) -> str:
    now = int(time.time())
    payload = {"iss": TEAM, "aud": [AUD], "email": OWNER, "iat": now, "exp": now + 300}
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm="RS256")


def client_for(settings: Settings) -> TestClient:
    async def endpoint(request: Request) -> PlainTextResponse:
        claims = request.scope.get("state", {}).get("cf_access_claims", {})
        return PlainTextResponse(f"ok {claims.get('email', '-')}")

    app = Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])
    wrapped = CloudflareAccessMiddleware(app, settings=settings, key_resolver=lambda _t: _KEY.public_key())
    return TestClient(wrapped, base_url=f"https://{HOST}")


def get(client: TestClient, path: str = "/mcp", host: str = HOST, token: str | None = None):
    headers = {"host": host}
    if token is not None:
        headers["cf-access-jwt-assertion"] = token
    return client.get(path, headers=headers)


def test_valid_owner_token_is_accepted(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token())
    assert response.status_code == 200
    assert response.text == f"ok {OWNER}"


def test_email_comparison_is_case_insensitive(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(email="Owner@MCP-HOSTING.TEST"))
    assert response.status_code == 200


def test_healthz_needs_no_token_on_any_host(tmp_path):
    client = client_for(make_settings(tmp_path))
    assert get(client, "/healthz", host="127.0.0.1").status_code == 200


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "workspace", "0.0.0.0", ""])
def test_local_host_headers_get_no_bypass(tmp_path, host):
    # The old server skipped authentication for these Host values.
    response = get(client_for(make_settings(tmp_path)), host=host)
    assert response.status_code in {400, 421}


def test_unknown_host_is_rejected_even_with_valid_token(tmp_path):
    response = get(client_for(make_settings(tmp_path)), host="files.example.com", token=make_token())
    assert response.status_code == 421


def test_missing_token_is_401(tmp_path):
    assert get(client_for(make_settings(tmp_path))).status_code == 401


def test_wrong_signature_is_403(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(key=_OTHER_KEY))
    assert response.status_code == 403


def test_wrong_audience_is_403(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(aud=["aud-admin"]))
    assert response.status_code == 403


def test_wrong_issuer_is_403(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(iss="https://evil.cloudflareaccess.com"))
    assert response.status_code == 403


def test_expired_beyond_leeway_is_403(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(exp=int(time.time()) - 120))
    assert response.status_code == 403


def test_expired_within_leeway_is_accepted(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(exp=int(time.time()) - 30))
    assert response.status_code == 200


def test_token_without_email_is_403(tmp_path):
    # Cloudflare service-token JWTs carry no email claim.
    response = get(client_for(make_settings(tmp_path)), token=make_token(email=None, common_name="svc"))
    assert response.status_code == 403


def test_other_email_is_403(tmp_path):
    response = get(client_for(make_settings(tmp_path)), token=make_token(email="someone@mcp-hosting.test"))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "override",
    [
        {"cf_access_enabled": False},
        {"cf_access_team_domain": ""},
        {"cf_access_aud": ""},
        {"cf_access_allowed_emails": ()},
    ],
)
def test_incomplete_access_config_locks_the_endpoint(tmp_path, override):
    settings = make_settings(tmp_path, **override)
    assert settings.locked
    response = get(client_for(settings), token=make_token())
    assert response.status_code == 503


@pytest.mark.parametrize(
    "override",
    [
        {"cf_access_team_domain": "https://CHANGE-ME.cloudflareaccess.com"},
        {"cf_access_aud": "CHANGE-ME"},
        {"cf_access_allowed_emails": ("your-email@example.com",)},
    ],
)
def test_env_example_placeholders_keep_the_endpoint_locked(tmp_path, override):
    assert make_settings(tmp_path, **override).locked


def test_local_dev_insecure_skips_auth(tmp_path):
    settings = make_settings(tmp_path, local_dev_insecure=True, cf_access_enabled=False)
    assert not settings.locked
    assert get(client_for(settings), host="127.0.0.1").status_code == 200
