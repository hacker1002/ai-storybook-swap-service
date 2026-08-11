"""Editor-session token SIGNER — single signing implementation (DRY).

Consumers:
  - `scripts/mint_dev_editor_token.py` (CLI — incl. deliberately-invalid tokens
    for negative tests: wrong aud/role/alg/expired)
  - `POST /api/dev/mint-editor-token` (spec 10 — flag-gated DEV stand-in for the
    Admin App backend mint; only ever mints VALID tokens)
  - pytest fixtures (tests/conftest.py)

DELIBERATELY pure: imports `jwt` only — NO `src.config.settings` — so the CLI runs
standalone without APP_DB_URL/.env and importing this module never boot-fails.
Production mint stays the Admin App backend's job (auth spec §4); nothing in the
service request path calls this except the flag-gated dev route.
"""

from __future__ import annotations

import time

import jwt


def mint_token(
    *,
    secret: str,
    admin_ref: str = "dev-admin-001",
    sid: str = "dev-session-001",
    consumer: str | None = "dev-script",
    ttl: int = 900,
    aud: str = "remix-editor",
    role: str = "admin",
    alg: str = "HS256",
    expired: bool = False,
) -> str:
    now = int(time.time())
    exp = now - 60 if expired else now + ttl
    claims: dict = {
        "aud": aud,
        "role": role,
        "admin_ref": admin_ref,
        "sid": sid,
        "iat": now,
        "exp": exp,
    }
    if consumer is not None:
        claims["consumer"] = consumer
    if alg == "none":
        # Unsigned token — must be rejected by the verifier (alg-confusion test).
        return jwt.encode(claims, key=None, algorithm="none")  # type: ignore[arg-type]
    return jwt.encode(claims, secret, algorithm=alg)
