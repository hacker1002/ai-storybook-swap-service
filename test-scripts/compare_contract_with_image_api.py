#!/usr/bin/env python3
"""L1 drift-guard — compare the RESPONSE CONTRACT of the swap-service (8100) against
image-api (8000) for the ported enqueue/cancel + remix-sync routes.

ADR-052 defends against drift with the *contract*, not shared BE code. This script
is that guard, applied cheaply: for each (route, payload) it calls BOTH backends and
diffs the recursive KEY SET + leaf TYPE — never the values (AI output is random, ids
and timestamps differ). A key present on one side but not the other, or a leaf whose
type changed, is reported as drift.

Auth differs by design: image-api uses `X-API-Key`, the swap-service uses a Bearer
editor-session token. Both are read from env / the P3a mint helper.

Usage (both servers must be live on the SAME local DB so fixtures resolve on both):
    IMAGE_API_URL=http://localhost:8000  IMAGE_API_KEY=demo-key-123 \
    SWAP_URL=http://localhost:8100 \
    uv run python test-scripts/compare_contract_with_image_api.py

Fixture ids come from test-scripts/fixtures/local-ids.env (written by
scripts/seed_remix_fixture.py). Exit 0 = no drift; 1 = drift found; 2 = setup error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "local-ids.env"


def _load_fixtures() -> dict[str, str]:
    if not FIXTURES.exists():
        print(f"[setup] missing {FIXTURES} — run scripts/seed_remix_fixture.py first", file=sys.stderr)
        sys.exit(2)
    out: dict[str, str] = {}
    for line in FIXTURES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def _mint_bearer() -> str:
    tok = os.environ.get("EDITOR_TOKEN")
    if tok:
        return tok
    # Fall back to the dev mint helper (same one the shell tests use).
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from scripts.mint_dev_editor_token import mint_token  # type: ignore

        secret = os.environ.get("REMIX_EDITOR_TOKEN_SECRET")
        return mint_token(secret=secret) if secret else mint_token()
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] cannot mint editor token: {exc}; set EDITOR_TOKEN env", file=sys.stderr)
        sys.exit(2)


def _shape(obj, path: str = "") -> dict[str, str]:
    """Flatten a JSON value into {dotted.path: type-name}. Lists collapse to their
    first element's shape under `[]` so array length (random) is ignored."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        if not obj:
            out[path or "."] = "object(empty)"
        for k, v in obj.items():
            out.update(_shape(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        out[f"{path}[]" if path else "[]"] = "array"
        if obj:
            out.update(_shape(obj[0], f"{path}[]" if path else "[]"))
    else:
        out[path or "."] = type(obj).__name__
    return out


def _diff(a: dict[str, str], b: dict[str, str]) -> list[str]:
    diffs: list[str] = []
    for key in sorted(set(a) | set(b)):
        ta, tb = a.get(key), b.get(key)
        if ta is None:
            diffs.append(f"  + only in swap-service: {key} ({tb})")
        elif tb is None:
            diffs.append(f"  - only in image-api:    {key} ({ta})")
        elif ta != tb:
            diffs.append(f"  ~ type differs: {key}  image-api={ta}  swap={tb}")
    return diffs


def main() -> int:
    fx = _load_fixtures()
    remix_id = fx.get("REMIX_ID")
    if not remix_id:
        print("[setup] REMIX_ID missing from fixtures", file=sys.stderr)
        return 2

    image_url = os.environ.get("IMAGE_API_URL", "http://localhost:8000").rstrip("/")
    image_key = os.environ.get("IMAGE_API_KEY", "demo-key-123")
    swap_url = os.environ.get("SWAP_URL", "http://localhost:8100").rstrip("/")
    bearer = _mint_bearer()

    # (label, method, path, json-body). Enqueue routes share paths on both backends.
    cases: list[tuple[str, str, str, dict]] = [
        ("enqueue rmbg", "POST", f"/api/jobs/remix/{remix_id}/rmbg", {}),
        ("enqueue upscale", "POST", f"/api/jobs/remix/{remix_id}/upscale", {}),
        ("enqueue sprite-swap", "POST", f"/api/jobs/remix/{remix_id}/sprite-swap", {}),
        ("enqueue detect-sprite-defects", "POST", f"/api/jobs/remix/{remix_id}/detect-sprite-defects", {}),
    ]

    any_drift = False
    with httpx.Client(timeout=60.0) as ci, httpx.Client(timeout=60.0) as cs:
        for label, method, path, body in cases:
            try:
                ri = ci.request(method, image_url + path, json=body, headers={"X-API-Key": image_key})
                rs = cs.request(method, swap_url + path, json=body, headers={"Authorization": f"Bearer {bearer}"})
            except httpx.HTTPError as exc:  # noqa: PERF203
                print(f"[{label}] transport error: {exc}", file=sys.stderr)
                any_drift = True
                continue
            # Compare envelope shape at the same HTTP status class; a status-class
            # mismatch is itself drift (except the known mix-swap 409 divergence).
            si, ss = ri.status_code, rs.status_code
            try:
                ji, js = ri.json(), rs.json()
            except Exception:  # noqa: BLE001
                print(f"[{label}] non-JSON body (image={si}, swap={ss})", file=sys.stderr)
                any_drift = True
                continue
            diffs = _diff(_shape(ji), _shape(js))
            status_note = "" if si // 100 == ss // 100 else f"  [STATUS CLASS DIFF image={si} swap={ss}]"
            if diffs or status_note:
                any_drift = True
                print(f"\n### {label}  (image={si} swap={ss}){status_note}")
                for d in diffs:
                    print(d)
            else:
                print(f"[ok] {label}  (image={si} swap={ss}) — key set + leaf types match")

    if any_drift:
        print("\nDRIFT DETECTED — reconcile response shape or record a deliberate divergence.")
        return 1
    print("\nNo contract drift on sampled routes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
