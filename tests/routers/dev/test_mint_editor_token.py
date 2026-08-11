"""POST /api/dev/mint-editor-token (spec 10) — flag gating + key gate + round-trip.

The main app is built with the flag OFF (conftest env) → the route must be ABSENT
there (plain 404). The enabled path is tested on a fresh FastAPI app mounting the
dev router directly, with `settings.dev_mint_key` monkeypatched — mirroring what
main.py does when DEV_MINT_ENABLED=true.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.editor_session import verify_editor_session
from src.config.settings import settings
from src.core.errors import register_exception_handlers

_DEV_KEY = "test-dev-mint-key"
_PATH = "/api/dev/mint-editor-token"


@pytest.fixture
def dev_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "dev_mint_key", _DEV_KEY)
    from src.routers.dev.router import router as dev_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(dev_router)
    return TestClient(app)


def _decode_unverified(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


class TestFlagGating:
    def test_route_absent_on_main_app_when_flag_off(self, client):
        # conftest env has no DEV_MINT_ENABLED -> main.py never registers /api/dev.
        r = client.post(_PATH, headers={"X-Dev-Mint-Key": _DEV_KEY})
        assert r.status_code == 404
        # Starlette default body — indistinguishable from any unknown path.
        assert r.json() == {"detail": "Not Found"}

    def test_handler_defense_in_depth_when_key_unset(self, monkeypatch):
        # Even if registration were wired by mistake with no key configured, the
        # handler must refuse (never an open mint).
        monkeypatch.setattr(settings, "dev_mint_key", "")
        from src.routers.dev.router import router as dev_router

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(dev_router)
        r = TestClient(app).post(_PATH, headers={"X-Dev-Mint-Key": ""})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "DEV_KEY_INVALID"


class TestKeyGate:
    def test_missing_key_401(self, dev_client):
        r = dev_client.post(_PATH)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "DEV_KEY_INVALID"

    def test_wrong_key_401_same_code(self, dev_client):
        r = dev_client.post(_PATH, headers={"X-Dev-Mint-Key": "wrong"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "DEV_KEY_INVALID"


class TestMint:
    def test_empty_body_defaults_and_round_trip(self, dev_client):
        r = dev_client.post(_PATH, headers={"X-Dev-Mint-Key": _DEV_KEY})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        data = r.json()["data"]
        assert data["tokenType"] == "Bearer"
        assert data["adminRef"] == "dev-admin-001"
        assert data["sid"].startswith("dev-")

        # Round-trip: the minted token must pass the service's OWN verifier.
        ctx = verify_editor_session(f"Bearer {data['token']}")
        assert ctx.admin_ref == "dev-admin-001"
        assert ctx.sid == data["sid"]
        assert ctx.consumer == "dev-mint-api"

        claims = _decode_unverified(data["token"])
        assert claims["aud"] == "remix-editor"
        assert claims["role"] == "admin"
        assert claims["exp"] - claims["iat"] == 900
        assert data["expiresAt"].endswith("Z")

    def test_custom_claims_echoed(self, dev_client):
        r = dev_client.post(
            _PATH,
            headers={"X-Dev-Mint-Key": _DEV_KEY},
            json={"adminRef": "qa-admin", "sid": "qa-sid-1", "consumer": "qa"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["adminRef"] == "qa-admin"
        assert data["sid"] == "qa-sid-1"
        claims = _decode_unverified(data["token"])
        assert claims["admin_ref"] == "qa-admin"
        assert claims["consumer"] == "qa"

    @pytest.mark.parametrize(("requested", "effective"), [(1, 60), (999999, 3600), (1800, 1800)])
    def test_ttl_clamped_not_rejected(self, dev_client, requested, effective):
        r = dev_client.post(
            _PATH, headers={"X-Dev-Mint-Key": _DEV_KEY}, json={"ttlSeconds": requested}
        )
        assert r.status_code == 200
        claims = _decode_unverified(r.json()["data"]["token"])
        assert claims["exp"] - claims["iat"] == effective

    def test_sid_unique_per_mint_when_not_supplied(self, dev_client):
        sids = {
            dev_client.post(_PATH, headers={"X-Dev-Mint-Key": _DEV_KEY}).json()["data"]["sid"]
            for _ in range(3)
        }
        assert len(sids) == 3

    def test_signs_with_newest_rotation_secret(self, dev_client, monkeypatch):
        monkeypatch.setattr(
            type(settings),
            "editor_token_secrets",
            property(lambda _self: ["old-secret", "test-secret-constant-do-not-reuse"]),
        )
        r = dev_client.post(_PATH, headers={"X-Dev-Mint-Key": _DEV_KEY})
        token = r.json()["data"]["token"]
        jwt.decode(
            token, "test-secret-constant-do-not-reuse", algorithms=["HS256"], audience="remix-editor"
        )  # raises if signed with old


class TestValidation:
    def test_bad_type_400(self, dev_client):
        r = dev_client.post(
            _PATH, headers={"X-Dev-Mint-Key": _DEV_KEY}, json={"ttlSeconds": "abc"}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_extra_field_forbidden_400(self, dev_client):
        r = dev_client.post(
            _PATH, headers={"X-Dev-Mint-Key": _DEV_KEY}, json={"bookId": "x"}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
