"""Hardcoded configuration constants for the background-job lib.

Ported from `ai-storybook-python-api/src/jobs/config.py`. Per Validation Session 1
(plan 260515-1011): no env vars. Tune = code change + deploy. KISS for v1.

The semaphore caps + reaper thresholds are load-bearing (they encode real upstream
provider limits + the reaper safety net) — do NOT alter: audio 4, rmbg 1,
upscale 1, default 2; reaper INTERVAL 30s / running-stale 1800s / queued-stale
300s; shutdown drain 25s.
"""

from __future__ import annotations

# ─── Per-type semaphore caps (concurrent handlers per process) ────────────────
AUDIO_SEM_CAP = 4           # remix_audio_swap — I/O bound (ElevenLabs)
MANUSCRIPT_SEM_CAP = 2      # generate_manuscript — LLM streaming
DEMO_SEM_CAP = 2            # demo_long_running — test handler
# export_pdf — headless Chromium @300 DPI (~0.5–2GB/instance). Hard cap 1
# (Validation S1): strictly sequential, safest for large trims; no tuning.
EXPORT_PDF_SEM_CAP = 1
# render_book_video — orchestrator → video-worker (1 render slot in-flight guard
# worker-side). Hard cap 1: at most one full-book render at a time per process.
RENDER_BOOK_VIDEO_SEM_CAP = 1
# remix_rmbg / remix_upscale — crop-pipeline stage jobs, each a Replicate consumer
# (Bria remove-bg / Real-ESRGAN). Cap 1: one handler of each type per process;
# cross-type in-flight is further bounded by the GLOBAL Replicate semaphore.
REMIX_RMBG_SEM_CAP = 1
REMIX_UPSCALE_SEM_CAP = 1
# actor_swap / actor_rmbg / actor_upscale — the casting-swap crop-pipeline stage
# jobs. Each a Gemini (swap) or Replicate (rmbg/upscale) consumer; cap 1 per type
# (parity remix stage jobs) — without an entry they'd fall to DEFAULT_SEM_CAP=2 and
# run 2 upstream calls in parallel, colliding with the dev account limit.
ACTOR_SWAP_SEM_CAP = 1
ACTOR_RMBG_SEM_CAP = 1
ACTOR_UPSCALE_SEM_CAP = 1
DEFAULT_SEM_CAP = 2         # fallback for any unmapped job_type

# ─── Reaper thresholds ────────────────────────────────────────────────────────
REAPER_INTERVAL_SEC = 30        # scan period
# 1800s (2026-05-29): the mix-swap post-swap pipeline (Gemini + Replicate stages +
# uploads) can reach ~8min/sheet; full batch 30+ min. Interim ctx.report()/heartbeat
# keeps updated_at fresh during normal operation; this threshold is the safety net so
# a slow Replicate cold start does not prematurely reap an in-flight job.
REAPER_STALE_SEC = 1800         # running jobs with `updated_at` older than this → failed
REAPER_QUEUED_STALE_SEC = 300   # queued jobs older than this (spawn race) → failed

# ─── Lifespan shutdown ────────────────────────────────────────────────────────
SHUTDOWN_TIMEOUT_SEC = 25   # `wait_all` timeout in lifespan teardown

SEMS_BY_TYPE: dict[str, int] = {
    "remix_audio_swap": AUDIO_SEM_CAP,
    "generate_manuscript": MANUSCRIPT_SEM_CAP,
    "demo_long_running": DEMO_SEM_CAP,
    "export_pdf": EXPORT_PDF_SEM_CAP,
    "render_book_video": RENDER_BOOK_VIDEO_SEM_CAP,
    "remix_rmbg": REMIX_RMBG_SEM_CAP,
    "remix_upscale": REMIX_UPSCALE_SEM_CAP,
    "actor_swap": ACTOR_SWAP_SEM_CAP,
    "actor_rmbg": ACTOR_RMBG_SEM_CAP,
    "actor_upscale": ACTOR_UPSCALE_SEM_CAP,
}


def get_sem_cap(job_type: str) -> int:
    """Return the per-process concurrency cap for `job_type`.

    Falls back to `DEFAULT_SEM_CAP` for types not in `SEMS_BY_TYPE` — runner
    won't crash on first use of an unmapped handler.
    """
    return SEMS_BY_TYPE.get(job_type, DEFAULT_SEM_CAP)
