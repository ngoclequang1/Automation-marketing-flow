from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not allowed.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("The URL hostname could not be resolved.") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Private, loopback, link-local and reserved hosts are not allowed.")
    return url


def resolve_media_path(value: str) -> Path:
    candidate = Path(value).resolve()
    try:
        candidate.relative_to(settings.media_root)
    except ValueError as exc:
        raise ValueError("Path must be inside MEDIA_ROOT.") from exc
    return candidate
