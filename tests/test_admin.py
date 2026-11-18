from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from mcp_hosting_platform.admin.app import build_app, summarize_request
from mcp_hosting_platform.admin.config import AdminSettings
from mcp_hosting_platform.admin.deployer_client import DeployerError
from mcp_hosting_platform.admin.filemanager import FileError, FileManager
from mcp_hosting_platform.admin.security import csrf_token
from mcp_hosting_platform.common.access import AccessVerifier

TEAM = "https://team.cloudflareaccess.com"
AUD = "aud-admin"
OWNER = "owner@mcp-hosting.test"
HOST = "admin.example.com"
ORIGIN = f"https://{HOST}"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(**claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": TEAM, "aud": [AUD], "email": OWNER, "iat": now, "exp": now + 300}
    payload.update(claims)
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, _KEY, algorithm="RS256")


class FakeDeployer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Any]] = []
        self.pending: list[dict[str, Any]] = []

    async def call(self, method: str, path: str, actor: str, json: Any = None,
                   params: dict[str, Any] | None = None, timeout: float = 180.0) -> dict[str, Any]:
        self.calls.append((method, path, actor, json))
        if path == "/v1/status":
            return {"apps": 0, "gaming": "off", "workspace_gpu_allowed": True, "pending_requests": 0}
        if path == "/v1/apps":
            return {"apps": []}
        if path == "/v1/requests":
            return {"requests": self.pending}
        if path == "/v1/people":
            return {"people": [], "groups": []}
        if path == "/v1/apps/blog":
            return {"name": "blog", "kind": "static", "stage": "dev", "environment": "development",
                    "effective_visibility": "dev", "protected": False, "online": True, "url": "https://blog.example.com",
                    "preview_url": "https://next-blog.example.com", "live_version": "a" * 32, "pending_version": None,
                    "previous_version": None, "people": [], "groups": [], "log_access": "default", "auto_release": True,
                    "secrets": [], "revision": "r1", "going_back": "Code only.", "revisions": [],
                    "ai_permissions": {}, "stats": {"requests_24h": 1, "unique_visitors_24h": 1, "bytes_24h": 1, "partial": False},
                    "pending_requests": [{k: v for k, v in item.items() if k != "app"} for item in self.pending]}
        if path.endswith("/log"):
            return {"id": "c" * 32, "status": "running", "phase": "Building the image", "text": "step 1\n",
                    "offset": int((params or {}).get("offset", 0)), "next_offset": 7, "size": 7}
        if path == "/v1/storage":
            return {"measured_at": None, "running": method == "POST", "error": None,
                    **({"app": {"total_bytes": None, "data_bytes": 5}} if params else {})}
        if path == "/v1/notices":
            return {"latest": 9, "notices": [] if params else [{"id": 9, "level": "ok", "text": "blog: release finished (1 min)"}]}
        if path == "/v1/images":
            if method == "POST":
                return {"removed": ["sha256:" + "1" * 64], "kept": 0}
            return {"unused": [{"id": "sha256:" + "1" * 64, "tags": ["old:1"], "bytes": 10, "unique_bytes": 10, "created": 0},
                               {"id": "sha256:" + "2" * 64, "tags": [], "bytes": 10, "unique_bytes": 10, "created": 0}],
                    "untagged_count": 1, "untagged_bytes": 10, "tagged_bytes": 10, "note": "Estimates."}
        if path.startswith("/v1/apps/missing"):
            raise DeployerError("no app named 'missing'")
        if path == "/v1/gpu":
            return {"gaming": "on", "paused": True, "frozen_by_platform": True, "held": True, "error": None,
                    "count": 1, "seconds": 0.4, "kcmp": False, "partial": False, "unchecked": 0,
                    "sample": [{"pid": 321, "command": "python train.py", "reason": "GPU device open", "state": "S"}],
                    "protected_sample": []}
        if path == "/v1/gpu/kill":
            return {"killed": 1, "pid": (json or {}).get("pid"), "count": 1}
        return {}


def settings(tmp_path: Path, **overrides: Any) -> AdminSettings:
    values = dict(
        public_host=HOST, team_domain=TEAM, audience=AUD, owner_email=OWNER,
        csrf_key="k" * 40, deployer_socket=str(tmp_path / "none.sock"),
        workspace=tmp_path / "ws", port=8080, base_domain="example.com",
        max_upload_gb=1.0, local_dev_insecure=False,
    )
    values.update(overrides)
    (tmp_path / "ws").mkdir(exist_ok=True)
    return AdminSettings(**values)


