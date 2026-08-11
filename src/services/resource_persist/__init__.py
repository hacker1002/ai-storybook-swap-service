"""`resource_persist` — ported opt-in `save_generated_resource` surface.

Same public API as image-api so ported models/routers import unchanged, but the
persist orchestration is a documented NO-OP in this editor-facing service (see
`save_generated_resource.py`). Pydantic/dataclass models (`SaveResourceDirective`
etc.) are ported VERBATIM so request/response field shapes stay byte-identical.
"""

from src.services.resource_persist.models import (
    GeneratedResourceValue,
    PersistContext,
    ResourceType,
    SaveResourceDirective,
    SaveResourceOutcome,
)
from src.services.resource_persist.save_generated_resource import (
    save_generated_resource,
    save_response_fields,
)

__all__ = [
    "save_generated_resource",
    "save_response_fields",
    "SaveResourceDirective",
    "GeneratedResourceValue",
    "PersistContext",
    "SaveResourceOutcome",
    "ResourceType",
]
