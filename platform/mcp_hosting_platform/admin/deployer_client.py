"""Talks to the deployer's full API over the shared unix socket."""

from __future__ import annotations

from typing import Any

import httpx

# Page loads must answer well inside Cloudflare's 100-second limit; actions that
# start work may take longer.
READ_TIMEOUT = 30.0
WRITE_TIMEOUT = 180.0


class DeployerError(RuntimeError):
    pass


class DeployerClient:
    def __init__(self, socket_path: str) -> None:
        # One client for the life of the process. Closing a client also closes
        # its connection pool, which would cut off every other call that is
        # still waiting for its answer (seen as "ReadError" on the admin page).
        self._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path),
                                         base_url="http://deployer")

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, actor: str, json: Any = None,
                   params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        read_only = method == "GET"
        if timeout is None:
            timeout = READ_TIMEOUT if read_only else WRITE_TIMEOUT
        # A read can be repeated safely once; a change is never sent twice.
        attempts = 2 if read_only else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, path, json=json, params=params,
                                                      headers={"X-Actor": actor}, timeout=timeout)
                break
            except httpx.TimeoutException as exc:
                raise DeployerError("the deployer did not answer in time; it may be busy with a long "
                                    "operation. Reload in a moment") from exc
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    raise DeployerError(f"the deployer is not reachable ({exc.__class__.__name__})") from exc
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:500]}
        if response.status_code >= 400:
            raise DeployerError(str(data.get("error", f"HTTP {response.status_code}")))
        return data
