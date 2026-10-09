from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import validation


def test_rejects_loopback_url():
    with patch("app.validation.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
        with pytest.raises(ValueError, match="Private"):
            validation.validate_public_url("http://localhost/admin")


def test_accepts_public_url():
    with patch("app.validation.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
        assert validation.validate_public_url("https://example.com/video") == "https://example.com/video"


def test_media_path_cannot_escape_root(monkeypatch):
    media_root = (Path.cwd() / "test-media-root").resolve()
    monkeypatch.setattr(validation, "settings", SimpleNamespace(media_root=media_root))
    with pytest.raises(ValueError, match="MEDIA_ROOT"):
        validation.resolve_media_path(str(media_root.parent / "secret.txt"))


def test_media_path_inside_root(monkeypatch):
    media_root = (Path.cwd() / "test-media-root").resolve()
    monkeypatch.setattr(validation, "settings", SimpleNamespace(media_root=media_root))
    target = media_root / "videos" / "clip.mp4"
    assert validation.resolve_media_path(str(target)) == Path(target).resolve()