@pytest.fixture
def admin(tmp_path):
    s = settings(tmp_path)
    deployer = FakeDeployer()
    verifier = AccessVerifier(TEAM, AUD, (OWNER,), key_resolver=lambda _t: _KEY.public_key())
    client = TestClient(build_app(s, verifier=verifier, deployer=deployer), base_url=ORIGIN)
    return s, deployer, client


def owner_headers(**extra: str) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": token(), **extra}


def post_headers(s: AdminSettings, **extra: str) -> dict[str, str]:
    return owner_headers(Origin=ORIGIN, **{"Sec-Fetch-Site": "same-origin"}, **extra)


def csrf_q(s: AdminSettings) -> str:
    return "_csrf=" + csrf_token(s, OWNER)


# ---------------------------------------------------------------- login
def test_healthz_is_open(admin):
    _, _, client = admin
    assert client.get("/healthz").text == "ok"


def test_live_pages_and_separate_save_forms(admin):
    _, _, client = admin
    page = client.get('/apps/blog', headers=owner_headers()).text
    assert 'data-live-page="/apps/blog"' in page
    assert 'id="permission-settings"' in page and 'id="log-access"' in page
    policy = re.search(r'<form[^>]+/policy\?[^>]*>(.*?)</form>', page, re.S)[1]
    logs = re.search(r'<form[^>]+/settings\?[^>]*>(.*?)</form>', page, re.S)[1]
    assert 'name="environment"' in policy and 'name="log_access"' not in policy
    assert 'name="log_access"' in logs and 'name="environment"' not in logs
    assert 'data-live-page=' not in client.get('/apps/missing', headers=owner_headers()).text
    assert 'data-live-page="/"' in client.get('/', headers=owner_headers()).text


def test_image_build_form_preserves_cache_flags(admin):
    settings, deployer, client = admin
    response = client.post('/apps/blog/extend/image-build?' + csrf_q(settings), headers=post_headers(settings),
                           data={'revision': 'r1', 'version': 'b' * 32, 'tcp': '80,443',
                                 'dns': 'on', 'no_cache': 'on', 'refresh_base': 'on'}, follow_redirects=False)
    assert response.status_code == 303
    body = deployer.calls[-1][3]
    assert body['no_cache'] is True and body['refresh_base'] is True


def test_workspace_polling_reuses_storage_scan(admin, monkeypatch):
    _, deployer, client = admin
    original = deployer.call
    async def call(method, path, actor, **kwargs):
        if path == '/v1/workspace/tokens':
            return {'tokens': []}
        if path == '/v1/workspace/resources':
            return {'available': False}
        return await original(method, path, actor, **kwargs)
    deployer.call = call
    scans = []
    monkeypatch.setattr(FileManager, 'storage_usage', lambda self: scans.append(True) or {'used': 0, 'partial': False, 'disk_free': 1})
    assert client.get('/workspace', headers=owner_headers()).status_code == 200
    assert client.get('/workspace', headers=owner_headers()).status_code == 200
    assert scans == [True]


def test_requires_owner_token(admin):
    _, _, client = admin
    assert client.get("/").status_code == 403
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(email="other@mcp-hosting.test")}).status_code == 403
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(aud=["other"])}).status_code == 403
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(email=None)}).status_code == 403
    response = client.get("/", headers=owner_headers())
    assert response.status_code == 200
    assert "MCP Hosting Workspace" in response.text


def test_rejects_other_hosts(admin):
    _, _, client = admin
    response = client.get("/", headers=owner_headers(), )
    assert response.status_code == 200
    other = TestClient(client.app, base_url="https://blog.example.com")
    assert other.get("/", headers=owner_headers()).status_code == 421


def test_security_headers(admin):
    _, _, client = admin
    response = client.get("/", headers=owner_headers())
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store, no-transform"


def test_locked_until_configured(tmp_path):
    s = settings(tmp_path, audience="")
    client = TestClient(build_app(s, deployer=FakeDeployer()), base_url=ORIGIN)
    assert client.get("/healthz").status_code == 200
    response = client.get("/", headers=owner_headers())
    assert response.status_code == 503
    assert "ADMIN_CF_ACCESS_AUD" in response.text


