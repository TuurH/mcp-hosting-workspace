"""Prints new secrets for .env, one KEY=VALUE per line (used by scripts/setup.ps1).

    docker run --rm mcp-hosting-workspace/platform:local python -m mcp_hosting_platform.setup_secrets KEY [KEY ...]

Windows PowerShell 5.1 cannot create an EC key in PEM form, so setup.ps1 asks
this helper for the keys it cannot make itself.
"""

from __future__ import annotations

import base64
import secrets
import sys

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _pomerium_signing_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


GENERATORS = {
    "POMERIUM_SIGNING_KEY": _pomerium_signing_key,
    "POMERIUM_SHARED_SECRET": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
    "POMERIUM_COOKIE_SECRET": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
    "DEPLOYER_SECRETS_KEY": lambda: Fernet.generate_key().decode(),
    "DEPLOYER_API_TOKEN": lambda: secrets.token_urlsafe(32),
    "ADMIN_CSRF_KEY": lambda: secrets.token_urlsafe(32),
    "FILES_MINT_TOKEN": lambda: secrets.token_urlsafe(32),
}


def main(argv: list[str]) -> int:
    unknown = [name for name in argv if name not in GENERATORS]
    if unknown or not argv:
        print(f"usage: setup_secrets KEY [KEY ...] with KEY in {', '.join(GENERATORS)}", file=sys.stderr)
        return 2
    for name in argv:
        print(f"{name}={GENERATORS[name]()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
