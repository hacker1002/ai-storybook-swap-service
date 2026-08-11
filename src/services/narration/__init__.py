"""Narration core services — in-process callable equivalents of the
`/api/text/narrate-script` and `/api/text/combine-audio-chunks` endpoints.

Used by both the FastAPI routers (thin adapter) and the job handler
`remix_audio_swap` (direct call — bypasses HTTP loopback).
"""
