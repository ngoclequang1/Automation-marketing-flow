from __future__ import annotations

import hmac
from fastapi import Header, HTTPException

from app.config import settings
from app.validation import resolve_media_path, validate_public_url


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Require an API key when configured, while keeping local development simple."""
    if settings.api_key and not (
        x_api_key and hmac.compare_digest(x_api_key, settings.api_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
