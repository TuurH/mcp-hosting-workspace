from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hosting_platform.deployer import pomerium
from mcp_hosting_platform.deployer.api import build_full_app, build_narrow_app
from mcp_hosting_platform.deployer.core import PlatformError, RouteError
from mcp_hosting_platform.deployer.db import Database, recover_from_snapshot
from platform_helpers import API_TOKEN, OWNER, make_platform, site_archive


def deploy(platform, tmp_path: Path, name: str, html: str, **kwargs):
    archive = site_archive({"index.html": html}, tmp_path, name=f"{name}-{abs(hash(html))}.tar.gz")
    return platform.deploy_version(name, archive, **kwargs)


def routes_by_host(platform) -> dict[str, dict]:
    config = json.loads(platform.settings.pomerium_config.read_text())
    return {route["from"].removeprefix("https://"): route for route in config["routes"]}


def legacy_request(platform, app, kind, payload):
    """A request as an older build filed it (those kinds can no longer be filed)."""
    import json as _json
    with platform.db.tx() as conn:
        return conn.execute("INSERT INTO requests (app, kind, payload, note, created_at) VALUES (?, ?, ?, '', 1)",
                            (app, kind, _json.dumps(payload))).lastrowid


# ---------------------------------------------------------------- deploy flow
def test_generated_pomerium_config_disables_product_analytics(tmp_path):
    platform, _ = make_platform(tmp_path)
    config, _ = pomerium.render(platform.settings, platform.db)
    assert config['runtime_flags']['product_analytics'] is False
    assert config['runtime_flags']['pomerium_jwt_endpoint'] is False


def test_new_app_starts_in_dev_and_is_owner_only(tmp_path):
    platform, _ = make_platform(tmp_path)
    result = deploy(platform, tmp_path, "blog", "<h1>v1</h1>")
    assert result["outcome"] == "live"
    assert result["stage"] == "dev"
    assert result["url"] == "https://blog.example.com"

    route = routes_by_host(platform)["blog.example.com"]
    assert "allow_public_unauthenticated_access" not in route
    assert route["policy"] == [{"allow": {"or": [{"email": {"is": OWNER}}]}}]
    assert route["preserve_host_header"] is True
    assert "X-Pomerium-Claim-Email" in route["remove_request_headers"]
    assert route["set_response_headers"]["Cache-Control"] == "private, no-store"

    served = platform.settings.sites_dir / "blog.example.com" / "current" / "index.html"
    assert served.read_text() == "<h1>v1</h1>"
    # The canary host is always there and owner-only.
    assert "canary.example.com" in routes_by_host(platform)


def test_sites_are_world_readable_whatever_the_umask(tmp_path):
    """The static server runs without capabilities: it needs o+r files and o+x folders."""
    platform, _ = make_platform(tmp_path)
    previous = os.umask(0o077)
    try:
        deploy(platform, tmp_path, "blog", "<p>x</p>")
    finally:
        os.umask(previous)
    sites = platform.settings.sites_dir
    served = sites / "blog.example.com" / "current"
    for path in (sites, sites / "_versions", sites / "_versions" / "blog", served.resolve(),
                 sites / "blog.example.com"):
        assert path.stat().st_mode & 0o005 == 0o005, path
    assert (served / "index.html").stat().st_mode & 0o004


def test_dev_redeploy_goes_live_and_keeps_previous(tmp_path):
    platform, _ = make_platform(tmp_path)
    first = deploy(platform, tmp_path, "blog", "v1")
    second = deploy(platform, tmp_path, "blog", "v2")
    assert second["outcome"] == "live"
    assert second["previous_version"] == first["version"]
    assert deploy(platform, tmp_path, "blog", "v2")["outcome"] == "unchanged"
    rolled = platform.rollback("blog", OWNER)
    assert rolled["live_version"] == first["version"]


