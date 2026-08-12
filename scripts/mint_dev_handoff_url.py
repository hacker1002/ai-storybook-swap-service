#!/usr/bin/env python3
"""DEV ONLY — mint a handoff assertion + print a ready-to-paste browser deeplink.

This is the PRIMARY dev entry for exercising the Remix Editor sub-app end-to-end:
it signs a 60s handoff assertion (same helper the Admin App backend will use in P2)
and prints the full URL to paste into a browser. The sub-app reads `#handoff=` from
the fragment and calls the REAL `POST /api/editor/auth/exchange` against a locally
running swap service — so this covers the whole flow, not just a forged token.

Prereq: swap service running locally with REMIX_EDITOR_HANDOFF_SECRET matching the
one this script signs with (default dev placeholder below).

Usage:
    uv run python scripts/mint_dev_handoff_url.py --book-id <BOOK_ID> [--remix-id <ID>]

Flags:
    --book-id STR     REQUIRED — book uuid the deeplink opens
    --remix-id STR    optional — ?remix=<id> query
    --base-url STR    default http://localhost:5175 (vite entry #3 dev server — dev:remix-editor)
    --admin-ref STR   default dev-admin-001
    --admin-name STR  optional display name echoed back by exchange
    --ttl SECONDS     default 60 (service rejects exp-iat > 60s + margin)
    --handoff-secret STR  default $REMIX_EDITOR_HANDOFF_SECRET
"""

from __future__ import annotations

import argparse
import os
import sys

from mint_dev_editor_token import mint_handoff_assertion  # sibling script (scripts/ on sys.path)

_DEFAULT_BASE_URL = "http://localhost:5175"
_DEFAULT_HANDOFF_SECRET = "dev-remix-handoff-secret-change-me"


def _main() -> int:
    p = argparse.ArgumentParser(description="DEV ONLY handoff deeplink generator")
    p.add_argument("--book-id", required=True)
    p.add_argument("--remix-id", default=None)
    p.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    p.add_argument("--admin-ref", default="dev-admin-001")
    p.add_argument("--admin-name", default=None)
    p.add_argument("--ttl", type=int, default=60)
    p.add_argument("--handoff-secret", default=None)
    args = p.parse_args()

    secret = args.handoff_secret or os.environ.get("REMIX_EDITOR_HANDOFF_SECRET", _DEFAULT_HANDOFF_SECRET)

    assertion = mint_handoff_assertion(
        secret=secret,
        admin_ref=args.admin_ref,
        admin_name=args.admin_name,
        ttl=args.ttl,
    )

    base = args.base_url.rstrip("/")
    query = f"?remix={args.remix_id}" if args.remix_id else ""
    url = f"{base}/book/{args.book_id}{query}#handoff={assertion}"

    print("# DEV ONLY — assertion valid ~60s; paste the URL below into a browser", file=sys.stderr)
    print(f"# swap service must run with REMIX_EDITOR_HANDOFF_SECRET matching (default {_DEFAULT_HANDOFF_SECRET!r})", file=sys.stderr)
    print("\n[assertion]")
    print(assertion)
    print("\n[deeplink]")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
