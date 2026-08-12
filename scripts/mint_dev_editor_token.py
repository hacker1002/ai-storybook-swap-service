#!/usr/bin/env python3
"""DEV ONLY signer / test-harness — mint editor-session tokens & handoff assertions.

After ADR-053 the swap service OWNS session lifecycle: the normal dev flow is to
mint a **handoff assertion** and let the service `POST /api/editor/auth/exchange`
it into an access token. For that end-to-end dev loop use
`scripts/mint_dev_handoff_url.py` (prints a ready-to-paste browser URL).

This script is now primarily a TEST HARNESS: `mint_token()` (access token) is still
needed by pytest + the test-scripts to forge tokens the exchange endpoint CANNOT
produce (wrong-aud / expired / bad-secret / alg-none) so the verify matrix can be
exercised. `mint_handoff_assertion()` is the exchange input, reused by the URL
script + `test-auth-exchange.sh`. It lives in `scripts/` (outside `src/`) on
purpose — NEVER part of the service.

Usage:
    uv run python scripts/mint_dev_editor_token.py --mode handoff [flags]  -> assertion
    uv run python scripts/mint_dev_editor_token.py --mode access  [flags]  -> access token (harness-only)

Flags:
    --mode {access|handoff}  default access (backward-compat for _editor-common.sh + test-scripts)
    --admin-ref STR   default dev-admin-001
    --sid STR         default dev-session-001   (access mode only)
    --consumer STR    default dev-script
    --ttl SECONDS     default 900 (access) / 60 (handoff)
    --aud STR         default remix-editor / remix-editor-handoff
    --role STR        default admin          (access only; viewer -> 403)
    --alg STR         default HS256          ('none' tests alg confusion -> 401)
    --expired         set exp in the past
    --jti STR         handoff mode: fixed jti (default random) — replay test
    --admin-name STR  handoff mode: optional display name echoed by exchange
    --secret STR      access secret; default $REMIX_EDITOR_TOKEN_SECRET
    --handoff-secret STR  handoff secret; default $REMIX_EDITOR_HANDOFF_SECRET
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import jwt

_HANDOFF_AUD = "remix-editor-handoff"


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


def mint_handoff_assertion(
    *,
    secret: str,
    admin_ref: str = "dev-admin-001",
    jti: str | None = None,
    consumer: str | None = "dev-script",
    admin_name: str | None = None,
    ttl: int = 60,
    aud: str = _HANDOFF_AUD,
    alg: str = "HS256",
    expired: bool = False,
) -> str:
    """Sign a one-time handoff assertion — the INPUT to POST /api/editor/auth/exchange.

    `jti` doubles as the one-time replay key AND becomes the access token `sid` after
    exchange; a fresh random uuid4 hex each call unless pinned (replay test). TTL 60s.
    """
    now = int(time.time())
    claims: dict = {
        "aud": aud,
        "jti": jti or uuid.uuid4().hex,
        "admin_ref": admin_ref,
        "iat": now,
        "exp": now - 60 if expired else now + ttl,
    }
    if consumer is not None:
        claims["consumer"] = consumer
    if admin_name is not None:
        claims["admin_name"] = admin_name
    if alg == "none":
        return jwt.encode(claims, key=None, algorithm="none")  # type: ignore[arg-type]
    return jwt.encode(claims, secret, algorithm=alg)


def _main() -> int:
    p = argparse.ArgumentParser(description="DEV ONLY editor-session signer / test harness")
    p.add_argument("--mode", choices=["access", "handoff"], default="access")
    p.add_argument("--admin-ref", default="dev-admin-001")
    p.add_argument("--sid", default="dev-session-001")
    p.add_argument("--consumer", default="dev-script")
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--aud", default=None)
    p.add_argument("--role", default="admin")
    p.add_argument("--alg", default="HS256")
    p.add_argument("--expired", action="store_true")
    p.add_argument("--jti", default=None)
    p.add_argument("--admin-name", default=None)
    p.add_argument("--secret", default=None)
    p.add_argument("--handoff-secret", default=None)
    args = p.parse_args()

    if args.mode == "handoff":
        secret = args.handoff_secret or os.environ.get("REMIX_EDITOR_HANDOFF_SECRET", "")
        if not secret and args.alg != "none":
            print("ERROR: REMIX_EDITOR_HANDOFF_SECRET not set (and no --handoff-secret)", file=sys.stderr)
            return 2
        print("# DEV ONLY handoff assertion — not for production", file=sys.stderr)
        print(
            mint_handoff_assertion(
                secret=secret,
                admin_ref=args.admin_ref,
                jti=args.jti,
                consumer=args.consumer,
                admin_name=args.admin_name,
                ttl=args.ttl if args.ttl is not None else 60,
                aud=args.aud or _HANDOFF_AUD,
                alg=args.alg,
                expired=args.expired,
            )
        )
        return 0

    secret = args.secret or os.environ.get("REMIX_EDITOR_TOKEN_SECRET", "")
    if not secret and args.alg != "none":
        print("ERROR: REMIX_EDITOR_TOKEN_SECRET not set (and no --secret)", file=sys.stderr)
        return 2

    print(
        "# DEV ONLY access token — HARNESS-ONLY (forges tokens exchange can't make). "
        "Normal dev flow: scripts/mint_dev_handoff_url.py",
        file=sys.stderr,
    )
    token = mint_token(
        secret=secret,
        admin_ref=args.admin_ref,
        sid=args.sid,
        consumer=args.consumer,
        ttl=args.ttl if args.ttl is not None else 900,
        aud=args.aud or "remix-editor",
        role=args.role,
        alg=args.alg,
        expired=args.expired,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
