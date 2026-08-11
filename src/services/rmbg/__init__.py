"""Per-model remove-background INPUT adapter package.

Each model (Bria, 851-labs, …) owns its Replicate `input` payload via a
`RemoveBgAdapter`. Dispatch/retry/output stays in `replicate_client.run_remove_bg`.
"""

from src.services.rmbg.base import RemoveBgAdapter
from src.services.rmbg.registry import get_remove_bg_adapter

__all__ = ["RemoveBgAdapter", "get_remove_bg_adapter"]
