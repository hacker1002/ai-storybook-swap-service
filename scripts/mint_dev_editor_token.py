#!/usr/bin/env python3
"""DEV ONLY — mint an editor-session JWT for local testing.

The real editor-session token is minted by the **Admin App backend** (P2), which
does not exist yet. This script is a local stand-in so the service (which only
VERIFIES) can be exercised. It lives in `scripts/` (outside `src/`) on purpose — it
is NEVER part of the service. The `mint_token` helper is also imported by the
pytest suite (DRY: one signer, not a copy).

Usage:
    uv run python scripts/mint_dev_editor_token.py [flags]   -> prints token to stdout

Flags (defaults produce a VALID admin token):
    --admin-ref STR   default dev-admin-001
    --sid STR         default dev-session-001
    --consumer STR    default dev-script
    --ttl SECONDS     default 900
    --aud STR         default remix-editor   (change to test wrong-aud -> 401)
    --role STR        default admin          (change to viewer -> 403)
    --alg STR         default HS256          ('none' tests alg confusion -> 401)
    --expired         set exp in the past    (-> 401 TOKEN_EXPIRED)
    --secret STR      default $REMIX_EDITOR_TOKEN_SECRET
"""

from __future__ import annotations

import argparse
import os
import sys
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


def _main() -> int:
    p = argparse.ArgumentParser(description="DEV ONLY editor-session token mint")
    p.add_argument("--admin-ref", default="dev-admin-001")
    p.add_argument("--sid", default="dev-session-001")
    p.add_argument("--consumer", default="dev-script")
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--aud", default="remix-editor")
    p.add_argument("--role", default="admin")
    p.add_argument("--alg", default="HS256")
    p.add_argument("--expired", action="store_true")
    p.add_argument("--secret", default=None)
    args = p.parse_args()

    secret = args.secret or os.environ.get("REMIX_EDITOR_TOKEN_SECRET", "")
    if not secret and args.alg != "none":
        print("ERROR: REMIX_EDITOR_TOKEN_SECRET not set (and no --secret)", file=sys.stderr)
        return 2

    print("# DEV ONLY token — not for production", file=sys.stderr)
    token = mint_token(
        secret=secret,
        admin_ref=args.admin_ref,
        sid=args.sid,
        consumer=args.consumer,
        ttl=args.ttl,
        aud=args.aud,
        role=args.role,
        alg=args.alg,
        expired=args.expired,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
