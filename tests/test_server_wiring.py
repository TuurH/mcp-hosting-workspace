"""The real app.server.app, imported fresh in a subprocess with a controlled environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = textwrap.dedent(
    """
    import asyncio, base64, json
    import httpx
    from app import server

    def answer(response):
        data = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]
        return data["result"]

    async def main():
        out = {"allowed_hosts": server.allowed_hosts}
        async with server.app.router.lifespan_context(server.app):
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
                health = (await client.get("/healthz")).json()
                out["healthz"] = health["status"]
                out["health_name"] = health["server"]
                headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}}
                response = await client.post("/mcp", json=init, headers=headers)
                out["initialize"] = response.status_code
                if response.status_code == 200:
                    out["mcp_name"] = answer(response)["serverInfo"]["name"]
                    session = response.headers.get("mcp-session-id")
                    out["session_id"] = session
                    if session:
                        headers["Mcp-Session-Id"] = session
                    notification = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
                    out["initialized_reply"] = [notification.status_code, len(notification.content)]
                    limit = server.settings.max_write_file_bytes
                    content = base64.b64encode(b"x" * (limit - 10)).decode()
                    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "workspace_write_file",
                        "arguments": {"path": "wiring/big.bin", "content": content, "encoding": "base64",
                                      "overwrite": True}}}
                    response = await client.post("/mcp", json=call, headers=headers)
                    out["write_status"] = response.status_code
                    out["write_ok"] = '"isError":false' in response.text.replace(" ", "")
                    # A call that arrives without any session (a client after a
                    # server restart, or one that never keeps sessions).
                    plain = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                    info = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "workspace_info", "arguments": {}}}
                    response = await client.post("/mcp", json=info, headers=plain)
                    out["sessionless_status"] = response.status_code
                    out["sessionless_ok"] = '"isError":false' in response.text.replace(" ", "")
                    out["answer_type"] = response.headers.get("content-type", "").split(";")[0]

                    def tool_answer(response):
                        result = answer(response)
                        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

                    async def call_tool(number, name, arguments, agent):
                        body = {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                        return tool_answer(await client.post("/mcp", json=body, headers={**plain, "User-Agent": agent}))

                    listed = answer(await client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, headers=plain))
                    tools = {tool["name"]: tool for tool in listed["tools"]}
                    out["tools"] = sorted(tools)
                    out["exec_inputs"] = sorted(tools["workspace_exec"]["inputSchema"]["properties"])
                    out["job_inputs"] = sorted(tools["workspace_job_start"]["inputSchema"]["properties"])
                    out["descriptions"] = {name: tools[name]["description"] for name in
                                           ("workspace_write_file", "workspace_import_file", "workspace_exec", "app_deploy", "app_call_read", "app_request", "app_stop")}
                    out["read_only"] = {name: tools[name]["annotations"]["readOnlyHint"] for name in ("app_call_read", "app_call_write", "workspace_activity")}
                    out["import_inputs"] = tools["workspace_import_file"]["inputSchema"]
                    # Real transport responses must preserve expected messages, while
                    # unexpected bugs still keep internal details out of the result.
                    async def raw_call(number, name, arguments):
                        return answer(await client.post('/mcp', json={"jsonrpc": "2.0", "id": number,
                            "method": "tools/call", "params": {"name": name, "arguments": arguments}}, headers=plain))
                    out['validation_error'] = await raw_call(20, 'workspace_job_start', {'command': ''})
                    out['url_error'] = await raw_call(21, 'workspace_import_file', {'url': 'http://example.test/file'})
                    original_call = server.platform_tools.call
                    def denied(*args, **kwargs):
                        raise server.platform_tools.PlatformError('Read action permission is off. Ask the owner: https://admin.example.test/apps/demo')
                    server.platform_tools.call = denied
                    out['permission_error'] = await raw_call(22, 'app_call_read', {'name': 'demo', 'action': 'list'})
                    def crash(*args, **kwargs):
                        raise RuntimeError('private crash details')
                    server.platform_tools.call = crash
                    out['crash_error'] = await raw_call(23, 'app_call_read', {'name': 'demo', 'action': 'list'})
                    server.platform_tools.call = original_call
                    # The distinct URL field bypasses file-upload client rewriting.
                    # Mock only the download; exercise validation, streaming and paths.
                    original_client = server.httpx.AsyncClient
                    class DownloadClient(original_client):
                        def __init__(self, **kwargs):
                            super().__init__(**kwargs, transport=httpx.MockTransport(
                                lambda request: httpx.Response(200, content=b'url-import-ok')))
                    server.httpx.AsyncClient = DownloadClient
                    out['url_import'] = await raw_call(24, 'workspace_import_file', {'url': 'https://example.test/test.txt'})
                    out['attachment_import'] = await raw_call(25, 'workspace_import_file', {'file': {
                        'download_url': 'https://example.test/attachment', 'file_id': 'file-test', 'file_name': 'attachment.txt'}})
                    server.httpx.AsyncClient = original_client
                    job = {"command": "sleep 20", "label": "wiring job", "request_id": "wiring-request-1"}
                    first = await call_tool(5, "workspace_job_start", job, "Claude-User/1.0")
                    second = await call_tool(6, "workspace_job_start", job, "Claude-User/1.0")
                    out["job_repeat"] = [first["job_id"] == second["job_id"], second.get("repeated")]
                    seen = await call_tool(7, "workspace_activity", {}, "openai-mcp/1.0")
                    out["activity_job"] = next((item for item in seen["jobs_active"] if item["job_id"] == first["job_id"]), None)
                    ran = await call_tool(8, "workspace_exec", {"command": "echo from-exec"}, "Claude-User/1.0")
                    out["exec_stdout"] = ran["stdout"].strip()
                    await call_tool(9, "workspace_job_stop", {"job_id": first["job_id"], "force_after_seconds": 2}, "test")
                public = await client.post("/mcp", json=init, headers={**headers, "Host": "mcp.example.com"})
                out["public_host"] = public.status_code
        print(json.dumps(out))

    asyncio.run(main())
    """
)


def run_server(tmp_path: Path, **env: str) -> dict:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "WORKSPACE_ROOT": str(tmp_path),
        "PUBLIC_MCP_HOST": "mcp.example.com",
        "FILES_MINT_TOKEN": "t" * 40,
        "PYTHONPATH": str(ROOT),
        **env,
    }
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_locked_without_access_settings(tmp_path):
    out = run_server(tmp_path, CF_ACCESS_ENABLED="true", CF_ACCESS_AUD="")
    assert out["healthz"] == "locked"
    assert out["health_name"] == "MCP Hosting Workspace"
    assert out["initialize"] == 503
    assert out["allowed_hosts"] == ["mcp.example.com", "mcp.example.com:*"]


def test_local_dev_round_trip_and_write_limit(tmp_path):
    out = run_server(tmp_path, LOCAL_DEV_INSECURE="true", MAX_WRITE_FILE_BYTES=str(6 * 1024 * 1024),
                     MCP_SERVER_NAME="Owner App Host")
    assert out["healthz"] == "insecure"
    assert out["initialize"] == 200
    assert out["health_name"] == out["mcp_name"] == "Owner App Host"
    assert out["initialized_reply"] == [202, 0]
    assert out["write_status"] == 200 and out["write_ok"], out
    # Stateless: no session is handed out and a call needs none, so a restart
    # never leaves a client with a dead session ID.
    assert out["session_id"] is None
    assert out["sessionless_status"] == 200 and out["sessionless_ok"], out
    # Answers stay streamed: the keep-alive pings carry long calls past the proxy's silence limit.
    assert out["answer_type"] == "text/event-stream"
    assert (tmp_path / "wiring" / "big.bin").stat().st_size == 6 * 1024 * 1024 - 10
    # Tools: the new ones exist, the request context never becomes an input, texts state real numbers.
    assert {"workspace_activity", "app_call_read", "app_call_write"} <= set(out["tools"])
    assert out["exec_inputs"] == ["command", "cwd", "env", "timeout_seconds"]
    assert out["job_inputs"] == ["command", "cwd", "env", "gpu", "gpu_memory_mb", "label", "request_id"]
    assert "6 MB" in out["descriptions"]["workspace_write_file"] and "100 KB" in out["descriptions"]["workspace_write_file"]
    assert "2 GB" in out["descriptions"]["workspace_import_file"] and "240 seconds" in out["descriptions"]["workspace_exec"]
    assert "data_change" in out["descriptions"]["app_deploy"] and "declare actions" in out["descriptions"]["app_call_read"]
    assert '"rollback"' in out["descriptions"]["app_request"] and '"remove"' not in out["descriptions"]["app_request"]
    assert "Always allowed" not in out["descriptions"]["app_stop"]
    assert out["read_only"] == {"app_call_read": True, "app_call_write": False, "workspace_activity": True}
    assert 'url' in out['import_inputs']['properties'] and 'file' not in out['import_inputs'].get('required', [])
    for field, words in [('validation_error', 'command cannot be empty'), ('url_error', 'Only HTTPS'),
                         ('permission_error', 'https://admin.example.test/apps/demo')]:
        assert out[field]['isError'] is True and words in out[field]['content'][0]['text'], out[field]
    assert out['crash_error']['isError'] is True and 'private crash details' not in json.dumps(out['crash_error'])
    assert out['url_import'].get('isError') is not True and (tmp_path / 'inbox/test.txt').read_bytes() == b'url-import-ok'
    assert out['attachment_import'].get('isError') is not True and (tmp_path / 'inbox/attachment.txt').read_bytes() == b'url-import-ok'
    # A repeated call with the same request_id starts one job; every chat sees who started it.
    assert out["job_repeat"] == [True, True]
    assert out["activity_job"]["label"] == "wiring job" and out["activity_job"]["started_by"]["client"] == "Claude"
    assert out["exec_stdout"] == "from-exec"
    # The public hostname is refused in local mode, so the tunnel cannot use it.
    assert out["public_host"] == 421
    assert "mcp.example.com" not in out["allowed_hosts"]
