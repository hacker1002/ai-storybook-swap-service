"""`/api/image/*` router group (P3c). Only `upscale-image` is ported — the remix
sub-app's upscale tab calls it. Other image-api `/api/image/*` endpoints
(normalize-ratio, normalize-human, extract-human-traits, parametric-variant,
spread-thumbnail) are out of scope (no remix surface calls them)."""
