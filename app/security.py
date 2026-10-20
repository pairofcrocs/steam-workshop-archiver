"""ASGI middleware: host allowlist, Basic auth, CSRF header check, security headers."""

import base64
import binascii
import ipaddress
import json
import secrets

from . import config

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.steamstatic.com https://*.steamusercontent.com "
    "https://*.akamaihd.net https://steamcommunity.com; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
_SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
]
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/healthz"}


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        path = scope["path"]

        if "*" not in config.ALLOWED_HOSTS and path not in _PUBLIC_PATHS:
            host = parse_host(headers.get("host", ""))
            if not host_allowed(host, config.ALLOWED_HOSTS):
                return await _reject(
                    send, 400, f"Host {host!r} not allowed — add it to the ALLOWED_HOSTS environment variable."
                )

        if config.AUTH_PASSWORD and path not in _PUBLIC_PATHS and not _authorized(headers):
            return await _reject(
                send, 401, "Unauthorized",
                [(b"www-authenticate", b'Basic realm="Steam Workshop Archiver", charset="UTF-8"')],
            )

        if scope["method"] not in _SAFE_METHODS and headers.get("x-swa") != "1":
            return await _reject(send, 403, "Missing X-SWA header")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                existing = {k.lower() for k, _ in message["headers"]}
                message["headers"] += [(k, v) for k, v in _SECURITY_HEADERS if k not in existing]
            await send(message)

        await self.app(scope, receive, send_with_headers)


_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost", ".ts.net")


def parse_host(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value[1:]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def host_allowed(host: str, allowed: list[str]) -> bool:
    if host in allowed:
        return True
    if "auto" not in allowed:
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Single-label names ("tower"), localhost and LAN suffixes can't be attacker-registered domains.
    return "." not in host or host == "localhost" or host.endswith(_LAN_SUFFIXES)


def _authorized(headers: dict) -> bool:
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, _, pwd = decoded.partition(":")
    return secrets.compare_digest(pwd.encode(), config.AUTH_PASSWORD.encode())


async def _reject(send, status: int, detail: str, extra: list | None = None):
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        + _SECURITY_HEADERS + (extra or []),
    })
    await send({"type": "http.response.body", "body": body})
