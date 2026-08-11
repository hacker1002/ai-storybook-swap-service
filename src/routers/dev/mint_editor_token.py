"""POST /api/dev/mint-editor-token (spec 10) — DEV-only editor-session mint.

Stand-in for the Admin App backend mint (auth spec §4 — P2, does not exist yet) so
the sub-app FE + live tests can run end-to-end against this service alone. The
route is only REGISTERED when `DEV_MINT_ENABLED=true` (see main.py) — this module
never runs in a default deploy. Gate = `X-Dev-Mint-Key` header vs `DEV_MINT_KEY`
env (constant-time). No DB, no entitlement check — which is exactly why this can
never graduate to a production mint path.

Only VALID tokens are minted here (aud/role/alg fixed). Negative-test tokens stay
the CLI's job (`scripts/mint_dev_editor_token.py`).

ENVELOPE: `/api/editor/*` `ServiceError` shape (editor-native route family).
"""

from __future__ import annotations

import secrets as py_secrets
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import Header, Response
from pydantic import BaseModel, ConfigDict, Field

from src.auth.dev_token_mint import mint_token
from src.config.settings import settings
from src.core.errors import ServiceError
from src.core.logging import get_logger

logger = get_logger("dev.mint_editor_token")

# TTL clamp bounds — auth spec §2.1 (recommended 15m, hard max 60m).
_TTL_MIN = 60
_TTL_MAX = 3600
_TTL_DEFAULT = 900
_CONSUMER_DEFAULT = "dev-mint-api"  # distinct from the CLI's "dev-script"


def dev_key_invalid() -> ServiceError:
    # One code for missing AND wrong key — no differentiation (spec 10).
    return ServiceError("DEV_KEY_INVALID", 401, "Invalid dev mint key")


class DevMintEditorTokenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adminRef: str = Field(default="dev-admin-001", min_length=1)
    # Default None -> a fresh `dev-<uuid4>` per mint (per-session trace semantics).
    sid: str | None = Field(default=None, min_length=1)
    consumer: str = Field(default=_CONSUMER_DEFAULT, min_length=1)
    # Out-of-range values are CLAMPED (not rejected) per spec 10.
    ttlSeconds: int = _TTL_DEFAULT


async def mint_editor_token(
    response: Response,
    params: DevMintEditorTokenParams | None = None,
    x_dev_mint_key: str | None = Header(default=None, alias="X-Dev-Mint-Key"),
) -> dict:
    # Registration is flag-gated, but re-check here so a future registration
    # mistake can't silently expose an ungated mint (defense in depth). The key is
    # guaranteed non-empty at boot (main.py fail-fast).
    if not settings.dev_mint_key or x_dev_mint_key is None:
        raise dev_key_invalid()
    if not py_secrets.compare_digest(x_dev_mint_key.encode(), settings.dev_mint_key.encode()):
        raise dev_key_invalid()

    params = params or DevMintEditorTokenParams()
    ttl = max(_TTL_MIN, min(_TTL_MAX, params.ttlSeconds))
    sid = params.sid or f"dev-{uuid.uuid4()}"

    # Sign with the NEWEST rotation secret (list convention "old,new") so freshly
    # minted tokens outlive a rotation window.
    token = mint_token(
        secret=settings.editor_token_secrets[-1],
        admin_ref=params.adminRef,
        sid=sid,
        consumer=params.consumer,
        ttl=ttl,
    )
    # exp read back from the token itself — exact, no clock re-read drift.
    exp = jwt.decode(token, options={"verify_signature": False})["exp"]

    # Token is a credential — never cache, never log (log ids + ttl only).
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "dev_token_minted",
        extra={"data": {"admin_ref": params.adminRef, "sid": sid, "ttl": ttl}},
    )
    return {
        "success": True,
        "data": {
            "token": token,
            "tokenType": "Bearer",
            "expiresAt": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "adminRef": params.adminRef,
            "sid": sid,
        },
    }
