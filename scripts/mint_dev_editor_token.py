#!/usr/bin/env python3
"""DEV ONLY — mint an editor-session JWT for local testing.

The real editor-session token is minted by the **Admin App backend** (P2), which
does not exist yet. This CLI is a local stand-in so the service (which only
VERIFIES) can be exercised. The signer lives in `src/auth/dev_token_mint.py`
(single signer — also behind the flag-gated `POST /api/dev/mint-editor-token`,
spec 10, and imported by the pytest suite). This script stays the tool for
DELIBERATELY-INVALID tokens (wrong aud/role/alg/expired) — the API route only
mints valid ones.

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
from pathlib import Path

# Standalone-run support: `python scripts/mint_dev_editor_token.py` puts scripts/
# (not the repo root) on sys.path — add the root so `src.*` resolves. The signer
# module is settings-free, so no env (APP_DB_URL, ...) is needed to import it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth.dev_token_mint import mint_token  # noqa: E402  (re-exported for pytest)

__all__ = ["mint_token"]


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
