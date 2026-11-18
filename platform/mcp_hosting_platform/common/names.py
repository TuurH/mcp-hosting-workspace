"""App name rules: one DNS label, no lookalikes of platform or login hostnames."""

from __future__ import annotations

import re
import hashlib


def runtime_gateway_name(app: str, version: str, preview: bool = False) -> str:
    """A stable, unique Docker DNS label below the 63-byte label limit."""
    identity = f'{app}:{version}:{int(preview)}'
    return 'mcp-hosting-gw-' + hashlib.sha256(identity.encode()).hexdigest()[:32]

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")

# Hostnames the platform or the owner already uses, and names a fake login page
# would want. Kept deliberately broad.
RESERVED = frozenset(
    {
        # platform and existing DNS records
        "mcp", "files", "auth", "admin", "www", "minecraft", "deployer", "pomerium",
        "static", "canary", "workspace", "cloudflared", "tunnel", "status", "api",
        "cdn", "assets", "mail", "smtp", "imap", "pop", "mx", "ftp", "ns", "ns1", "ns2",
        "domainconnect",
        # login / account lookalikes
        "login", "logout", "signin", "signup", "sso", "oauth", "oidc", "account",
        "accounts", "google", "gmail", "microsoft", "apple", "cloudflare", "claude",
        "chatgpt", "openai", "anthropic", "discord", "github", "id", "identity",
        "secure", "security", "verify", "password", "passwords", "billing", "pay",
        "payment", "bank", "support", "help", "root", "system", "internal",
    }
)
RESERVED_PREFIXES = ("next-", "auth-", "login-", "admin-", "sso-", "xn--")


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    # b is one longer: try deleting each char of b
    return any(b[:i] + b[i + 1 :] == a for i in range(len(b)))


def looks_like_app_name(name: str) -> bool:
    """The basic shape only (existing apps may predate newer reserved words)."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def name_problem(name: str, extra_reserved: frozenset[str] = frozenset()) -> str | None:
    """Return why a name is not allowed, or None when it is fine."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return "names are 2-30 characters: lowercase letters, digits and '-', starting with a letter"
    if "--" in name or name.endswith("-"):
        return "names cannot contain '--' or end with '-'"
    for prefix in RESERVED_PREFIXES:
        if name.startswith(prefix):
            return f"names cannot start with '{prefix}'"
    reserved = RESERVED | extra_reserved
    if name in reserved:
        return f"'{name}' is reserved"
    for word in reserved:
        if len(word) >= 5 and _edit_distance_at_most_one(name, word):
            return f"'{name}' is too similar to the reserved name '{word}'"
    return None
