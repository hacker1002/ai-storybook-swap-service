"""In-memory fake `AppStorageAdapter` for unit tests.

Mirrors `tests/fakes/fake_app_db_adapter.py`: one seam swapped via
`set_storage(FakeAppStorageAdapter())`, zero network. `upload` records the call +
returns a deterministic public URL so ported cores' upload path is exercised
without touching Supabase Storage.
"""

from __future__ import annotations


class FakeAppStorageAdapter:
    def __init__(self, base_url: str = "https://storage.test/object/public") -> None:
        self.base_url = base_url
        self.uploads: list[dict] = []

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str,
        bucket: str | None = None,
        upsert: bool = True,
    ) -> str:
        self.uploads.append(
            {"path": path, "bytes": len(data), "content_type": content_type, "bucket": bucket}
        )
        return self.public_url(path, bucket=bucket)

    def public_url(self, path: str, bucket: str | None = None) -> str:
        b = bucket or "remix"
        return f"{self.base_url}/{b}/{path}"

    async def create_signed_url(
        self, path: str, expires_in: int, bucket: str | None = None
    ) -> str:
        return f"{self.public_url(path, bucket=bucket)}?token=fake&exp={expires_in}"

    async def delete(self, path: str, bucket: str | None = None) -> None:
        return None
