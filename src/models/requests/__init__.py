"""Request-model constants used by the ported AI call layer.

P3b scope note: only the model-id CONSTANTS that `services/replicate_client.py`
imports verbatim live here — NOT the full Pydantic request models from image-api
(those belong to endpoints outside the swap-service's jobs pipeline). Values are
byte-identical to `ai-storybook-python-api/src/models/requests/*` so the ported
`replicate_client.py` keeps its import lines verbatim (no dispatch-table drift).
"""
