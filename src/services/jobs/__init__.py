"""Job-domain resolvers (pure, no I/O) shared across job handlers.

Ported from `ai-storybook-image-api/src/services/jobs/` (P3b). Modules here project
a `remixes` stage batch into the request shapes handlers need, so sibling handlers
can never drift.
"""
