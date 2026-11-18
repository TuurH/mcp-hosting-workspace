"""Command line for the owner, run inside the deployer container:

    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli gaming on --source auto
    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli gaming off
    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli status
    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli snapshot-db
    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli games
    docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli killswitch

The kill switch works without a browser, e.g. when example.com cookies are broken.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from .config import DeployerSettings


def _client(settings: DeployerSettings) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=str(settings.socket_path))
    return httpx.Client(transport=transport, base_url="http://deployer", timeout=180.0,
                        headers={"X-Actor": "owner (Windows CLI)"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-hosting-deployer")
    sub = parser.add_subparsers(dest="command", required=True)
    gaming = sub.add_parser("gaming", help="turn gaming mode on or off")
    gaming.add_argument("action", choices=["on", "off", "status"])
    gaming.add_argument("--source", default="manual", choices=["manual", "auto"])
    sub.add_parser("runtime-images", help="print immutable application image IDs for backups")
    sub.add_parser("status", help="show platform status")
    sub.add_parser("snapshot-db", help="write a consistent copy of the database for backups")
    sub.add_parser("games", help="print the game programs for the automatic trigger, one per line")
    sub.add_parser("killswitch", help="make every app visible only to the owner again")
    args = parser.parse_args(argv)

    settings = DeployerSettings()
    with _client(settings) as client:
        if args.command == "gaming" and args.action in ("on", "off"):
            body = {"source": args.source} if args.action == "on" else {}
            response = client.post(f"/v1/gaming/{args.action}", json=body)
        elif args.command in ("status", "gaming"):
            response = client.get("/v1/status")
        elif args.command == "runtime-images":
            response = client.get("/v1/runtime/images")
        elif args.command == "games":
            response = client.get("/v1/settings")
        elif args.command == "killswitch":
            response = client.post("/v1/killswitch")
        else:
            response = client.post("/v1/snapshot")
    data = response.json()
    if args.command == "runtime-images":
        for image in data.get("images", []):
            print(image)
        return 0 if response.status_code < 400 else 1
    if args.command == "games":
        for game in data.get("gaming", {}).get("games", []):
            print(game)
        return 0 if response.status_code < 400 else 1
    if args.command == "gaming" and args.action == "status":
        data = {"gaming": data.get("gaming"), "detail": data.get("gaming_detail", {}).get("log", [])[-5:]}
    print(json.dumps(data, indent=2))
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
