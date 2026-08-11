"""Cross-batch `is_final` mutex helper for the remix mix-swap job (R1).

Ported VERBATIM from `ai-storybook-image-api/src/jobs/helpers/promote_is_final.py`
— pure (mutates its `mixes` argument in-place, does NO I/O), so no seam change.

Invariant (steady state): for every layer position `(spread_id, id)` that has
≥1 crop in any `swap_results[is_selected].crops[]` of any batch in the remix,
EXACTLY one crop has `is_final=true`.

After a sheet swap succeeds and the new `swap_result` is appended (with
`is_selected=true`), the just-appended crops are the new winners for their
positions — flip their `is_final=true` and CLEAR `is_final` on any crop with
the same key in OTHER batches (defensively also walking history rows
`is_selected=false` so reader sees a clean slate even on partial regressions).

Per-sheet promote relies on atomic full-blob persist + R1 idempotency for crash
recovery: a persist failure leaves the DB in the prior valid state, retry
re-fires R1. Caller owns persistence. NOT thread-safe under concurrent sheet
workers — the current handler pins `MAX_CONCURRENT_SHEETS=1` so no lock is needed;
bumping concurrency requires guarding the mutation.
"""

from __future__ import annotations

from typing import Any, TypedDict


class PromotionStats(TypedDict):
    promoted_count: int
    cleared_count: int
    affected_batches: list[str]


def _zero_stats() -> PromotionStats:
    return {"promoted_count": 0, "cleared_count": 0, "affected_batches": []}


def promote_is_final_for_sheet(
    mixes: list[dict[str, Any]],
    batch_idx: int,
    sheet_idx: int,
) -> PromotionStats:
    """Promote `is_final=true` on the just-appended swap_result of (batch_idx,
    sheet_idx) and clear `is_final` on cross-batch crops sharing
    `(spread_id, id)`.

    Defensive: returns zero-stats (no raise) when the target sheet has no
    selected swap_result — caller has just appended one, so this branch is a
    bug indicator but not a job-killer.
    """
    if batch_idx < 0 or batch_idx >= len(mixes):
        return _zero_stats()

    target_batch = mixes[batch_idx]
    if not isinstance(target_batch, dict):
        return _zero_stats()

    target_sheets = target_batch.get("crop_sheets") or []
    if sheet_idx < 0 or sheet_idx >= len(target_sheets):
        return _zero_stats()

    target_sheet = target_sheets[sheet_idx]
    if not isinstance(target_sheet, dict):
        return _zero_stats()

    selected = None
    for r in target_sheet.get("swap_results") or []:
        if isinstance(r, dict) and r.get("is_selected"):
            selected = r
            break
    if selected is None:
        return _zero_stats()

    target_keys: set[tuple[str | None, str | None]] = set()
    promoted_count = 0
    for crop in selected.get("crops") or []:
        if not isinstance(crop, dict):
            continue
        crop["is_final"] = True
        target_keys.add((crop.get("spread_id"), crop.get("id")))
        promoted_count += 1

    cleared_count = 0
    affected_batches: set[str] = set()
    for bi, batch in enumerate(mixes):
        if bi == batch_idx or not isinstance(batch, dict):
            continue
        for sheet in batch.get("crop_sheets") or []:
            if not isinstance(sheet, dict):
                continue
            for result in sheet.get("swap_results") or []:
                if not isinstance(result, dict):
                    continue
                for crop in result.get("crops") or []:
                    if not isinstance(crop, dict):
                        continue
                    key = (crop.get("spread_id"), crop.get("id"))
                    if key in target_keys and crop.get("is_final"):
                        crop["is_final"] = False
                        cleared_count += 1
                        bid = batch.get("id")
                        if isinstance(bid, str):
                            affected_batches.add(bid)

    return {
        "promoted_count": promoted_count,
        "cleared_count": cleared_count,
        "affected_batches": sorted(affected_batches),
    }


__all__ = ["promote_is_final_for_sheet", "PromotionStats"]
