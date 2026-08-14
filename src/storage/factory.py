"""Storage backend selector (ADR-054 env-presence switch).

ONE decision point wiring the single `AppStorageAdapter` seam: if
`settings.storage_service_url` is set → the self-hosted storage service
(`StorageServiceRestStorage`, loopback S2S); otherwise the legacy Supabase
Storage REST impl (`SupabaseRestStorage`) — the rollback path.

Dispatch is at WIRING time (lifespan), NOT per-call: this service already funnels
every write through one adapter accessor (`get_storage()`), so a per-call switch
(image-api's model, needed because its uploader is a module of functions) would be
redundant here (KISS). Settings' `model_validator` already guarantees the cluster
is whole when the URL is set — this reads a single non-empty flag.
"""

from __future__ import annotations

import logging

from src.config.settings import Settings
from src.storage.adapter import AppStorageAdapter
from src.storage.storage_service_rest import StorageServiceRestStorage
from src.storage.supabase_rest import SupabaseRestStorage

logger = logging.getLogger(__name__)


def build_storage_adapter(settings: Settings) -> AppStorageAdapter:
    """Return the `AppStorageAdapter` impl selected by env presence.

    Logs the chosen backend + bucket ONCE (no secrets) — the fast way to diagnose a
    "thought it cut over" surprise. Construction is I/O-free (URL/key only)."""
    if settings.storage_service_url:
        logger.info(
            "storage_backend=storage_service base=%s public_base=%s bucket=%s",
            settings.storage_service_url,
            settings.storage_public_base_url,
            settings.app_storage_bucket,
        )
        return StorageServiceRestStorage(
            base_url=settings.storage_service_url,
            api_key=settings.storage_service_api_key,
            public_base_url=settings.storage_public_base_url,
            default_bucket=settings.app_storage_bucket,
        )

    logger.info(
        "storage_backend=supabase_legacy base=%s bucket=%s",
        settings.app_storage_url,
        settings.app_storage_bucket,
    )
    return SupabaseRestStorage(
        base_url=settings.app_storage_url,
        service_key=settings.app_storage_service_key,
        default_bucket=settings.app_storage_bucket,
    )
