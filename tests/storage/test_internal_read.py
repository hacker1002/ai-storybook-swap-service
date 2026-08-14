"""Unit tests for the internal-read URL rewrite (ADR-054 — parity image-api).

`to_fetch_url` reads the settings singleton at CALL time, so monkeypatching the
instance attributes is enough — no Settings re-construction needed.
"""

from __future__ import annotations

import pytest

from src.config.settings import settings
from src.storage.internal_read import to_fetch_url

_PUB = "https://storage.example.com"
_INTERNAL = "http://127.0.0.1:8200"
_KEY_PATH = "/files/storybook-assets/remix/sheet.png"


@pytest.fixture
def internal_read_on(monkeypatch):
    monkeypatch.setattr(settings, "storage_public_base_url", _PUB)
    monkeypatch.setattr(settings, "storage_internal_read_base_url", _INTERNAL)


def test_rewrites_public_url_to_internal_base(internal_read_on):
    assert to_fetch_url(_PUB + _KEY_PATH) == _INTERNAL + _KEY_PATH


def test_noop_when_internal_base_empty(monkeypatch):
    monkeypatch.setattr(settings, "storage_public_base_url", _PUB)
    monkeypatch.setattr(settings, "storage_internal_read_base_url", "")
    url = _PUB + _KEY_PATH
    assert to_fetch_url(url) == url


def test_noop_when_public_base_empty(monkeypatch):
    monkeypatch.setattr(settings, "storage_public_base_url", "")
    monkeypatch.setattr(settings, "storage_internal_read_base_url", _INTERNAL)
    url = _PUB + _KEY_PATH
    assert to_fetch_url(url) == url


def test_noop_for_url_outside_public_base(internal_read_on):
    # Legacy Supabase URL (dual-read window) must pass through untouched.
    url = "https://xyz.supabase.co/storage/v1/object/public/storybook-assets/a.png"
    assert to_fetch_url(url) == url


def test_trailing_slashes_on_bases_are_tolerated(monkeypatch):
    monkeypatch.setattr(settings, "storage_public_base_url", _PUB + "/")
    monkeypatch.setattr(settings, "storage_internal_read_base_url", _INTERNAL + "/")
    assert to_fetch_url(_PUB + _KEY_PATH) == _INTERNAL + _KEY_PATH
