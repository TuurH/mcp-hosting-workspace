"""Bounded HTTP observations of untrusted app responses."""
from __future__ import annotations

import asyncio
import httpx


async def observe(method: str, url: str, *, limit: int = 4000, seconds: float = 10,
                  headers: dict | None = None, body: str | None = None,
                  transport=None) -> dict:
    try:
        # HTTPX's read timeout is per chunk. A separate deadline also stops a
        # peer that sends one byte just often enough to keep the read alive.
        async with asyncio.timeout(seconds):
            async with httpx.AsyncClient(timeout=seconds, trust_env=False,
                                         follow_redirects=False, transport=transport) as client:
                async with client.stream(method, url, content=body,
                                         headers={**(headers or {}), 'Accept-Encoding': 'identity'}) as response:
                    result = {'status': response.status_code,
                              'content_type': response.headers.get('content-type', ''),
                              'location': response.headers.get('location')}
                    # Do not inflate app-controlled compressed bytes in this
                    # privileged helper. Apps should honour Accept-Encoding.
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        return {**result, 'error': 'Encoded responses are not previewed.'}
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        data.extend(chunk[:limit-len(data)])
                        if len(data) >= limit:
                            break
                    return {**result, 'truncated': len(data) >= limit,
                            'body': data.decode('utf-8', 'replace')}
    except (httpx.HTTPError, TimeoutError) as exc:
        return {'error': type(exc).__name__}


def probe(method: str, url: str, **kwargs) -> dict:
    """Synchronous entry point for the API's worker thread and gateway execs."""
    return asyncio.run(observe(method, url, **kwargs))
