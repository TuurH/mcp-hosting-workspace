"""Runs inside a container on the deployer-api, platform and edge-admin networks.

Plays the workspace (narrow API with the bearer token), a visitor (through
Pomerium) and the owner (admin page in local test mode).
"""

from __future__ import annotations

import io
import os
import re
import sys
import tarfile
import time

import httpx

TOKEN = os.environ["DEPLOYER_API_TOKEN"]
ADMIN_TOKEN = os.environ["ADMIN_LOCAL_TOKEN"]
DEPLOYER = "http://deployer:8090"
POMERIUM = "http://pomerium:80"
ADMIN = "http://admin:8080"
AI = {"Authorization": f"Bearer {TOKEN}"}
OWNER_FORM = {"Origin": "http://localhost:8088", "Sec-Fetch-Site": "same-origin"}
failures = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global failures
    print(("ok   " if ok else "FAIL ") + label + (f"  ({detail})" if detail != "" and not ok else ""))
    if not ok:
        failures += 1


def site(html: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = html.encode()
        info = tarfile.TarInfo("index.html")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def visit(client: httpx.Client, host: str, path: str = "/") -> httpx.Response:
    return client.get(f"{POMERIUM}{path}", headers={
        "Host": host, "X-Forwarded-Proto": "https", "Cf-Connecting-IP": "203.0.113.9",
        "X-Pomerium-Claim-Email": "owner@hertogs.test"})


def until(fn, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(0.5)


def owner_post(client: httpx.Client, path: str, data: dict) -> httpx.Response:
    page = client.get(f"{ADMIN}/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return client.post(f"{ADMIN}{path}", params={"_csrf": csrf}, data=data, headers=OWNER_FORM)


with httpx.Client(timeout=30.0, follow_redirects=False) as client:
    # --- the admin page's local test mode needs the one-time link -------------
    check("admin local mode refuses requests without the login link", client.get(f"{ADMIN}/").status_code == 403)
    check("wrong login link is refused",
          client.get(f"{ADMIN}/local-login", params={"token": "guess"}).status_code == 403)
    login = client.get(f"{ADMIN}/local-login", params={"token": ADMIN_TOKEN})
    check("login link sets the session cookie", login.status_code == 303 and "hertogs_admin_local" in client.cookies)
    check("login link works only once",
          httpx.get(f"{ADMIN}/local-login", params={"token": ADMIN_TOKEN}).status_code == 403)

    # --- the AI's narrow API -------------------------------------------------
    check("narrow API refuses requests without the token", client.get(f"{DEPLOYER}/v1/status").status_code == 401)
    status = client.get(f"{DEPLOYER}/v1/status", headers=AI)
    check("narrow API status", status.status_code == 200, status.text)

    deployed = client.post(f"{DEPLOYER}/v1/apps/blog/versions", content=site("<h1>v1</h1>"), headers=AI)
    check("deploy a new app", deployed.status_code == 200 and deployed.json()["stage"] == "dev", deployed.text)

    # --- Pomerium routes ------------------------------------------------------
    dev = until(lambda: (r := visit(client, "blog.example.com")).status_code == 302 and r)
    check("Dev app asks for a login", bool(dev) and "auth.example.com" in dev.headers.get("location", ""))
    check("unknown host is 404", visit(client, "nothing.example.com").status_code == 404)
    check("canary host asks for a login", visit(client, "canary.example.com").status_code == 302)
    test = client.post(f"{DEPLOYER}/v1/apps/blog/test", json={"path": "/"}, headers=AI).json()
    owner_view = test["results"].get("live (direct upstream; authentication bypassed)", {})
    check("app_test fetches the direct upstream (without testing owner authentication)", owner_view.get("status") == 200 and "v1" in owner_view.get("body_start", ""), test)

    # The AI cannot make it public; it can only ask.
    asked = client.post(f"{DEPLOYER}/v1/apps/blog/requests", json={"kind": "stage", "payload": {"stage": "public"}},
                        headers=AI)
    check("stage change is only a request", asked.json().get("status") == "pending", asked.text)
    check("still private", visit(client, "blog.example.com").status_code == 302)

    # --- the owner approves on the admin page ----------------------------------
    request_id = asked.json()["id"]
    refused = owner_post(client, f"/requests/{request_id}/decide", {"decision": "approve"})
    check("approval without the typed host is refused", "err=" in refused.headers.get("location", ""), refused.headers)
    approved = owner_post(client, f"/requests/{request_id}/decide",
                          {"decision": "approve", "confirm": "blog.example.com"})
    check("owner approves Public", "msg=Applied" in approved.headers.get("location", ""), approved.headers)
    public = until(lambda: (r := visit(client, "blog.example.com")).status_code == 200 and r)
    check("Public app is served without login", bool(public) and "v1" in public.text)

    # --- updates of a Public app wait for the owner ------------------------------
    pending = client.post(f"{DEPLOYER}/v1/apps/blog/versions", content=site("<h1>v2</h1>"), headers=AI).json()
    check("update of a Public app is pending", pending.get("outcome") == "pending", pending)
    check("live site unchanged", "v1" in visit(client, "blog.example.com").text)
    check("preview is owner-only", visit(client, "next-blog.example.com").status_code == 302)
    diff = client.get(f"{ADMIN}/apps/blog/diff")
    check("admin shows the diff", diff.status_code == 200 and "v2" in diff.text)
    unbound = owner_post(client, "/apps/blog/do/release", {})
    check("a release must name the reviewed version", "err=" in unbound.headers.get("location", ""), unbound.headers)
    released = owner_post(client, "/apps/blog/do/release", {"version": pending["version"]})
    check("owner releases", released.status_code == 303 and "msg=" in released.headers.get("location", ""), released.headers)
    check("new version is live", bool(until(lambda: "v2" in visit(client, "blog.example.com").text)))

    # --- logs, masked for the AI ------------------------------------------------
    visit(client, "blog.example.com", "/u/alice@example.org")  # a path a page made up
    logs = until(lambda: (r := client.get(f"{DEPLOYER}/v1/apps/blog/logs", headers=AI).json())
                 and "[other path]" in str(r.get("entries")) and r)
    entries = (logs or {}).get("entries", [])
    check("AI gets masked visitor logs", bool(logs) and logs.get("mode") == "masked"
          and any(e["client_ip"] == "203.0.113.x" for e in entries)
          and all(e["client_ip"].endswith((".x", "/48", "?")) for e in entries), entries[:3])
    check("made-up paths are hidden from the AI", bool(logs) and "alice" not in str(logs), entries[-3:])

    # --- rollback names the version it goes back to ---------------------------------
    previous = client.get(f"{DEPLOYER}/v1/apps/blog", headers=AI).json()["previous_version"]
    check("a rollback must name the version", "err=" in owner_post(client, "/apps/blog/do/rollback", {}).headers.get("location", ""))
    rolled = owner_post(client, "/apps/blog/do/rollback", {"version": previous})
    check("owner rolls back", "msg=" in rolled.headers.get("location", ""), rolled.headers)
    check("old version is live again", bool(until(lambda: "v1" in visit(client, "blog.example.com").text)))

    # --- source reads stay inside the app -----------------------------------------
    escape = client.get(f"{DEPLOYER}/v1/apps/blog/source", params={"version": "../../..", "path": "etc/passwd"}, headers=AI)
    check("source cannot leave the app", escape.status_code in (400, 404), escape.status_code)

    # --- gaming mode ------------------------------------------------------------
    gaming = client.post(f"{DEPLOYER}/v1/gaming/on", headers=AI).json()
    check("AI can turn gaming mode on", gaming.get("state") == "on", gaming)
    check("AI cannot turn it off", client.post(f"{DEPLOYER}/v1/gaming/off", headers=AI).status_code in (404, 405))
    off = owner_post(client, "/gaming/off", {})
    check("owner turns it off", off.status_code == 303)

    # --- kill switch ---------------------------------------------------------------
    killed = owner_post(client, "/killswitch", {"confirm": "make everything private"})
    check("kill switch", "msg=" in killed.headers.get("location", ""), killed.headers)
    check("app is private again", bool(until(lambda: visit(client, "blog.example.com").status_code == 302)))

    # --- GPU page (there is no workspace container in this test) -----------------------
    gpu = client.get(f"{ADMIN}/gpu")
    check("GPU page says why it cannot check", gpu.status_code == 200 and "could not run" in gpu.text, gpu.status_code)

    # --- file manager -------------------------------------------------------------
    files = client.get(f"{ADMIN}/files")
    check("file manager lists /workspace", files.status_code == 200 and "Workspace files" in files.text)

sys.exit(1 if failures else 0)
