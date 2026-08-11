"""Remix core services.

P3b Phase 02 seeds ONLY `errors.py` (RemixDomainError) — the dependency shared by
`jobs/model_registry.py` + `gemini/model_resolution.py`. Phase 05 ports the full
core set (composers, resolvers, swap/detect cores) into this package; `errors.py`
is byte-identical to image-api so the later port is idempotent.
"""
