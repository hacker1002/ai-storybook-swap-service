"""Image-domain service core (framework-agnostic).

Holds service-first cores for `/api/image/*` endpoints — e.g.
`upscale_core.run_upscale()` — that raise `ImageDomainError` instead of
HTTPException so an in-process job (ADR-031) or the thin HTTP router can both
reuse them. The router maps `ImageDomainError` to the spec envelope via the
`@app.exception_handler` in `main.py`.
"""