def test_local_mode_needs_the_login_link(tmp_path):
    s = settings(tmp_path, local_dev_insecure=True, local_token="t" * 32, team_domain="", audience="")
    deployer = FakeDeployer()
    client = TestClient(build_app(s, deployer=deployer), base_url="http://localhost:8088")
    assert client.get("/").status_code == 403  # e.g. the AI through host.docker.internal
    assert client.get("/local-login", params={"token": "wrong"}).status_code == 403
    login = client.get("/local-login", params={"token": "t" * 32}, follow_redirects=False)
    assert login.status_code == 303
    assert "httponly" in login.headers["set-cookie"].lower() and "samesite=strict" in login.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200
    # The link works once, and the token itself is not a session.
    assert client.get("/local-login", params={"token": "t" * 32}).status_code == 403
    other = TestClient(build_app(s, deployer=deployer), base_url="http://localhost:8088")
    other.cookies.set("mcp_hosting_admin_local", "t" * 32)
    assert other.get("/").status_code == 403
    token = csrf_token(s, OWNER)
    evil = {"Origin": "http://localhost.evil.example", "Sec-Fetch-Site": "same-origin"}
    assert client.post(f"/gaming/on?_csrf={token}", headers=evil, follow_redirects=False).status_code == 403
    good = {"Origin": "http://localhost:8088", "Sec-Fetch-Site": "same-origin"}
    assert client.post(f"/gaming/on?_csrf={token}", headers=good, follow_redirects=False).status_code == 303


def test_flash_messages_must_be_signed(admin):
    _, _, client = admin
    spoof = client.get("/", params={"msg": "Request 7 is safe to approve"}, headers=owner_headers())
    assert "Request 7 is safe" not in spoof.text


def test_error_pages_do_not_repeat_the_link(admin):
    _, _, client = admin
    for path in ("/apps/missing", "/apps/missing/diff", "/apps/missing/logs",
                 "/apps/Call%20support%20now/diff", "/apps/" + "x" * 80):
        page = client.get(path, headers=owner_headers())
        assert page.status_code == 200 and "No such app." in page.text, path
        assert "missing" not in page.text and "support" not in page.text, path


def test_gpu_page_shows_frozen_processes_and_kills_them(admin):
    s, deployer, client = admin
    page = client.get("/gpu", headers=owner_headers())
    assert page.status_code == 200
    assert "python train.py" in page.text and "keep their GPU memory" in page.text and "kcmp" in page.text
    killed = client.post("/gpu/kill", params={"_csrf": csrf_token(s, OWNER)}, data={"pid": "321"},
                         headers=post_headers(s), follow_redirects=False)
    assert killed.status_code == 303 and "msg=" in killed.headers["location"]
    assert ("POST", "/v1/gpu/kill", OWNER, {"pid": 321}) in deployer.calls
    refused = client.post("/gpu/kill", data={"pid": "321"}, headers=owner_headers(), follow_redirects=False)
    assert refused.status_code == 403  # no CSRF token


def test_malformed_requests_do_not_break_the_page(admin):
    _, deployer, client = admin
    deployer.pending = [
        {"id": 1, "app": "blog", "kind": "stage", "payload": {"stage": "shared", "people": "x"}, "note": "", "created_at": 0},
        {"id": 2, "app": "blog", "kind": "weird", "payload": [], "note": "", "created_at": 0},
    ]
    page = client.get("/apps/blog", headers=owner_headers())
    assert page.status_code == 200
    assert "reject it" in page.text and 'id="ask-1"' in page.text and 'id="ask-2"' in page.text


# ---------------------------------------------------------------- CSRF
def test_post_needs_csrf_origin_and_same_origin(admin):
    s, deployer, client = admin
    url = "/gaming/on"
    good = client.post(f"{url}?{csrf_q(s)}", headers=post_headers(s), follow_redirects=False)
    assert good.status_code == 303
    assert deployer.calls[-1][:3] == ("POST", "/v1/gaming/on", OWNER)

    count = len(deployer.calls)
    assert client.post(url, headers=post_headers(s), follow_redirects=False).status_code == 403
    assert client.post(f"{url}?_csrf=wrong", headers=post_headers(s), follow_redirects=False).status_code == 403
    cross = owner_headers(Origin="https://evil.example.com", **{"Sec-Fetch-Site": "same-site"})
    assert client.post(f"{url}?{csrf_q(s)}", headers=cross, follow_redirects=False).status_code == 403
    no_origin = owner_headers(**{"Sec-Fetch-Site": "same-origin"})
    assert client.post(f"{url}?{csrf_q(s)}", headers=no_origin, follow_redirects=False).status_code == 403
    foreign_origin = owner_headers(Origin="https://blog.example.com")
    assert client.post(f"{url}?{csrf_q(s)}", headers=foreign_origin, follow_redirects=False).status_code == 403
    assert len(deployer.calls) == count  # nothing reached the deployer


