"""Background-jobs package (in-process asyncio) — the P3b port of image-api's
in-process job lib onto this service's `AppDbAdapter` (asyncpg) seam.

Public surface (re-exported below):
  - `enqueue`            — insert a queued row + spawn the runner task.
  - `JobContext`         — handler-facing `report()` / `check_cancel()`.
  - `JobError`           — structured `result.errors[]` entry.
  - `register` / `register_finalize` — handler + finalize-hook decorators.
  - `reaper_loop`        — long-lived stale-job sweep (spawned in lifespan).
  - `wait_all`           — drain in-flight handler tasks on shutdown.

`model_registry` (Phase 02) stays importable as a submodule
(`src.jobs.model_registry`) — it is NOT eagerly imported here to keep this
package's import light (it pulls model/remix deps).

Handler registration is a SIDE-EFFECT import (`src.jobs.handlers`) done in
`main.py` ABOVE `app = FastAPI(...)`; do NOT import handlers here (would create an
import cycle: handlers → runner → __init__).
"""

from src.jobs.errors import JobError
from src.jobs.lifespan import wait_all
from src.jobs.reaper import reaper_loop
from src.jobs.runner import (
    JobContext,
    enqueue,
    register,
    register_finalize,
)

__all__ = [
    "JobContext",
    "JobError",
    "enqueue",
    "reaper_loop",
    "register",
    "register_finalize",
    "wait_all",
]
