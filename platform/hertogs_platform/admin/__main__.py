from __future__ import annotations

import logging

import uvicorn

from .app import build_app
from .config import AdminSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = AdminSettings()
    problems = settings.problems()
    if problems:
        # Stay up (healthy) but answer every page with 503 until .env is complete.
        logging.getLogger("hertogs.admin").error(
            "Admin page is LOCKED until configured: %s", "; ".join(problems))
    if settings.local_dev_insecure:
        logging.getLogger("hertogs.admin").warning(
            "ADMIN_LOCAL_DEV_INSECURE is on: no login check. Never use this behind the tunnel.")
    # Only cloudflared (fixed address on edge-admin) may set forwarded headers.
    uvicorn.run(build_app(settings), host="0.0.0.0", port=settings.port, proxy_headers=True,
                forwarded_allow_ips="10.204.3.2", server_header=False, log_level="info")


if __name__ == "__main__":
    main()
