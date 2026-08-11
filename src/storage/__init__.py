"""Storage seam for the Remix Swap Service.

Mirrors the `src/db` adapter pattern: an `AppStorageAdapter` Protocol + a
module-global accessor (`get_storage`/`set_storage`). The concrete impl talks to
Supabase Storage over the REST API via httpx (NO Supabase SDK) — see
`supabase_rest.py`. Swapping to S3/GCS later means writing one new adapter.
"""
