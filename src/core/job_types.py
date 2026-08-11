"""Canonical `background_jobs.type` string constants for the 8 remix job types.

The `type` column has NO DB CHECK constraint — a mistyped string is NOT rejected
by Postgres; it just means the FE (which keys off `REMIX_SWAP_TYPES`) silently
never picks up the job. So every enqueue route + handler `@register(...)` MUST use
these constants as the single source of truth (they match image-api verbatim).
"""

from __future__ import annotations

# Stamped into every job's `params.source` on enqueue. The service shares the
# `background_jobs` table with image-api/editor, so this marker is how the reaper
# scopes its sweep to its OWN rows — reclaiming a foreign (image-api) job would
# flip it to `failed` WITHOUT running that service's finalize hook, orphaning its
# distribution/video leaf. Also the cost/audit rollup discriminator.
SERVICE_SOURCE = "remix-swap-service"

JOB_TYPE_AUDIO_SWAP = "remix_audio_swap"
JOB_TYPE_SPRITE_SWAP = "remix_sprite_swap"
JOB_TYPE_MIX_SWAP = "remix_mix_swap"
JOB_TYPE_RMBG = "remix_rmbg"
JOB_TYPE_UPSCALE = "remix_upscale"
JOB_TYPE_DETECT_DEFECTS = "remix_detect_defects"
JOB_TYPE_DETECT_MIX = "remix_detect_mix_defects"
JOB_TYPE_DETECT_RMBG = "remix_detect_rmbg_defects"

ALL_REMIX_JOB_TYPES = (
    JOB_TYPE_AUDIO_SWAP,
    JOB_TYPE_SPRITE_SWAP,
    JOB_TYPE_MIX_SWAP,
    JOB_TYPE_RMBG,
    JOB_TYPE_UPSCALE,
    JOB_TYPE_DETECT_DEFECTS,
    JOB_TYPE_DETECT_MIX,
    JOB_TYPE_DETECT_RMBG,
)
