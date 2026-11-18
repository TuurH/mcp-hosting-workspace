from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator

import uvicorn

from .app import build_mint_app, build_public_app
from .config import FilesSettings
from .store import LinkStore


class _Server(uvicorn.Server):
    """uvicorn server whose signal handling is done by main() for both listeners."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = FilesSettings()
    settings.validate()
    store = LinkStore(settings.db_path)

    common = {"host": "0.0.0.0", "proxy_headers": False, "server_header": False, "log_level": "info"}
    servers = [
        # No access log on the public port: its paths contain the download tokens.
        _Server(uvicorn.Config(build_public_app(settings, store), port=settings.public_port,
                               access_log=False, **common)),
        _Server(uvicorn.Config(build_mint_app(settings, store), port=settings.mint_port, **common)),
    ]

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)

    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
