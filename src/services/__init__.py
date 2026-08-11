"""Shared infrastructure services (SSRF guard, HTTP fetch, Pillow image ops).

Ported from image-api. NO Supabase SDK coupling — the storage seam lives in
`src/storage/` (httpx REST).
"""