def test_shared_app_update_needs_release(tmp_path):
    platform, _ = make_platform(tmp_path)
    platform.add_person("friend@mcp-hosting.test", "Friend", OWNER)
    first = deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "shared", ["friend@mcp-hosting.test"], [], OWNER)

    second = deploy(platform, tmp_path, "blog", "v2")
    assert second["outcome"] == "pending"
    assert second["live_version"] == first["version"]
    assert second["preview_url"] == "https://next-blog.example.com"
    routes = routes_by_host(platform)
    assert routes["next-blog.example.com"]["policy"] == [{"allow": {"or": [{"email": {"is": OWNER}}]}}]
    emails = {rule["email"]["is"] for rule in routes["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "friend@mcp-hosting.test"}

    # The pending version itself waits on the app page: no separate request is filed.
    assert platform.db.requests(status="pending", app="blog") == []
    assert second["approval_url"] == "https://admin.example.com/apps/blog#versions"
    platform.release("blog", OWNER, expected_version=second["version"])
    app = platform.db.app("blog")
    assert app["live_version"] == second["version"]
    assert app["pending_version"] is None
    assert "next-blog.example.com" not in routes_by_host(platform)


def test_release_is_bound_to_the_reviewed_version(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    reviewed = deploy(platform, tmp_path, "blog", "v2")["version"]
    newer = deploy(platform, tmp_path, "blog", "v3")["version"]  # arrives while the owner looks at v2
    with pytest.raises(PlatformError, match="newer version"):
        platform.release("blog", OWNER, expected_version=reviewed)
    assert platform.release("blog", OWNER, expected_version=newer)["live_version"] == newer


def test_stage_change_is_bound_to_the_live_version_shown(tmp_path):
    platform, _ = make_platform(tmp_path)
    shown = deploy(platform, tmp_path, "blog", "v1")["version"]
    deploy(platform, tmp_path, "blog", "v2")  # a Dev app goes live at once
    with pytest.raises(PlatformError, match="live version changed"):
        platform.set_stage("blog", "public", [], [], OWNER, expected_live_version=shown)
    assert platform.db.app("blog")["stage"] == "dev"
    live = platform.db.app("blog")["live_version"]
    assert platform.set_stage("blog", "public", [], [], OWNER, expected_live_version=live)["stage"] == "public"


def test_rollback_is_bound_to_the_version_shown(tmp_path):
    platform, _ = make_platform(tmp_path)
    first = deploy(platform, tmp_path, "blog", "v1")["version"]
    second = deploy(platform, tmp_path, "blog", "v2")["version"]
    deploy(platform, tmp_path, "blog", "v3")  # previous is now v2, not v1
    with pytest.raises(PlatformError, match="versions changed"):
        platform.rollback("blog", OWNER, expected_version=first)
    assert platform.rollback("blog", OWNER, expected_version=second)["live_version"] == second


def test_old_release_requests_without_a_version_are_cancelled(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    deploy(platform, tmp_path, "blog", "v2")
    request = {"id": legacy_request(platform, "blog", "release", {})}
    with pytest.raises(PlatformError, match="does not say which version"):
        platform.decide_request(request["id"], True, OWNER)
    assert platform.db.request(request["id"])["status"] == "superseded"
    assert platform.db.app("blog")["pending_version"]  # still there to release by hand


def test_ai_cannot_file_release_requests_or_junk(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with pytest.raises(PlatformError):
        platform.file_request("blog", "release", {"version": "safe, already reviewed"}, "")
    for payload in ({"stage": "shared", "people": "a@mcp-hosting.test"}, {"stage": "shared", "people": {"a": 1}},
                    {"stage": "shared", "groups": "family"}, {"stage": "shared", "people": [1]}):
        with pytest.raises(PlatformError):
            platform.file_request("blog", "stage", payload, "")
    empty = platform.file_request("blog", "stage", {"stage": "shared", "people": ""}, "")
    live = platform.db.app("blog")["live_version"]
    assert platform.db.request(empty["id"])["payload"] == {"stage": "shared", "people": [], "groups": [],
                                                           "live_version": live, "revision": platform.revision("blog")}
    request = platform.file_request("blog", "log_access", {"mode": "full", "extra": "<b>x</b>"}, "")
    assert platform.db.request(request["id"])["payload"] == {"mode": "full", "revision": platform.revision("blog")}


def test_newer_upload_supersedes_waiting_release(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    second = deploy(platform, tmp_path, "blog", "v2")
    first_request = {"id": legacy_request(platform, "blog", "release", {"version": second["version"]})}
    deploy(platform, tmp_path, "blog", "v3")
    assert platform.db.request(first_request["id"])["status"] == "superseded"
    with pytest.raises(PlatformError):
        platform.decide_request(first_request["id"], True, OWNER)


def test_auto_release_is_refused_for_public_apps(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    with pytest.raises(PlatformError):
        platform.update_app_settings("blog", OWNER, auto_release=True)


def test_public_route_and_offline(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "weather", "sunny")
    platform.set_stage("weather", "public", [], [], OWNER)
    route = routes_by_host(platform)["weather.example.com"]
    assert route["allow_public_unauthenticated_access"] is True
    assert "policy" not in route
    platform.set_online("weather", False, OWNER)
    assert "weather.example.com" not in routes_by_host(platform)
    assert not (platform.settings.sites_dir / "weather.example.com").exists()


def test_group_members_get_access(tmp_path):
    platform, _ = make_platform(tmp_path)
    platform.add_person("a@mcp-hosting.test", "", OWNER)
    platform.add_group("family", OWNER)
    deploy(platform, tmp_path, "photos", "x")
    platform.set_stage("photos", "shared", [], ["family"], OWNER)
    platform.set_group_member("family", "a@mcp-hosting.test", True, OWNER)
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["photos.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "a@mcp-hosting.test"}
    platform.remove_person("a@mcp-hosting.test", OWNER)
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["photos.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER}


def test_stage_requires_known_people(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with pytest.raises(PlatformError, match="people list"):
        platform.set_stage("blog", "shared", ["stranger@evil.test"], [], OWNER)
    with pytest.raises(PlatformError, match="people list"):
        platform.file_request("blog", "stage", {"stage": "shared", "people": ["stranger@evil.test"]}, "")


def test_kill_switch_makes_everything_private(tmp_path):
    platform, _ = make_platform(tmp_path)
    for name in ("one", "two"):
        deploy(platform, tmp_path, name, name)
        platform.set_stage(name, "public", [], [], OWNER)
    assert platform.kill_switch(OWNER) == 2
    for host in ("one.example.com", "two.example.com"):
        assert "allow_public_unauthenticated_access" not in routes_by_host(platform)[host]


def test_names_and_quotas(tmp_path):
    platform, _ = make_platform(tmp_path)
    for bad in ("admin", "auth", "next-blog", "logim", "a", "UPPER", "x--y"):
        with pytest.raises(PlatformError):
            deploy(platform, tmp_path, bad, "x")
    platform.db.set_setting("quotas", {"max_dev_apps": 1})
    deploy(platform, tmp_path, "first", "x")
    with pytest.raises(PlatformError, match="Dev app limit"):
        deploy(platform, tmp_path, "second", "x")


def test_broken_archives_are_refused(tmp_path):
    platform, _ = make_platform(tmp_path)
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    with pytest.raises(ValueError):
        platform.deploy_version("blog", bad)
    assert platform.db.app("blog") is None
    assert list((platform.settings.sites_dir / "_staging").iterdir()) == []


def test_old_versions_are_pruned(tmp_path):
    platform, _ = make_platform(tmp_path)
    for n in range(5):
        deploy(platform, tmp_path, "blog", f"v{n}")
    versions = os.listdir(platform.settings.sites_dir / "_versions" / "blog")
    assert len(versions) == 5  # the newest ten revisions keep their source
    for n in range(5, 13):
        deploy(platform, tmp_path, "blog", f"v{n}")
    app = platform.db.app("blog")
    versions = set(os.listdir(platform.settings.sites_dir / "_versions" / "blog"))
    assert len(versions) == 10 and {app["live_version"], app["previous_version"]} <= versions
    assert len(platform.db.versions("blog")) == 10
    platform.db.set_setting("quotas", {**platform.db.get_setting("quotas"), "kept_revisions": 0})
    deploy(platform, tmp_path, "blog", "last")
    assert len(os.listdir(platform.settings.sites_dir / "_versions" / "blog")) == 2  # live and previous


def test_remove_app(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_secret("blog", "TOKEN", "s3cret", OWNER)
    platform.remove_app("blog", OWNER)
    assert platform.db.app("blog") is None
    assert platform.secret_names("blog") == []
    assert "blog.example.com" not in routes_by_host(platform)
    assert not (platform.settings.sites_dir / "_versions" / "blog").exists()


def test_remove_request_is_recorded_as_approved(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    # "remove" asks can no longer be filed; one from an older build can still be decided.
    with pytest.raises(PlatformError, match="request kind"):
        platform.file_request("blog", "remove", {}, "")
    with pytest.raises(PlatformError, match="request kind"):
        platform.file_request("blog", "stop", {}, "")
    request = {"id": legacy_request(platform, "blog", "remove", {"revision": platform.revision("blog")})}
    result = platform.decide_request(request["id"], True, OWNER)
    assert result["status"] == "approved"
    assert platform.db.app("blog") is None


# ---------------------------------------------------------------- secrets and requests
def test_secrets_are_encrypted_and_never_listed(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "bot", "x")
    request = platform.file_request("bot", "secret", {"name": "DISCORD_TOKEN"}, "for the bot")
    with pytest.raises(PlatformError, match="enter the secret"):
        platform.decide_request(request["id"], True, OWNER)
    platform.decide_request(request["id"], True, OWNER, value="abc123")
    raw = platform.db.query_one("SELECT ciphertext FROM secrets WHERE app = 'bot'")["ciphertext"]
    assert b"abc123" not in raw
    assert platform.secret_value("bot", "DISCORD_TOKEN") == "abc123"
    assert platform.secret_names("bot")[0]["name"] == "DISCORD_TOKEN"
    assert "abc123" not in json.dumps(platform.secret_names("bot"))


def test_request_validation_and_limit(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with pytest.raises(PlatformError):
        platform.file_request("blog", "docker", {}, "")
    with pytest.raises(PlatformError):
        platform.file_request("blog", "secret", {"name": "lower"}, "")
    with pytest.raises(PlatformError):
        platform.file_request("blog", "log_access", {"mode": "everything"}, "")
    for _ in range(10):
        platform.file_request("blog", "log_access", {"mode": "full"}, "")
    with pytest.raises(PlatformError, match="10 pending"):
        platform.file_request("blog", "log_access", {"mode": "full"}, "")


def test_ai_log_view_hides_visitors_from_before_dev(tmp_path):
    from mcp_hosting_platform.deployer.api import _logs

    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    log = platform.settings.static_log
    log.parent.mkdir(parents=True, exist_ok=True)
    old = {"ts": time.time() - 3600, "status": 200, "size": 1, "duration": 0.001,
           "request": {"host": "blog.example.com", "method": "GET", "uri": "/?me=alice@example.org",
                       "client_ip": "203.0.113.7", "headers": {}}}
    log.write_text(json.dumps(old) + "\n")
    platform.kill_switch(OWNER)  # back to Dev, where the AI's default is "full"
    entries = _logs(platform, "blog", 100, owner=False)["entries"]
    assert entries[0]["client_ip"] == "203.0.113.x"
    assert "alice" not in json.dumps(entries)
    assert _logs(platform, "blog", 100, owner=True)["entries"][0]["client_ip"] == "203.0.113.7"


def test_log_nicknames_use_a_salt_the_ai_does_not_have(tmp_path):
    platform, _ = make_platform(tmp_path)
    salt = platform.log_salt()
    assert salt == platform.log_salt() and len(salt) == 64
    assert not salt.startswith(platform.settings.api_token[:8])


def test_full_log_access_expires(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    platform.update_app_settings("blog", OWNER, log_access="full")
    assert platform.effective_log_access(platform.db.app("blog")) == "full"
    with platform.db.tx() as conn:
        conn.execute("UPDATE apps SET log_access_until = 1 WHERE name = 'blog'")
    assert platform.effective_log_access(platform.db.app("blog")) == "masked"


# ---------------------------------------------------------------- pomerium config
def failing_probe(*args, **kwargs):
    return ["blog.example.com: expected a login redirect, got 200"], True


def test_failed_widening_is_undone_in_db_and_config(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    before = json.loads(platform.settings.pomerium_config.read_text())
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    with pytest.raises(RouteError):
        platform.set_stage("blog", "public", [], [], OWNER)
    assert platform.db.app("blog")["stage"] == "dev"
    assert json.loads(platform.settings.pomerium_config.read_text()) == before
    # The reconcile loop sees nothing to change and so does not make it public later.
    platform.apply_routes("reconcile")
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]


def test_failed_stage_approval_stays_pending(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    request = platform.file_request("blog", "stage", {"stage": "public"}, "")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    with pytest.raises(RouteError):
        platform.decide_request(request["id"], True, OWNER)
    assert platform.db.request(request["id"])["status"] == "pending"
    assert platform.db.app("blog")["stage"] == "dev"


def test_narrowing_is_never_undone(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for name in ("one", "two"):
        deploy(platform, tmp_path, name, name)
        platform.set_stage(name, "public", [], [], OWNER)
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    assert platform.kill_switch(OWNER) == 2  # no exception, nothing restored
    routes = routes_by_host(platform)
    assert all("allow_public_unauthenticated_access" not in routes[h] for h in ("one.example.com", "two.example.com"))
    platform.set_online("one", False, OWNER)
    assert "one.example.com" not in routes_by_host(platform)
    assert platform.route_problems


def test_people_who_leave_stay_removed_when_adding_others_fails(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for email in ("a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"):
        platform.add_person(email, email[0], OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test", "b@mcp-hosting.test"], [], OWNER)
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    with pytest.raises(RouteError) as caught:
        platform.set_stage("blog", "shared", ["b@mcp-hosting.test", "c@mcp-hosting.test"], [], OWNER)
    assert caught.value.undone
    assert platform.db.app("blog")["people"] == ["b@mcp-hosting.test"]
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "b@mcp-hosting.test"}


def test_reconcile_keeps_the_narrower_routes_when_the_check_fails(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    before = json.loads(platform.settings.pomerium_config.read_text())
    with platform.db.tx() as conn:  # e.g. a restored database that says Public
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    calls = []
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: calls.append(1) or failing_probe())
    platform.apply_routes("reconcile")
    assert json.loads(platform.settings.pomerium_config.read_text()) == before
    assert "out of sync" in platform.route_problems[0]
    platform.apply_routes("reconcile")  # a minute later: not tried again yet
    assert len(calls) == 1
    assert json.loads(platform.settings.pomerium_config.read_text()) == before


def test_failed_checks_stay_visible_until_a_check_passes(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for name in ("one", "two"):
        deploy(platform, tmp_path, name, name)
        platform.set_stage(name, "public", [], [], OWNER)
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.kill_switch(OWNER)
    assert platform.route_problems
    platform.apply_routes("reconcile")  # nothing changed: no new check
    assert platform.route_problems
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: ([], True))
    platform.apply_routes("reconcile")
    assert platform.route_problems  # checked again only after 10 minutes
    platform._last_probe -= 601
    platform.apply_routes("reconcile")
    assert platform.route_problems == []


def test_narrowing_stays_when_writing_the_routes_fails(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    real_write = pomerium.write_config

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    with pytest.raises(RouteError) as caught:
        platform.set_online("blog", False, OWNER)
    assert not caught.value.undone and "No space" in str(caught.value)
    assert not platform.db.app("blog")["online"]  # kept in the database
    assert platform.route_problems
    monkeypatch.setattr(pomerium, "write_config", real_write)
    platform.apply_routes("reconcile")  # the disk has room again
    assert "blog.example.com" not in routes_by_host(platform)


def test_kill_switch_with_a_full_disk_is_kept_and_shown(tmp_path, monkeypatch):
    platform, gaming = make_platform(tmp_path)
    for name in ("one", "two"):
        deploy(platform, tmp_path, name, name)
        platform.set_stage(name, "public", [], [], OWNER)
    real_write = pomerium.write_config

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    client = TestClient(build_full_app(platform, gaming), base_url="http://deployer")
    response = client.post("/v1/killswitch", json={})
    assert response.status_code == 502 and "kept" in response.json()["error"]
    assert platform.db.get_setting("access_lockdown") is True
    assert all(app["stage"] == "public" and app["protected"] for app in platform.db.apps())
    assert "No space" in platform.route_problems[0]
    with pytest.raises(RouteError):
        platform.apply_routes("reconcile")  # still full
    assert platform.route_problems
    monkeypatch.setattr(pomerium, "write_config", real_write)
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: (["one.example.com: still public"], True))
    platform.apply_routes("reconcile")
    assert platform.route_problems == ["one.example.com: still public"]  # cleared only by a passing check
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: ([], True))
    platform._last_probe -= 601
    platform.apply_routes("reconcile")
    assert platform.route_problems == []
    routes = routes_by_host(platform)
    assert all("allow_public_unauthenticated_access" not in routes[h] for h in ("one.example.com", "two.example.com"))


def test_a_recovered_write_does_not_hide_a_failed_check(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: (["blog.example.com: expected the site, got 403"], True))
    deploy(platform, tmp_path, "yapp", "y")  # a check fails and is shown
    assert platform.route_problems == ["blog.example.com: expected the site, got 403"]
    real_write = pomerium.write_config

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)  # the AI fills the disk
    with pytest.raises(RouteError):
        platform.set_online("yapp", False, "ai")  # the AI stops its app: kept, not written
    assert "No space" in platform.route_problems[0]
    monkeypatch.setattr(pomerium, "write_config", real_write)  # and frees it
    platform.ai_start("yapp")  # back to what is on disk: nothing to write, but checked
    assert platform.route_problems == ["blog.example.com: expected the site, got 403"]  # still shown


def test_a_failed_restore_of_held_back_routes_is_retried_first(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    before = json.loads(platform.settings.pomerium_config.read_text())
    with platform.db.tx() as conn:  # e.g. a restored database that says Public
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    real_write = pomerium.write_config
    writes = []

    def fill_disk_during_probe(path, *args, **kwargs):
        if path == platform.settings.pomerium_config:  # the live file (not the checked copy)
            writes.append(1)
            if len(writes) > 1:  # the widened write works, the restore after the probe does not
                raise OSError(28, "No space left on device")
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(pomerium, "write_config", fill_disk_during_probe)
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    with pytest.raises(RouteError):
        platform.apply_routes("reconcile")
    with pytest.raises(RouteError):
        platform.apply_routes("reconcile")  # still full: restore retried first
    monkeypatch.setattr(pomerium, "write_config", real_write)
    platform.apply_routes("reconcile")
    assert json.loads(platform.settings.pomerium_config.read_text()) == before
    assert "out of sync" in platform.route_problems[0]


class _AuditDiskFull:
    """A connection on which audit rows fail as on a full disk."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.lstrip().startswith("INSERT INTO audit"):
            import sqlite3
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_an_audit_row_that_cannot_be_written_never_stops_the_action(tmp_path):
    platform, _ = make_platform(tmp_path)
    platform.db._conn = _AuditDiskFull(platform.db._conn)
    platform.db.audit(OWNER, "test", {})  # logged instead, no exception
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    assert platform.kill_switch(OWNER) == 1


def test_held_back_restore_survives_a_full_disk(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with platform.db.tx() as conn:  # e.g. a restored database that says Public
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    platform.db._conn = _AuditDiskFull(platform.db._conn)
    real_write = pomerium.write_config
    writes = []

    def fill_disk_during_probe(path, *args, **kwargs):
        if path == platform.settings.pomerium_config:  # the live file (not the checked copy)
            writes.append(1)
            if len(writes) > 1:
                raise OSError(28, "No space left on device")
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(pomerium, "write_config", fill_disk_during_probe)
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    with pytest.raises(RouteError):
        platform.apply_routes("reconcile")
    assert platform._pending_restore is not None
    monkeypatch.setattr(pomerium, "write_config", real_write)
    platform.apply_routes("reconcile")
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]


def test_holding_back_keeps_narrowings_of_other_apps(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "shop", "s")
    platform.set_stage("shop", "public", [], [], OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    with platform.db.tx() as conn:  # a database wider than Pomerium for blog
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    real_write = pomerium.write_config

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    with pytest.raises(RouteError):
        platform.set_stage("shop", "dev", [], [], OWNER)  # kept in the database, not written
    monkeypatch.setattr(pomerium, "write_config", real_write)
    monkeypatch.setattr(pomerium, "probe", failing_probe)  # blog fails its check
    platform.apply_routes("reconcile")
    routes = routes_by_host(platform)
    assert "allow_public_unauthenticated_access" not in routes["shop.example.com"]  # the narrowing stays
    assert "allow_public_unauthenticated_access" not in routes["blog.example.com"]  # held back


def test_holding_back_applies_to_every_route_write(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with platform.db.tx() as conn:  # a database wider than Pomerium
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]
    deploy(platform, tmp_path, "toy", "t")  # the AI deploys: written, but blog stays held back
    platform.set_online("toy", False, "ai")
    routes = routes_by_host(platform)
    assert "allow_public_unauthenticated_access" not in routes["blog.example.com"]
    assert "out of sync" in platform.route_problems[0]
    platform._held = {host: (0.0, why) for host, (_, why) in platform._held.items()}  # ten minutes later
    platform.apply_routes("reconcile")  # tried again, fails again, held back again
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]


def test_holding_back_a_host_keeps_removals_on_that_host(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for email in ("a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"):
        platform.add_person(email, email[0], OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test", "b@mcp-hosting.test"], [], OWNER)
    with platform.db.tx() as conn:  # the database also admits c, b was removed meanwhile
        conn.execute("DELETE FROM app_access WHERE principal = 'b@mcp-hosting.test'")
        conn.execute("INSERT INTO app_access (app, principal_type, principal) VALUES ('blog', 'person', 'c@mcp-hosting.test')")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "a@mcp-hosting.test"}  # b removed, c held back


def test_a_restart_during_a_check_falls_back_to_the_checked_routes(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")  # owner-only, recorded as checked
    with platform.db.tx() as conn:  # e.g. a restored database that says Public
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    wider, _ = pomerium.render(platform.settings, platform.db)
    pomerium.write_config(platform.settings.pomerium_config, wider)  # written, then the deployer stopped mid-check
    restarted, _ = make_platform(tmp_path)
    restarted.apply_routes("startup", probe=False)
    assert "allow_public_unauthenticated_access" not in routes_by_host(restarted)["blog.example.com"]
    assert "waiting for a routing check" in restarted.route_problems[0]


def test_an_owner_narrowing_of_a_held_host_applies_at_once(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    platform.add_person("a@mcp-hosting.test", "A", OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    with platform.db.tx() as conn:  # a database wider than Pomerium
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")  # held back
    assert "blog.example.com" in platform._held
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: ([], True))
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test"], [], OWNER)  # less than Public: checked now
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "a@mcp-hosting.test"}
    assert "blog.example.com" not in platform._held and platform.route_problems == []


def test_without_widening_keeps_only_the_narrower_route_per_host():
    def route(host, public=False, emails=()):
        if public:
            return {"from": f"https://{host}", "allow_public_unauthenticated_access": True}
        return {"from": f"https://{host}", "policy": [{"allow": {"or": [{"email": {"is": e}} for e in emails]}}]}
    old = {"routes": [route("a.x", public=True), route("b.x", emails=[OWNER])]}
    new = {"routes": [route("a.x", emails=[OWNER]), route("b.x", public=True), route("c.x", public=True),
                      route("d.x", emails=[OWNER])], "address": ":80"}
    merged = pomerium.without_widening(new, old, OWNER)
    access = pomerium.access_map(merged)
    assert access == {"a.x": frozenset({OWNER}), "b.x": frozenset({OWNER}), "d.x": frozenset({OWNER})}
    assert merged["address"] == ":80"


def test_people_edit_whose_additions_fail_stays_pending(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for email in ("a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"):
        platform.add_person(email, email[0], OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test", "b@mcp-hosting.test"], [], OWNER)
    request = platform.file_request("blog", "stage", {"stage": "shared", "people": ["a@mcp-hosting.test", "c@mcp-hosting.test"]}, "")

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    with pytest.raises(RouteError) as caught:
        platform.decide_request(request["id"], True, OWNER)
    assert caught.value.undone  # c was not added
    assert platform.db.app("blog")["people"] == ["a@mcp-hosting.test"]  # b's removal is kept
    assert platform.db.request(request["id"])["status"] == "pending"


def test_approved_request_kept_despite_a_failed_write_is_recorded(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    request = platform.file_request("blog", "stage", {"stage": "dev"}, "")

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    with pytest.raises(RouteError) as caught:
        platform.decide_request(request["id"], True, OWNER)
    assert not caught.value.undone
    assert platform.db.app("blog")["stage"] == "dev"
    assert platform.db.request(request["id"])["status"] == "approved"
    with pytest.raises(PlatformError, match="no such pending request"):
        platform.decide_request(request["id"], False, OWNER)


def test_widening_is_undone_when_writing_the_routes_fails(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pomerium, "write_config", full_disk)
    with pytest.raises(RouteError) as caught:
        platform.set_stage("blog", "public", [], [], OWNER)
    assert caught.value.undone
    assert platform.db.app("blog")["stage"] == "dev"


def test_old_stage_requests_without_a_version_are_cancelled(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    request = platform.file_request("blog", "stage", {"stage": "public"}, "")
    with platform.db.tx() as conn:  # as the first build filed it
        conn.execute("UPDATE requests SET payload = ? WHERE id = ?", (json.dumps({"stage": "public"}), request["id"]))
    deploy(platform, tmp_path, "blog", "v2")  # goes live at once (Dev)
    with pytest.raises(PlatformError, match="does not say which version"):
        platform.decide_request(request["id"], True, OWNER)
    assert platform.db.request(request["id"])["status"] == "superseded"
    assert platform.db.app("blog")["stage"] == "dev"


def test_widening_detection():
    owner = OWNER
    private = {"routes": [{"from": "https://a.example.com", "policy": [{"allow": {"or": [{"email": {"is": owner}}]}}]}]}
    shared = {"routes": [{"from": "https://a.example.com",
                          "policy": [{"allow": {"or": [{"email": {"is": owner}}, {"email": {"is": "f@x.be"}}]}}]}]}
    public = {"routes": [{"from": "https://a.example.com", "allow_public_unauthenticated_access": True}]}
    assert pomerium.is_widening(private, shared, owner)
    assert pomerium.is_widening(shared, public, owner)
    assert pomerium.is_widening(None, public, owner)
    assert not pomerium.is_widening(public, shared, owner)
    assert not pomerium.is_widening(shared, private, owner)
    assert not pomerium.is_widening(None, private, owner)  # a new owner-only host


def test_missing_version_files_do_not_block_other_routes(tmp_path):
    import shutil

    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "broken", "x")
    deploy(platform, tmp_path, "fine", "y")
    for name in ("broken", "fine"):
        platform.set_stage(name, "public", [], [], OWNER)
    version = platform.db.app("broken")["live_version"]
    shutil.rmtree(platform.settings.sites_dir / "_versions" / "broken" / version)
    assert platform.kill_switch(OWNER) == 2
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["fine.example.com"]
    assert "missing" in platform.describe(platform.db.app("broken"))["problem"]
    platform.repair_versions()
    assert platform.db.app("broken")["live_version"] is None


def test_release_refuses_a_version_without_files(tmp_path):
    import shutil

    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "public", [], [], OWNER)
    pending = deploy(platform, tmp_path, "blog", "v2")["version"]
    shutil.rmtree(platform.settings.sites_dir / "_versions" / "blog" / pending)
    with pytest.raises(PlatformError, match="missing"):
        platform.release("blog", OWNER, expected_version=pending)
    platform.repair_versions()
    assert platform.db.app("blog")["pending_version"] is None
    assert platform.db.requests(status="pending", app="blog") == []


def test_stage_approval_is_bound_to_the_live_version(tmp_path):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "harmless")
    request = platform.file_request("blog", "stage", {"stage": "public"}, "looks fine")
    deploy(platform, tmp_path, "blog", "something else")  # Dev: goes live at once
    with pytest.raises(PlatformError, match="changed after this request"):
        platform.decide_request(request["id"], True, OWNER)
    assert platform.db.request(request["id"])["status"] == "superseded"
    assert platform.db.app("blog")["stage"] == "dev"


def test_unreachable_pomerium_widens_nothing_until_it_can_check(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: (["pomerium: connection refused"], False))
    platform.set_stage("blog", "public", [], [], OWNER)  # kept in the database...
    assert platform.db.app("blog")["stage"] == "public"
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]  # ...not live yet
    assert "waiting for a routing check" in platform.route_problems[0]
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: ([], True))  # Pomerium is up
    platform.apply_routes("reconcile")
    assert routes_by_host(platform)["blog.example.com"]["allow_public_unauthenticated_access"] is True
    assert platform.route_problems == []


def test_unchecked_writes_never_widen(tmp_path, monkeypatch):
    # Startup (and the undo of a failed change) write without a check.
    platform, _ = make_platform(tmp_path)
    deploy(platform, tmp_path, "blog", "v1")
    with platform.db.tx() as conn:  # e.g. a restored database that says Public
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    platform.apply_routes("startup", probe=False)
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")  # the first check fails: held back
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]
    assert "out of sync" in platform.route_problems[0]


def test_a_failed_owner_widening_keeps_other_hosts_held(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    platform.add_person("a@mcp-hosting.test", "A", OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    deploy(platform, tmp_path, "shop", "s")
    with platform.db.tx() as conn:
        conn.execute("UPDATE apps SET stage = 'public' WHERE name = 'blog'")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")
    platform._held = {host: (0.0, why) for host, (_, why) in platform._held.items()}  # the hold expires
    with pytest.raises(RouteError):
        platform.set_stage("shop", "shared", ["a@mcp-hosting.test"], [], OWNER)  # fails, is undone
    assert platform.db.app("shop")["stage"] == "dev"
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]
    platform._last_probe -= 601
    platform.apply_routes("reconcile")
    assert "allow_public_unauthenticated_access" not in routes_by_host(platform)["blog.example.com"]


def test_an_owner_change_to_a_held_host_is_checked_now(tmp_path, monkeypatch):
    platform, _ = make_platform(tmp_path)
    for email in ("a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"):
        platform.add_person(email, email[0], OWNER)
    deploy(platform, tmp_path, "blog", "v1")
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test"], [], OWNER)
    with platform.db.tx() as conn:  # the database also admits c
        conn.execute("INSERT INTO app_access (app, principal_type, principal) VALUES ('blog', 'person', 'c@mcp-hosting.test')")
    monkeypatch.setattr(pomerium, "probe", failing_probe)
    platform.apply_routes("reconcile")  # c held back
    with pytest.raises(RouteError) as caught:
        platform.set_stage("blog", "shared", ["a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"], [], OWNER)
    assert caught.value.undone
    monkeypatch.setattr(pomerium, "probe", lambda *a, **k: ([], True))
    platform.set_stage("blog", "shared", ["a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"], [], OWNER)
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert emails == {OWNER, "a@mcp-hosting.test", "b@mcp-hosting.test", "c@mcp-hosting.test"}
    platform.apply_routes("reconcile")  # stays: the host is no longer held
    emails = {rule["email"]["is"] for rule in routes_by_host(platform)["blog.example.com"]["policy"][0]["allow"]["or"]}
    assert "b@mcp-hosting.test" in emails


def test_config_is_written_atomically_and_readable(tmp_path):
    path = tmp_path / "p" / "config.yaml"
    assert pomerium.write_config(path, {"a": 1}) is True
    assert pomerium.write_config(path, {"a": 1}) is False
    assert pomerium.write_config(path, {"a": 2}) is True
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert pomerium.restore_previous(path) is True
    assert json.loads(path.read_text()) == {"a": 1}


# ---------------------------------------------------------------- database
def test_snapshot_and_recovery(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.set_setting("quotas", {"max_apps": 3})
    snapshot = tmp_path / "backup" / "db.sqlite3"
    db.snapshot(snapshot)
    assert recover_from_snapshot(tmp_path / "db.sqlite3", snapshot) is False  # healthy: untouched
    (tmp_path / "broken.sqlite3").write_bytes(b"garbage" * 1000)
    assert recover_from_snapshot(tmp_path / "broken.sqlite3", snapshot) is True
    assert Database(tmp_path / "broken.sqlite3").get_setting("quotas")["max_apps"] == 3


# ---------------------------------------------------------------- narrow API (the AI)
@pytest.fixture
def narrow(tmp_path):
    platform, gaming = make_platform(tmp_path)
    return platform, TestClient(build_narrow_app(platform, gaming), base_url="http://deployer:8090")


def auth_headers(token: str = API_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_narrow_api_requires_token(narrow):
    _, client = narrow
    assert client.get("/v1/apps").status_code == 401
    assert client.get("/v1/apps", headers=auth_headers("wrong")).status_code == 401
    assert client.get("/v1/apps", headers=auth_headers()).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_narrow_api_deploy_and_read(narrow, tmp_path):
    platform, client = narrow
    archive = site_archive({"index.html": "<p>hi</p>", "css/a.css": "p{}"}, tmp_path)
    response = client.post("/v1/apps/blog/versions", params={"note": "first"},
                           content=archive.read_bytes(), headers=auth_headers())
    assert response.status_code == 200, response.text
    assert response.json()["files"] == 2
    source = client.get("/v1/apps/blog/source", headers=auth_headers()).json()
    assert {f["path"] for f in source["files"]} == {"index.html", "css/a.css"}
    one = client.get("/v1/apps/blog/source", params={"path": "css/a.css"}, headers=auth_headers()).json()
    assert one["content"] == "p{}"
    assert list((platform.settings.sites_dir / "_staging").iterdir()) == []


@pytest.mark.parametrize("version,path", [
    ("../../..", None),
    ("../../../etc", "passwd"),
    ("live", "../../../../../../proc/self/environ"),
    ("0123456789abcdef", None),
])
def test_narrow_api_source_cannot_escape(narrow, tmp_path, version, path):
    _, client = narrow
    archive = site_archive({"index.html": "x"}, tmp_path)
    client.post("/v1/apps/blog/versions", content=archive.read_bytes(), headers=auth_headers())
    params = {"version": version}
    if path:
        params["path"] = path
    response = client.get("/v1/apps/blog/source", params=params, headers=auth_headers())
    assert response.status_code in (400, 404)
    assert "DEPLOYER" not in response.text


def test_narrow_api_cannot_widen_access(narrow, tmp_path):
    platform, client = narrow
    archive = site_archive({"index.html": "x"}, tmp_path)
    client.post("/v1/apps/blog/versions", content=archive.read_bytes(), headers=auth_headers())
    # No route changes the stage directly; it only files a request.
    assert client.post("/v1/apps/blog/stage", json={"stage": "public"}, headers=auth_headers()).status_code in (404, 405)
    response = client.post("/v1/apps/blog/requests", json={"kind": "stage", "payload": {"stage": "public"}},
                           headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert platform.db.app("blog")["stage"] == "dev"


def test_narrow_api_shared_apps_need_approval_to_start_or_remove(narrow, tmp_path):
    platform, client = narrow
    archive = site_archive({"index.html": "x"}, tmp_path)
    client.post("/v1/apps/blog/versions", content=archive.read_bytes(), headers=auth_headers())
    platform.set_stage("blog", "public", [], [], OWNER)
    # Stopping and deleting a protected app are the owner's own actions: no ask is filed.
    stopped = client.post("/v1/apps/blog/stop", headers=auth_headers())
    assert stopped.status_code == 400 and "not allowed the AI to start or stop" in stopped.json()["error"]
    assert platform.db.app("blog")["online"]
    platform.set_online("blog", False, OWNER)
    start = client.post("/v1/apps/blog/start", headers=auth_headers()).json()
    assert start["status"] == "approval needed"
    assert start["request"]["approval_url"].startswith("https://admin.example.com/apps/blog#ask-")
    removed = client.delete("/v1/apps/blog", headers=auth_headers())
    assert removed.status_code == 400 and "not allowed the AI to delete" in removed.json()["error"]
    assert platform.db.app("blog") is not None
    assert [r["kind"] for r in platform.db.requests(status="pending", app="blog")] == ["start"]


def test_narrow_api_bad_numbers_are_400(narrow):
    _, client = narrow
    response = client.get("/v1/apps/blog/logs", params={"lines": "many"}, headers=auth_headers())
    assert response.status_code == 400


def test_narrow_api_refuses_big_json_bodies(narrow):
    _, client = narrow
    big = '{"kind": "stage", "payload": {"x": "' + "y" * 200_000 + '"}}'
    response = client.post("/v1/apps/blog/requests", content=big,
                           headers={**auth_headers(), "Content-Type": "application/json"})
    assert response.status_code == 413


def test_narrow_api_start_and_remove_respect_the_stage(narrow, tmp_path):
    platform, client = narrow
    archive = site_archive({"index.html": "x"}, tmp_path)
    client.post("/v1/apps/blog/versions", content=archive.read_bytes(), headers=auth_headers())
    client.post("/v1/apps/blog/stop", headers=auth_headers())
    assert client.post("/v1/apps/blog/start", headers=auth_headers()).json()["online"] is True
    assert client.delete("/v1/apps/blog", headers=auth_headers()).json() == {"removed": "blog"}


def test_narrow_api_upload_limit(narrow, tmp_path):
    platform, client = narrow
    platform.db.set_setting("quotas", {"max_upload_mb": 1})
    response = client.post("/v1/apps/blog/versions", content=b"x" * (1024 * 1024 + 10), headers=auth_headers())
    assert response.status_code == 413


# ---------------------------------------------------------------- full API (admin page)
def test_full_api_settings_validation(tmp_path):
    platform, gaming = make_platform(tmp_path)
    client = TestClient(build_full_app(platform, gaming), base_url="http://deployer")
    assert client.put("/v1/settings/quotas", json={"value": {"max_apps": "5"}}).json()["value"]["max_apps"] == 5
    assert client.put("/v1/settings/quotas", json={"value": {"max_apps": "-1"}}).status_code == 400
    assert client.put("/v1/settings/quotas", json={"value": {"nope": 1}}).status_code == 400
    assert client.put("/v1/settings/log_access_defaults", json={"value": {"public": "everything"}}).status_code == 400
    assert client.put("/v1/settings/other", json={"value": 1}).status_code == 400
    response = client.put("/v1/settings/workspace_gpu_allowed", json={"value": False})
    assert response.json()["value"] is False
    state = json.loads((platform.settings.control_dir / "state.json").read_text())
    assert state["workspace_gpu_allowed"] is False


def test_full_api_audit_records_actor(tmp_path):
    platform, gaming = make_platform(tmp_path)
    client = TestClient(build_full_app(platform, gaming), base_url="http://deployer")
    client.post("/v1/people", json={"op": "add_person", "email": "x@mcp-hosting.test"}, headers={"X-Actor": OWNER})
    entries = client.get("/v1/audit").json()["audit"]
    assert entries[0]["actor"] == OWNER and entries[0]["action"] == "people.add"
