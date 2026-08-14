"""Unit tests for the storage backend factory + settings cluster validator (ADR-054).

`Settings` is constructed with explicit kwargs (never `.env`) so the presence-switch
+ half-config fail-fast are asserted in isolation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.storage.factory import build_storage_adapter
from src.storage.storage_service_rest import StorageServiceRestStorage
from src.storage.supabase_rest import SupabaseRestStorage

_REQUIRED = {
    "app_db_url": "postgresql://unit/db",
    "remix_editor_token_secret": "a",
    "remix_editor_handoff_secret": "b",
}


def test_empty_cluster_selects_supabase_legacy():
    s = Settings(**_REQUIRED)
    assert isinstance(build_storage_adapter(s), SupabaseRestStorage)


def test_full_cluster_selects_storage_service():
    s = Settings(
        **_REQUIRED,
        storage_service_url="http://127.0.0.1:8200",
        storage_service_api_key="k",
        storage_public_base_url="http://localhost:8200",
    )
    assert isinstance(build_storage_adapter(s), StorageServiceRestStorage)


def test_trailing_slashes_stripped_off_url_fields():
    s = Settings(
        **_REQUIRED,
        storage_service_url="http://127.0.0.1:8200/",
        storage_service_api_key="k",
        storage_public_base_url="http://localhost:8200/",
    )
    assert s.storage_service_url == "http://127.0.0.1:8200"
    assert s.storage_public_base_url == "http://localhost:8200"


@pytest.mark.parametrize(
    "missing",
    [
        {"storage_service_api_key": "k"},          # missing public base
        {"storage_public_base_url": "http://p"},   # missing api key
        {},                                        # missing both
    ],
)
def test_half_config_fails_fast(missing):
    with pytest.raises(ValidationError):
        Settings(**_REQUIRED, storage_service_url="http://127.0.0.1:8200", **missing)