def test_csrf_header_works_for_fetch(admin):
    s, _, client = admin
    response = client.post("/api/uploads", json={"dir": "", "name": "a.txt", "size": 3},
                           headers=post_headers(s, **{"X-CSRF-Token": csrf_token(s, OWNER)}))
    assert response.status_code == 200
    assert re.fullmatch(r"[a-f0-9]{32}", response.json()["upload_id"])


def test_csrf_token_is_bound_to_the_owner(tmp_path):
    s = settings(tmp_path)
    assert csrf_token(s, OWNER) != csrf_token(s, "other@mcp-hosting.test")
    assert csrf_token(s, OWNER) != csrf_token(settings(tmp_path, csrf_key="z" * 40), OWNER)


# ---------------------------------------------------------------- requests page
def test_public_request_needs_typed_confirmation_from_the_server(admin):
    s, deployer, client = admin
    deployer.pending = [{"id": 7, "app": "blog", "kind": "stage", "payload": {"stage": "public"},
                         "note": "", "created_at": time.time()}]
    url = f"/requests/7/decide?{csrf_q(s)}"
    # A forged form without the confirmation must not get through.
    response = client.post(url, data={"decision": "approve"}, headers=post_headers(s), follow_redirects=False)
    assert response.status_code == 303 and "err=" in response.headers["location"]
    assert not any(call[1] == "/v1/requests/7/decide" for call in deployer.calls)
    response = client.post(url, data={"decision": "approve", "confirm": "blog.example.com"},
                           headers=post_headers(s), follow_redirects=False)
    # Back on the page of the app, where the ask was shown.
    assert response.headers["location"].startswith("/apps/blog?") and "msg=Applied" in response.headers["location"]
    assert deployer.calls[-1][1] == "/v1/requests/7/decide"
    # The signed message is shown after the redirect.
    deployer.pending = []
    shown = client.get(response.headers["location"], headers=owner_headers())
    assert "Applied." in shown.text
    # An ask that was decided meanwhile changes nothing and says so.
    count = len([call for call in deployer.calls if call[1].endswith("/decide")])
    gone = client.post(url, data={"decision": "approve", "confirm": "blog.example.com"}, headers=post_headers(s), follow_redirects=False)
    assert "err=" in gone.headers["location"] and len([call for call in deployer.calls if call[1].endswith("/decide")]) == count


def test_asks_are_shown_on_the_app_page_and_the_requests_page_is_gone(admin):
    s, deployer, client = admin
    deployer.pending = [
        {"id": 3, "app": "blog", "kind": "stage", "payload": {"stage": "shared", "people": ["a@example.org"]},
         "note": "for my friend", "created_at": time.time()},
        {"id": 4, "app": "blog", "kind": "rollback", "payload": {"version": "b" * 32, "role": "older", "image": "none (static files)"},
         "note": "", "created_at": time.time()},
        {"id": 5, "app": "blog", "kind": "release", "payload": {"version": "c" * 32}, "note": "", "created_at": time.time()},
    ]
    page = client.get("/apps/blog", headers=owner_headers()).text
    assert 'id="ask-3"' in page and "Share blog.example.com with: a@example.org" in page and "for my friend" in page
    assert 'id="ask-4"' in page and "app data is not rolled back" in page
    assert 'id="ask-5"' not in page                      # an old release request: the pending version is the thing to review
    assert "The AI asks for 2 things" in page and "/requests/3/decide?" in page
    assert 'href="/requests"' not in page
    old = client.get("/requests", headers=owner_headers(), follow_redirects=False)
    assert old.status_code == 303 and old.headers["location"] == "/"
    home = client.get("/", headers=owner_headers()).text
    assert "Waiting on you" in home and 'href="/requests"' not in home


