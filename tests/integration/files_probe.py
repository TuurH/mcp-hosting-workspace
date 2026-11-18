"""Runs inside a container on the test network; exits non-zero on the first failed check."""

import json
import os
import urllib.error
import urllib.request

TOKEN = os.environ["TOKEN"]


def request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()  # case-insensitive
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def mint(path, token=TOKEN):
    return request(
        "http://files:8081/internal/links",
        json.dumps({"path": path}).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def check(label, condition):
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        raise SystemExit(1)


status, _, body = mint("outputs/a.txt")
check("mint a 0600 file owned by the workspace user", status == 200)
path = json.loads(body)["download_url"].replace("https://files.example.com", "")

status, headers, body = request("http://files:8080" + path)
check("download returns the file", status == 200 and body == b"hello from workspace")
check("served as attachment", headers.get("Content-Disposition", "").startswith("attachment"))
check("sandbox CSP", "sandbox" in headers.get("Content-Security-Policy", ""))
check("nosniff", headers.get("X-Content-Type-Options") == "nosniff")

check("symlink to /etc/passwd refused", mint("outputs/evil")[0] == 404)
check("path traversal refused", mint("../etc/passwd")[0] == 404)
check("wrong mint token refused", mint("outputs/a.txt", token="x" * 43)[0] == 401)
check("public port has no mint route", request("http://files:8080/internal/links", b"{}", method="POST")[0] == 404)
check("public port root is 404", request("http://files:8080/")[0] == 404)
check("mint port has no download route", request("http://files:8081" + path)[0] == 404)
