"""851-labs `851-labs/background-remover` adapter (InSPyReNet / transparent-background).

Input schema DIFFERS from Bria — it has NO `preserve_alpha`. Inputs (confirmed
against the LIVE Replicate version schema at Phase 02 Final Step, 2026-06-13):
  - `image` (uri, required)
  - `background_type` (default `rgba`)
  - `format` (default `png`)
  - `threshold` (default 0), `reverse` (default false) — NOT exposed v1 (YAGNI)

`background_type="rgba"` = transparent foreground, which mirrors Bria's default
(transparent bg, `backgroundColor=null` path in the core). `format="png"` keeps
the alpha channel.

DISPATCH: 851-labs is a COMMUNITY model — `predictions.create(model="851-labs/
background-remover")` 404s (the `model=` endpoint serves OFFICIAL models only).
It MUST be dispatched by a pinned `version=`. The hash below is the latest
version as of 2026-06-13; bump it to adopt a newer release.
"""

from __future__ import annotations

from src.services.rmbg.base import RemoveBgAdapter


class Labs851RemoveBgAdapter(RemoveBgAdapter):
    model_id = "851-labs/background-remover"
    version = "a029dff38972b5fda4ec5d75d7d1cd25aeff621d2cf4946a41055d7db66b80bc"

    def build_payload(self, image_value: str, preserve_alpha: bool) -> dict:
        del preserve_alpha  # 851-labs has NO preserve_alpha input
        # background_type="rgba" = transparent foreground ≡ Bria default parity.
        return {"image": image_value, "background_type": "rgba", "format": "png"}