def test_live_operation_log_storage_notices_and_images(admin):
    s, deployer, client = admin
    operation = "c" * 32
    log = client.get(f"/apps/blog/operations/{operation}/log?offset=0", headers=owner_headers())
    assert log.status_code == 200 and log.json()["phase"] == "Building the image" and log.json()["text"] == "step 1\n"
    whole = client.get(f"/apps/blog/operations/{operation}/log.txt", headers=owner_headers())
    assert whole.text == "step 1\n" and whole.headers["content-type"].startswith("text/plain")
    assert "sandbox" in whole.headers["content-security-policy"]     # log text can never run as a page
    assert client.get("/apps/blog/operations/..%2f..%2fx/log", headers=owner_headers()).status_code == 404
    assert client.get("/apps/Bad%20Name/operations/" + operation + "/log", headers=owner_headers()).status_code == 404

    assert client.get("/api/storage?app=blog", headers=owner_headers()).json()["text"]["data_bytes"] == "5 B"
    assert client.post("/api/storage?app=blog", headers=post_headers(s)).status_code == 403   # a change needs the token
    started = client.post("/api/storage?app=blog", headers=post_headers(s, **{"X-CSRF-Token": csrf_token(s, OWNER)}))
    assert started.json()["running"] is True and ("POST", "/v1/storage", OWNER, None) in deployer.calls

    assert client.get("/api/notices", headers=owner_headers()).json()["notices"][0]["text"].startswith("blog: release finished")
    assert client.get("/api/notices?after=9", headers=owner_headers()).json() == {"latest": 9, "notices": []}
    assert client.get("/api/notices?after=x", headers=owner_headers()).status_code == 400

    page = client.get("/images", headers=owner_headers()).text
    assert "old:1" in page and "Images without a name" in page
    removed = client.post(f"/images/cleanup?{csrf_q(s)}", data={"action": "untagged"}, headers=post_headers(s), follow_redirects=False)
    assert removed.status_code == 303 and "msg=Removed" in removed.headers["location"]
    assert deployer.calls[-1][:2] == ("POST", "/v1/images") and deployer.calls[-1][3]["action"] == "untagged"
    refused = client.post(f"/images/cleanup?{csrf_q(s)}", data={"action": "everything"}, headers=post_headers(s), follow_redirects=False)
    assert "err=" in refused.headers["location"]


def test_restore_and_build_cache_actions_reach_the_deployer(admin):
    s, deployer, client = admin
    restored = client.post(f"/apps/blog/do/restore?{csrf_q(s)}", data={"version": "b" * 32}, headers=post_headers(s), follow_redirects=False)
    assert restored.status_code == 303
    assert deployer.calls[-1] == ("POST", "/v1/apps/blog/restore", OWNER, {"version": "b" * 32})
    cleared = client.post(f"/apps/blog/do/build-cache?{csrf_q(s)}", headers=post_headers(s), follow_redirects=False)
    assert "image-builds" in cleared.headers["location"] and deployer.calls[-1][:2] == ("POST", "/v1/apps/blog/build-cache")
    built = client.post(f"/apps/blog/extend/image-build?{csrf_q(s)}", data={"revision": "r1", "version": "b" * 32, "tcp": "443", "no_cache": "on"},
                        headers=post_headers(s), follow_redirects=False)
    assert built.status_code == 303 and deployer.calls[-1][3]["no_cache"] is True


def test_request_summary_comes_from_the_payload():
    item = {"app": "blog", "kind": "stage", "payload": {"stage": "public"}, "note": "just a typo fix"}
    summary = summarize_request(item, "example.com")
    assert summary["risk"] == "high"
    assert "PUBLIC" in summary["text"] and "typo" not in summary["text"]
    assert summary["confirm"] == "blog.example.com"


def test_ai_text_is_escaped(admin):
    s, deployer, client = admin
    deployer.pending = [{"id": 1, "app": "blog", "kind": "start", "payload": {},
                         "note": "<script>alert(1)</script>", "created_at": time.time()}]
    page = client.get("/apps/blog", headers=owner_headers()).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ---------------------------------------------------------------- file manager
@pytest.fixture
def fm(tmp_path) -> FileManager:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "apps").mkdir()
    (root / "apps" / "a.txt").write_text("hello")
    (tmp_path / "secret.txt").write_text("outside")
    os.symlink(tmp_path / "secret.txt", root / "link-out")
    os.symlink(tmp_path, root / "dir-out")
    return FileManager(root, max_upload_bytes=1024)


