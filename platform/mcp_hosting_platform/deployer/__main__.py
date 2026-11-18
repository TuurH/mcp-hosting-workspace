from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import uvicorn

from ..common.docker_client import DockerClient
from .api import build_full_app, build_narrow_app
from .config import DeployerSettings
from .core import Platform, RouteError
from .db import Database, recover_from_snapshot
from .gaming import Gaming
from .sites import Sites
from .runtime import Runtime

logger = logging.getLogger("mcp-hosting.deployer")


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _reconcile_loop(platform: Platform, gaming: Gaming, stop: threading.Event) -> None:
    while not stop.wait(60):
        # Separately: a failure in one (for example a full disk) must not stop the other.
        try:
            gaming.reconcile()
            gaming.resources.reconcile()
        except Exception:  # keep the loop alive
            logger.exception("gaming reconcile failed")
        try:
            if platform.runtime:
                platform.runtime.reconcile()
            platform.workspace_credentials.publish()
            platform.apply_routes("reconcile")
        except RouteError as exc:
            logger.error("reconcile: %s", exc)
        except Exception:
            logger.exception("reconcile failed")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # One INFO line per Docker call (about 170,000 a day) buries the real errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = DeployerSettings()
    problems = settings.problems()
    if problems:
        logger.error("Deployer is not configured: %s", "; ".join(problems))
        sys.exit(2)

    if recover_from_snapshot(settings.db_path, settings.snapshot_path):
        logger.warning("The database was damaged; restored it from %s", settings.snapshot_path)
    db = Database(settings.db_path)
    sites = Sites(settings.sites_dir)
    docker = DockerClient(settings.docker_socket) if Path(settings.docker_socket).exists() else None
    if docker is None:
        logger.warning("No Docker socket at %s: gaming mode cannot change the workspace", settings.docker_socket)
    else:
        health = docker.ping()
        if health != "ok":
            logger.error("The Docker socket cannot be used (%s): check DOCKER_SOCKET_GID in .env", health)
    platform = Platform(settings, db, sites)
    platform.runtime = Runtime(platform, docker)
    gaming = Gaming(settings, db, docker)
    gaming.runtime = platform.runtime
    platform.runtime.gaming = gaming
    try:
        platform.runtime.reconcile()
    except Exception:
        logger.exception('Runtime startup recovery failed; the reconcile loop retries')
    try:
        gaming.write_control()
    except OSError:
        logger.exception("could not write the control file at startup; the reconcile loop retries")
    platform.workspace_credentials.publish()
    platform.repair_versions()
    # Pomerium starts after the deployer is healthy, so write without probing here.
    # A failure must not keep the deployer (and with it the admin page) down.
    try:
        platform.apply_routes("startup", probe=False)
    except Exception:
        logger.exception("could not write the routes at startup; the reconcile loop retries")

    stop = threading.Event()
    threading.Thread(target=_reconcile_loop, args=(platform, gaming, stop), daemon=True).start()
    if docker is not None:
        # Storage is measured in the background; pages only read the last result.
        threading.Thread(target=platform.runtime.meter.loop, args=(stop,), daemon=True, name="storage-meter").start()

    # The full API needs no token: the socket's folder (a volume shared only with
    # the admin container, both uid 1000) is closed to everyone else. Do not
    # change the umask instead: the static server must be able to read the sites.
    settings.socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.socket_path.parent, 0o700)
    with contextlib.suppress(FileNotFoundError):
        settings.socket_path.unlink()

    common = {"proxy_headers": False, "server_header": False, "log_level": "info"}
    servers = [
        _Server(uvicorn.Config(build_narrow_app(platform, gaming), host="0.0.0.0",
                               port=settings.narrow_port, **common)),
        _Server(uvicorn.Config(build_full_app(platform, gaming), uds=str(settings.socket_path), **common)),
    ]

    def shutdown() -> None:
        stop.set()
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
