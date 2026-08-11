"""Per-model remove-background INPUT adapter (payload-only).

Each Replicate remove-bg model has a DIFFERENT input schema (Bria wants
`preserve_alpha`; 851-labs/InSPyReNet wants `background_type`/`format`). An
adapter owns the ONLY part that differs between models — building the Replicate
`input` dict. Dispatch (slot/429-retry/error-map) and output extraction stay in
`replicate_client.run_remove_bg`, which the core calls AFTER building the payload.

v1 needs no `parse_output`: both shipped models return a single output URI, so
`run_remove_bg`'s `_extract_url` already covers it. Add `parse_output` only when
a model returns a different output shape (YAGNI).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class RemoveBgAdapter(ABC):
    """Base class for per-model Replicate remove-bg input adapters.

    Subclasses set `model_id` (the public owner/name ref, == provider) and
    implement `build_payload`. The registry keys adapters by `model_id`.

    `version` distinguishes the two Replicate dispatch modes:
      - None (default) → OFFICIAL model: dispatch by `model=owner/name`, Replicate
        resolves the latest version server-side (e.g. Bria).
      - set → COMMUNITY model: dispatch by a PINNED `version=<hash>`. The
        `model=owner/name` endpoint 404s for community models, so the version is
        mandatory. Bump the hash to adopt a new model release (reproducibility
        tradeoff — pinned community versions never auto-update).
    """

    model_id: str = ""
    version: str | None = None

    @abstractmethod
    def build_payload(self, image_value: str, preserve_alpha: bool) -> dict:
        """Return the Replicate `input` dict for this model.

        `image_value` is an HTTPS URL or a data URI (both accepted by a
        `format: uri` field). `preserve_alpha` is the public knob — adapters
        whose model has no such input ignore it (e.g. 851-labs).
        """
        raise NotImplementedError