def test_file_manager_stays_inside(fm):
    for bad in ("../secret.txt", "/../secret.txt", "link-out", "dir-out/secret.txt", "apps/../../secret.txt"):
        with pytest.raises(FileError):
            fm.raw(bad, False)
    assert fm.describe("apps/a.txt")["text"] == "hello"


def test_file_manager_never_writes_through_links(fm):
    with pytest.raises(FileError):
        fm.mkdir("dir-out", "new")
    upload = fm.start_upload("apps", "b.txt", 2)
    fm.append_chunk(upload["upload_id"], 0, b"hi")
    # Deleting a link moves the link itself, not its target.
    fm.delete("link-out")
    assert (fm.root.parent / "secret.txt").read_text() == "outside"
    assert fm.finish_upload(upload["upload_id"], overwrite=False) == "apps/b.txt"


def test_trash_symlink_never_reaches_outside(fm, tmp_path):
    outside = tmp_path / "run-mcp-hosting"
    outside.mkdir()
    (outside / "deployer.sock").write_text("socket")
    os.symlink(outside, fm.root / ".trash")  # planted by the AI
    with pytest.raises(FileError):
        fm.empty_trash()
    with pytest.raises(FileError):
        fm.delete("apps/a.txt")
    assert (outside / "deployer.sock").exists()
    assert (fm.root / "apps" / "a.txt").exists()


def test_upload_metadata_is_checked_again(fm, tmp_path):
    import json as _json

    upload = fm.start_upload("apps", "b.txt", 2)
    fm.append_chunk(upload["upload_id"], 0, b"hi")
    meta = fm.root / ".uploads" / f"{upload['upload_id']}.json"
    info = _json.loads(meta.read_text())
    info["name"] = "../../escaped.txt"  # edited by the AI
    meta.write_text(_json.dumps(info))
    with pytest.raises(FileError):
        fm.finish_upload(upload["upload_id"], overwrite=False)
    assert not (tmp_path / "escaped.txt").exists()


def test_uploads_symlink_is_refused(fm, tmp_path):
    os.symlink(tmp_path, fm.root / ".uploads")
    with pytest.raises(FileError):
        fm.start_upload("apps", "b.txt", 1)


def test_chunked_upload(fm):
    upload = fm.start_upload("apps", "big.bin", 10)
    upload_id = upload["upload_id"]
    assert fm.append_chunk(upload_id, 0, b"01234") == 5
    with pytest.raises(FileError, match="expected offset 5"):
        fm.append_chunk(upload_id, 0, b"01234")
    with pytest.raises(FileError, match="more data"):
        fm.append_chunk(upload_id, 5, b"0123456789")
    with pytest.raises(FileError, match="incomplete"):
        fm.finish_upload(upload_id, overwrite=False)
    fm.append_chunk(upload_id, 5, b"56789")
    assert fm.finish_upload(upload_id, overwrite=False) == "apps/big.bin"
    assert (fm.root / "apps" / "big.bin").read_bytes() == b"0123456789"
    with pytest.raises(FileError):
        fm.start_upload("apps", "huge.bin", 4096)  # above max_upload_bytes
    with pytest.raises(FileError):
        fm.start_upload("apps", "../x", 1)
    with pytest.raises(FileError):
        fm.upload_status("../../etc")


def test_overwrite_rules(fm):
    upload = fm.start_upload("apps", "a.txt", 1)
    fm.append_chunk(upload["upload_id"], 0, b"x")
    with pytest.raises(FileError, match="exists"):
        fm.finish_upload(upload["upload_id"], overwrite=False)
    fm.finish_upload(upload["upload_id"], overwrite=True)
    assert (fm.root / "apps" / "a.txt").read_text() == "x"


def test_delete_goes_to_trash(fm):
    trashed = fm.delete("apps/a.txt")
    assert trashed.startswith(".trash/")
    assert not (fm.root / "apps" / "a.txt").exists()
    assert fm.empty_trash() == 1
    with pytest.raises(FileError):
        fm.delete("")
    with pytest.raises(FileError):
        fm.delete(".trash")


def test_raw_downloads_are_attachments_except_images(admin, tmp_path):
    s, _, client = admin
    (s.workspace / "page.html").write_text("<script>alert(1)</script>")
    (s.workspace / "pic.png").write_bytes(b"\x89PNG\r\n")
    response = client.get("/files/raw", params={"path": "page.html", "inline": "1"}, headers=owner_headers())
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"].startswith("sandbox")
    response = client.get("/files/raw", params={"path": "pic.png", "inline": "1"}, headers=owner_headers())
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline")


def test_exact_source_view_is_owner_only_and_plain_text(admin):
    _,deployer,client=admin
    async def source_call(method,path,actor,json=None,params=None,**kwargs):
        assert path=='/v1/apps/blog/source' and params['version']=='reviewed-revision'
        return {'version':'reviewed-revision','path':'app.py','content':'<script>alert(1)</script>','truncated':False,'offset':0}
    deployer.call=source_call
    url='/apps/blog/source?version=reviewed-revision&path=app.py'
    assert client.get(url).status_code==403
    response=client.get(url,headers=owner_headers())
    assert response.status_code==200 and '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert '<script>alert(1)</script>' not in response.text


def test_release_prerequisites_follow_pending_revision_not_live():
    from mcp_hosting_platform.admin.app import release_checklist
    pending = {'dockerfile': 'Dockerfile', 'secrets': ['SERVICE_TOKEN'], 'capabilities': {'gpu': True}}
    app = {'kind': 'runtime', 'live_version': 'live', 'pending_version': 'next',
           'manifests': {'next': pending}, 'capabilities': {},
           'secrets': [{'name': 'SERVICE_TOKEN', 'has_value': True}],
           'grants': {'live': ['SERVICE_TOKEN'], 'next': []},
           'builds': {'live': {'built': {'image': 'sha256:old'}}, 'next': {'built': None}}}
    checks = {check['target']: check['done'] for check in release_checklist(app)}
    assert checks == {'image-builds': False, 'capabilities': False, 'credentials': True, 'credential-grants': False}
    app['builds']['next']['built'] = {'image': 'sha256:new'}
    app['capabilities'] = {'gpu': True}
    app['grants']['next'] = ['SERVICE_TOKEN']
    assert all(check['done'] for check in release_checklist(app))
    app['secrets'][0]['has_value'] = False
    assert not next(check['done'] for check in release_checklist(app) if check['target'] == 'credentials')


def test_simple_app_has_no_unrelated_release_prerequisites():
    from mcp_hosting_platform.admin.app import release_checklist
    assert release_checklist({'kind': 'static', 'pending_version': 'next'}) == []
    checks = release_checklist({'kind': 'runtime', 'pending_version': 'next', 'manifests': {'next': {}}})
    assert checks == []
    checks = release_checklist({'kind': 'runtime', 'pending_version': 'next',
                                'manifests': {'next': {'image': 'sha256:approved'}}})
    assert all(check['done'] for check in checks)


# ---------------------------------------------------------------- deployer client
def test_deployer_client_keeps_slow_calls_alive_while_fast_calls_finish(tmp_path):
    """A finished call must not close the connections of calls still waiting
    (the cause of "the deployer is not reachable (ReadError)")."""
    import asyncio
    import threading

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from mcp_hosting_platform.admin.deployer_client import DeployerClient

    async def slow(request):
        await asyncio.sleep(0.6)
        return JSONResponse({"slow": True, "actor": request.headers.get("x-actor")})

    async def fast(request):
        return JSONResponse({"fast": True})

    socket_path = str(tmp_path / "deployer.sock")
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/slow", slow), Route("/fast", fast)]),
                                           uds=socket_path, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started

    async def scenario():
        client = DeployerClient(socket_path)
        try:
            pending = asyncio.create_task(client.call("GET", "/slow", OWNER))
            await asyncio.sleep(0.1)
            for _ in range(5):  # each of these used to close the shared pool
                assert await client.call("GET", "/fast", OWNER) == {"fast": True}
            return await pending
        finally:
            await client.close()

    try:
        assert asyncio.run(scenario()) == {"slow": True, "actor": OWNER}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_deployer_client_reports_unreachable_and_never_repeats_a_change(tmp_path):
    import asyncio

    from mcp_hosting_platform.admin.deployer_client import DeployerClient

    async def scenario():
        client = DeployerClient(str(tmp_path / "missing.sock"))
        try:
            with pytest.raises(DeployerError, match="not reachable"):
                await client.call("GET", "/v1/status", OWNER)
            with pytest.raises(DeployerError, match="not reachable"):
                await client.call("POST", "/v1/apps/x/start", OWNER)
        finally:
            await client.close()

    asyncio.run(scenario())
